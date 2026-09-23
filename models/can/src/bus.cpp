#include "bus.hpp"

#include <algorithm>
#include <limits>
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
inline constexpr std::uint8_t bus_error_code = 0x31;
inline constexpr std::uint8_t bit_error = 0x01;
inline constexpr std::uint8_t primary_error_flag = 0x01;
inline constexpr std::uint8_t secondary_error_flag = 0x02;
Bytes identifier_operation(std::uint8_t code, std::uint32_t id) {
  return {code, 0, 0, 0, 12, 0, 0, 0,
          std::uint8_t(id), std::uint8_t(id >> 8), 0, 0};
}
Bytes bus_error_operation(std::uint32_t id, bool primary, bool sender) {
  return {bus_error_code, 0, 0, 0, 15, 0, 0, 0,
          std::uint8_t(id), std::uint8_t(id >> 8), 0, 0,
          bit_error, primary ? primary_error_flag : secondary_error_flag,
          std::uint8_t(sender)};
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

void Bus::validate_configuration_limits(
    unsigned active_nodes, unsigned queue_capacity,
    std::uint64_t retry_limit, std::uint64_t fault_rule_count) {
  require(active_nodes >= 1 && active_nodes <= terminal_capacity,
          "active node count outside declared terminal capacity");
  require(queue_capacity >= 1 && queue_capacity <= max_queue_capacity,
          "per-node queue capacity must be 1..64");
  require(retry_limit <= max_fault_retries,
          "automatic CAN retransmission limit must be 0..4");
  require(fault_rule_count <= max_fault_rules,
          "fault schedule exceeds its finite rule capacity");
}

void Bus::configure(unsigned active_nodes, unsigned queue_capacity,
                    std::uint64_t retry_limit, std::uint64_t fault_rule_count,
                    std::span<const FaultRuleInput> fault_rules) {
  validate_configuration_limits(active_nodes, queue_capacity, retry_limit,
                                fault_rule_count);
  require(fault_rules.size() <= max_fault_rules &&
              fault_rules.size() >= fault_rule_count,
          "fault schedule parameters do not match the declared rule count");
  require(!has_pending() && !on_wire_, "cannot reconfigure a busy bus");
  for (const auto& notifications : notifications_)
    require(notifications.empty(), "cannot reconfigure pending notifications");

  std::array<FaultRule, max_fault_rules> configured_rules{};
  for (std::size_t index = 0; index < fault_rules.size(); ++index) {
    const auto& input = fault_rules[index];
    if (index >= fault_rule_count) {
      require(input.kind == 0 && input.sender_node == 0 &&
                  input.receiver_node == 0 && input.identifier == 0 &&
                  input.first_request == 0 && input.last_request == 0 &&
                  input.occurrence == 0 && input.attempt == 0,
              "fault schedule contains fields beyond its declared rule count");
      continue;
    }

    require(input.kind == std::uint64_t(FaultKind::transmission_error) ||
                input.kind == std::uint64_t(FaultKind::receiver_delivery_suppression),
            "fault rule kind must be 1 (transmission error) or 2 (receiver delivery suppression)");
    require(input.sender_node >= 1 && input.sender_node <= active_nodes,
            "fault rule sender must name an active one-based terminal");
    require(input.identifier <= max_classical_identifier,
            "fault rule identifier is outside Classical CAN");
    require(input.first_request <= std::uint64_t(max_time) &&
                input.last_request <= std::uint64_t(max_time) &&
                input.first_request <= input.last_request,
            "fault rule request-time window must be an inclusive range within the supported time");
    require(input.occurrence > 0,
            "fault rule occurrence is a one-based positive index");
    require(input.attempt > 0 && input.attempt <= retry_limit + 1,
            "fault rule attempt exceeds the configured retry bound");

    auto& rule = configured_rules[index];
    rule.kind = static_cast<FaultKind>(input.kind);
    rule.sender = unsigned(input.sender_node - 1);
    rule.id = std::uint32_t(input.identifier);
    rule.first_request = Nanoseconds(input.first_request);
    rule.last_request = Nanoseconds(input.last_request);
    rule.occurrence = input.occurrence;
    rule.attempt = unsigned(input.attempt);
    if (rule.kind == FaultKind::transmission_error) {
      require(input.receiver_node == 0,
              "transmission-error rule receiver must be zero");
      rule.receiver = terminal_capacity;
    } else {
      require(input.receiver_node >= 1 && input.receiver_node <= active_nodes &&
                  input.receiver_node != input.sender_node,
              "delivery-suppression receiver must be active and differ from its sender");
      rule.receiver = unsigned(input.receiver_node - 1);
    }
  }
  active_nodes_ = active_nodes;
  queue_capacity_ = queue_capacity;
  retry_limit_ = unsigned(retry_limit);
  fault_rule_count_ = unsigned(fault_rule_count);
  fault_rules_ = configured_rules;
  fault_consumed_ = {};
  rule_occurrences_ = {};
}

bool Bus::has_pending() const {
  return std::any_of(queues_.begin(), queues_.end(),
                     [](const auto& queue) { return !queue.empty(); }) ||
         std::any_of(retries_.begin(), retries_.end(),
                     [](const auto& retry) { return retry.has_value(); });
}

const Bus::Request& Bus::head(unsigned node) const {
  if (retries_[node]) return *retries_[node];
  return queues_[node].front();
}

Bus::Request Bus::take_head(unsigned node) {
  if (retries_[node]) {
    auto request = std::move(*retries_[node]);
    retries_[node].reset();
    return request;
  }
  auto request = std::move(queues_[node].front());
  queues_[node].pop_front();
  return request;
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
        require(u32(op, 8) <= max_classical_identifier && op[12] == 0 && op[13] == 0,
                "only 11-bit Classical CAN data frames are supported");
        const auto id = u32(op, 8);
        std::uint64_t occurrence = 0;
        for (unsigned index = 0; index < next.fault_rule_count_; ++index) {
          const auto& rule = next.fault_rules_[index];
          if (rule.sender != terminal || rule.id != id) continue;
          auto& count = next.rule_occurrences_[index];
          require(count < std::numeric_limits<std::uint64_t>::max(),
                  "CAN request occurrence counter exhausted");
          occurrence = ++count;
        }
        requests.emplace_back(terminal, Request{
            Bytes(op.begin(), op.end()), id, now, occurrence, 1});
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
    if ((retries_[node] || !queues_[node].empty()) &&
        (winner == terminal_capacity || head(node).id < head(winner).id))
      winner = node;
  require(winner < terminal_capacity, "arbitration without pending frames");
  const auto chosen = head(winner);
  Transfer transfer{chosen.operation, {}, false, terminal_capacity, std::nullopt,
                    now + Nanoseconds(frame_bits(chosen.id,
                        std::span<const std::uint8_t>(chosen.operation).subspan(16))) * bit_time()};
  require(transfer.end <= max_time, "frame end beyond the supported time range");
  for (unsigned node = 0; node < active_nodes_; ++node) {
    if (!retries_[node] && queues_[node].empty()) continue;
    const auto contender = head(node);
    if (contender.id == chosen.id) {
      require(contender.operation == chosen.operation,
              "equal CAN identifiers with different payloads cannot be resolved without error modeling");
      transfer.requests[node] = take_head(node);
    } else if (discards_on_loss_[node]) {
      // The standard ArbitrationLost operation contains the lost identifier.
      auto& out = notifications_[node];
      const auto notice = identifier_operation(arbitration_lost_code, contender.id);
      out.insert(out.end(), notice.begin(), notice.end());
      static_cast<void>(take_head(node));
    }
  }

  // The schedule order is the precedence order. At most one rule applies to
  // a physical transmission attempt, even when identical frames co-transmit.
  for (unsigned rule_index = 0; rule_index < fault_rule_count_; ++rule_index) {
    if (fault_consumed_[rule_index]) continue;
    const auto& rule = fault_rules_[rule_index];
    for (unsigned node = 0; node < active_nodes_; ++node) {
      if (!transfer.requests[node]) continue;
      const auto& request = *transfer.requests[node];
      if (node != rule.sender || request.id != rule.id ||
          request.requested_at < rule.first_request ||
          request.requested_at > rule.last_request ||
          request.occurrence != rule.occurrence || request.attempt != rule.attempt)
        continue;
      fault_consumed_[rule_index] = true;
      if (rule.kind == FaultKind::transmission_error) {
        transfer.transmission_error = true;
        transfer.primary_error_node = node;
      } else {
        transfer.suppressed_receiver = rule.receiver;
      }
      break;
    }
    if (transfer.transmission_error || transfer.suppressed_receiver) break;
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
    if (done.transmission_error) {
      // FMI-LS-BUS requires PRIMARY_ERROR_FLAG and Is Sender to be unique per
      // simulated error, even when identical requests co-transmit.
      const auto error = bus_error_operation(
          u32(frame, 8), node == done.primary_error_node,
          node == done.primary_error_node);
      outputs_[node].insert(outputs_[node].end(), error.begin(), error.end());
    } else if (done.requests[node]) {
      const auto confirmation = identifier_operation(confirm_code, u32(frame, 8));
      outputs_[node].insert(outputs_[node].end(), confirmation.begin(), confirmation.end());
    } else if (!done.suppressed_receiver || *done.suppressed_receiver != node) {
      outputs_[node].insert(outputs_[node].end(), frame.begin(), frame.end());
    }
  }
  if (done.transmission_error) {
    for (unsigned node = 0; node < active_nodes_; ++node) {
      if (!done.requests[node]) continue;
      auto retry = *done.requests[node];
      if (retry.attempt <= retry_limit_) {
        ++retry.attempt;
        require(!retries_[node], "automatic CAN retry slot is already occupied");
        retries_[node] = std::move(retry);
      }
    }
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
