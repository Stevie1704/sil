#include "bus.hpp"

#include <algorithm>
#include <stdexcept>

namespace can {
namespace {
std::uint32_t u32(std::span<const std::uint8_t> b, std::size_t at) {
  return std::uint32_t(b[at]) | (std::uint32_t(b[at + 1]) << 8) |
         (std::uint32_t(b[at + 2]) << 16) | (std::uint32_t(b[at + 3]) << 24);
}
void require(bool condition, const char* message) {
  if (!condition) throw std::runtime_error(message);
}
bool supported_bitrate(std::uint32_t rate) {
  return rate >= min_bitrate && rate <= max_bitrate && ns_per_s % rate == 0;
}
inline constexpr std::uint8_t confirm_code = 0x20;
inline constexpr std::uint8_t arbitration_lost_code = 0x30;
Bytes identifier_operation(std::uint8_t code, std::uint32_t id) {
  return {code, 0, 0, 0, 12, 0, 0, 0,
          std::uint8_t(id), std::uint8_t(id >> 8), 0, 0};
}
// ISO 11898-1 CRC-15 shift register over SOF through the data field.
unsigned crc15(const std::vector<bool>& bits) {
  unsigned crc = 0;
  for (bool bit : bits) {
    const bool next = bit != bool((crc >> 14) & 1);
    crc = (crc << 1) & 0x7fff;
    if (next) crc ^= 0x4599;
  }
  return crc;
}
// Stuff bits count towards the next run, and five equal bits at the end of
// the CRC sequence still get one.
unsigned stuff_bits(const std::vector<bool>& bits) {
  unsigned stuffed = 0, run = 0;
  bool last = true;
  for (bool bit : bits) {
    run = bit == last ? run + 1 : 1;
    last = bit;
    if (run == 5) {
      ++stuffed;
      last = !bit;
      run = 1;
    }
  }
  return stuffed;
}
}  // namespace

unsigned frame_bits(std::uint32_t id, std::span<const std::uint8_t> data) {
  std::vector<bool> bits{false};  // SOF
  const auto append = [&](unsigned value, int width) {
    for (int k = width - 1; k >= 0; --k) bits.push_back((value >> k) & 1);
  };
  append(id, 11);
  append(0, 3);  // RTR, IDE, r0
  append(unsigned(data.size()), 4);
  for (auto byte : data) append(byte, 8);
  append(crc15(bits), 15);
  // CRC delimiter, ACK slot, ACK delimiter and EOF are fixed form.
  return unsigned(bits.size()) + stuff_bits(bits) + 1 + 1 + 1 + 7;
}

void Bus::configure(unsigned active_nodes, unsigned queue_capacity) {
  require(active_nodes >= 1 && active_nodes <= terminal_capacity,
          "active node count outside declared terminal capacity");
  require(queue_capacity >= 1 && queue_capacity <= max_queue_capacity,
          "per-node queue capacity must be 1..64");
  require(!has_pending() && !on_wire_, "cannot reconfigure a busy bus");
  for (const auto& notifications : notifications_)
    require(notifications.empty(), "cannot reconfigure pending notifications");
  active_nodes_ = active_nodes;
  queue_capacity_ = queue_capacity;
}

bool Bus::has_pending() const {
  return std::any_of(queues_.begin(), queues_.end(),
                     [](const auto& queue) { return !queue.empty(); });
}

void Bus::receive(const Inputs& inputs, Nanoseconds now) {
  require(now >= 0 && now <= max_time, "event time outside the supported range");
  require(!next_event() || *next_event() >= now, "a bus countdown was skipped");
  // Validate the complete transaction before committing any operation.
  auto next = *this;
  std::vector<std::pair<unsigned, Request>> requests;
  for (unsigned terminal = 0; terminal < inputs.size(); ++terminal) {
    auto operations = inputs[terminal];
    require(terminal < active_nodes_ || operations.empty(), "input to inactive terminal");
    while (!operations.empty()) {
      require(operations.size() >= 8, "truncated operation header");
      const auto code = u32(operations, 0), length = u32(operations, 4);
      require(length >= 8 && length <= operations.size(), "invalid operation length");
      const auto op = operations.first(length);
      if (code == 0x40) {
        require(length >= 9, "missing configuration kind");
        if (op[8] == 1) {
          require(length == 13, "invalid bitrate configuration length");
          const auto rate = u32(op, 9);
          require(supported_bitrate(rate),
                  "unsupported bitrate: need 10000..1000000 bit/s dividing 1e9");
          require(!next.bitrate_ || *next.bitrate_ == rate, "inconsistent node bitrates");
          next.bitrate_ = rate;
          next.configured_[terminal] = true;
        } else if (op[8] == 4) {
          require(length == 10 && (op[9] == 1 || op[9] == 2),
                  "unsupported arbitration-loss behavior");
          next.discards_on_loss_[terminal] = op[9] == 2;
        } else {
          throw std::runtime_error("unsupported configuration kind");
        }
      } else if (code == 0x10) {
        require(length >= 16, "truncated Classical CAN operation");
        const unsigned size = unsigned(op[14]) | (unsigned(op[15]) << 8);
        require(size <= 8 && length == 16 + size, "invalid Classical CAN payload length");
        require(u32(op, 8) <= 0x7ff && op[12] == 0 && op[13] == 0,
                "only 11-bit Classical CAN data frames are supported");
        requests.emplace_back(terminal, Request{Bytes(op.begin(), op.end()), u32(op, 8)});
      } else {
        throw std::runtime_error("unsupported CAN operation");
      }
      operations = operations.subspan(length);
    }
  }
  for (const auto& [sender, request] : requests) next.enqueue(sender, request);
  if (!next.on_wire_ && next.has_pending() && now >= next.idle_from_)
    next.arbitrate(now);
  *this = std::move(next);
}

void Bus::enqueue(unsigned sender, const Request& request) {
  require(bitrate_.has_value(), "CAN bitrate is not configured");
  for (unsigned node = 0; node < active_nodes_; ++node)
    require(configured_[node],
            "every active terminal must configure the CAN bitrate before transmitting");
  require(queues_[sender].size() < queue_capacity_, "per-node CAN queue is full");
  queues_[sender].push_back(request);
}

void Bus::arbitrate(Nanoseconds now) {
  unsigned winner = terminal_capacity;
  for (unsigned node = 0; node < active_nodes_; ++node)
    if (!queues_[node].empty() &&
        (winner == terminal_capacity || queues_[node].front().id < queues_[winner].front().id))
      winner = node;
  require(winner < terminal_capacity, "arbitration without pending frames");
  const auto chosen = queues_[winner].front();
  Transfer transfer{chosen.operation, {}, now + Nanoseconds(frame_bits(chosen.id,
                                 std::span<const std::uint8_t>(chosen.operation).subspan(16))) * bit_time()};
  require(transfer.end <= max_time, "frame end beyond the supported time range");
  for (unsigned node = 0; node < active_nodes_; ++node) {
    if (queues_[node].empty()) continue;
    const auto& contender = queues_[node].front();
    if (contender.id == chosen.id) {
      require(contender.operation == chosen.operation,
              "equal CAN identifiers with different payloads cannot be resolved without error modeling");
      transfer.senders[node] = true;
      queues_[node].pop_front();
    } else if (discards_on_loss_[node]) {
      // The standard ArbitrationLost operation contains the lost identifier.
      auto& out = notifications_[node];
      const auto notice = identifier_operation(arbitration_lost_code, contender.id);
      out.insert(out.end(), notice.begin(), notice.end());
      queues_[node].pop_front();
    }
  }
  on_wire_ = std::move(transfer);
}

void Bus::complete(Nanoseconds now) {
  require(on_wire_ && on_wire_->end == now,
          "no frame ends at this instant");
  const auto& done = *on_wire_;
  const auto& frame = done.operation;
  for (unsigned node = 0; node < active_nodes_; ++node) {
    outputs_[node] = notifications_[node];
    if (done.senders[node]) {
      const auto confirmation = identifier_operation(confirm_code, u32(frame, 8));
      outputs_[node].insert(outputs_[node].end(), confirmation.begin(), confirmation.end());
    } else
      outputs_[node].insert(outputs_[node].end(), frame.begin(), frame.end());
  }
  notifications_ = {};
  idle_from_ = now + intermission_bits * bit_time();
  on_wire_.reset();
}

std::optional<Nanoseconds> Bus::next_event() const {
  if (on_wire_) return on_wire_->end;
  if (has_pending()) return idle_from_;
  return std::nullopt;
}
}  // namespace can
