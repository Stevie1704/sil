#include "bus.hpp"

#include <algorithm>

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

void Bus::receive(const Inputs& inputs, Nanoseconds now) {
  require(now >= 0 && now <= max_time, "event time outside the supported range");
  require(transfers_.empty() || transfers_.front().end >= now, "a frame completion was skipped");
  // Validate the complete transaction before committing any frame.
  auto next = *this;
  std::vector<std::pair<unsigned, std::span<const std::uint8_t>>> requests;
  for (unsigned terminal = 0; terminal < inputs.size(); ++terminal) {
    auto operations = inputs[terminal];
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
          require(length == 10 && op[9] == 1,
                  "only BufferAndRetransmit configuration is supported");
        } else {
          throw std::runtime_error("unsupported configuration kind");
        }
      } else if (code == 0x10) {
        require(length >= 16, "truncated Classical CAN operation");
        const unsigned size = unsigned(op[14]) | (unsigned(op[15]) << 8);
        require(size <= 8 && length == 16 + size, "invalid Classical CAN payload length");
        require(u32(op, 8) <= 0x7ff && op[12] == 0 && op[13] == 0,
                "only 11-bit Classical CAN data frames are supported");
        requests.emplace_back(terminal, op);
      } else {
        throw std::runtime_error("unsupported CAN operation");
      }
      operations = operations.subspan(length);
    }
  }
  for (const auto& [sender, op] : requests) next.schedule(sender, op, now);
  *this = std::move(next);
}

void Bus::schedule(unsigned sender, std::span<const std::uint8_t> operation, Nanoseconds now) {
  require(configured_[0] && configured_[1],
          "every terminal must configure the CAN bitrate before transmitting");
  // A frame that starts now or later is still eligible at an arbitration
  // opportunity; a second one there would need arbitration (issue #154).
  require(std::none_of(transfers_.begin(), transfers_.end(),
                       [&](const Transfer& t) { return t.start >= now; }),
          "competing transmission requests need arbitration, which is unsupported");
  const auto opportunity = transfers_.empty()
      ? idle_from_ : transfers_.back().end + intermission_bits * bit_time();
  const auto start = std::max(now, opportunity);
  const auto end = start + Nanoseconds(frame_bits(u32(operation, 8), operation.subspan(16))) * bit_time();
  require(end <= max_time, "frame end beyond the supported time range");
  transfers_.push_back({Bytes(operation.begin(), operation.end()), sender, start, end});
}

void Bus::complete(Nanoseconds now) {
  require(!transfers_.empty() && transfers_.front().end == now,
          "no frame ends at this instant");
  const auto& done = transfers_.front();
  const auto& frame = done.operation;
  outputs_[1 - done.sender] = frame;
  outputs_[done.sender] = {0x20, 0, 0, 0, 12, 0, 0, 0, frame[8], frame[9], frame[10], frame[11]};
  idle_from_ = now + intermission_bits * bit_time();
  transfers_.erase(transfers_.begin());
}

std::optional<Nanoseconds> Bus::next_completion() const {
  if (transfers_.empty()) return std::nullopt;
  return transfers_.front().end;
}
}  // namespace can
