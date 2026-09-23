#pragma once

#include <array>
#include <cstdint>
#include <deque>
#include <optional>
#include <span>
#include <vector>

namespace can {
using Bytes = std::vector<std::uint8_t>;
// Bus time in whole nanoseconds from the start of the Run.
using Nanoseconds = std::int64_t;
inline constexpr unsigned terminal_capacity = 4;
inline constexpr unsigned max_queue_capacity = 64;
inline constexpr unsigned max_fault_rules = 8;
inline constexpr unsigned max_fault_retries = 4;
inline constexpr std::uint32_t max_classical_identifier = 0x7ff;
// The operation buffers each terminal handed the bus in one event.
using Inputs = std::array<std::span<const std::uint8_t>, terminal_capacity>;

inline constexpr Nanoseconds ns_per_s = 1000000000;
// Supported rates divide one second into whole-nanosecond bit times.
inline constexpr std::uint32_t min_bitrate = 10000, max_bitrate = 1000000;
inline constexpr unsigned intermission_bits = 3;
// Beyond 2^50 ns (about 13 days) an FMI Float64 time no longer round-trips to
// the nanosecond, so later instants are rejected rather than rounded.
inline constexpr Nanoseconds max_time = Nanoseconds(1) << 50;

unsigned frame_bits(std::uint32_t id, std::span<const std::uint8_t> data);

enum class FaultKind : std::uint8_t {
  transmission_error = 1,
  receiver_delivery_suppression = 2,
};

struct FaultRule {
  FaultKind kind = FaultKind::transmission_error;
  unsigned sender = 0;
  unsigned receiver = terminal_capacity;
  std::uint32_t id = 0;
  Nanoseconds first_request = 0;
  Nanoseconds last_request = 0;
  std::uint64_t occurrence = 0;
  unsigned attempt = 0;
};

// FMI-facing values stay one-based and wide until the model validates them.
// This keeps range checks and normalization in the CAN core.
struct FaultRuleInput {
  std::uint64_t kind = 0;
  std::uint64_t sender_node = 0;
  std::uint64_t receiver_node = 0;
  std::uint64_t identifier = 0;
  std::uint64_t first_request = 0;
  std::uint64_t last_request = 0;
  std::uint64_t occurrence = 0;
  std::uint64_t attempt = 0;
};

// The core owns CAN semantics; the adapter supplies complete FMI event inputs.
class Bus {
 public:
  void configure(unsigned active_nodes, unsigned queue_capacity,
                 std::uint64_t retry_limit = 1,
                 std::uint64_t fault_rule_count = 0,
                 std::span<const FaultRuleInput> fault_rules = {});
  static void validate_configuration_limits(
      unsigned active_nodes, unsigned queue_capacity,
      std::uint64_t retry_limit, std::uint64_t fault_rule_count);
  unsigned active_nodes() const { return active_nodes_; }
  unsigned queue_capacity() const { return queue_capacity_; }
  void receive(const Inputs& inputs, Nanoseconds now);
  // A countdown can mean a frame end or the first instant after intermission.
  // For the latter, call receive at that instant before selecting a winner.
  void complete(Nanoseconds now);
  bool is_completion(Nanoseconds now) const { return on_wire_ && on_wire_->end == now; }
  std::optional<Nanoseconds> next_event() const;
  const std::array<Bytes, terminal_capacity>& outputs() const { return outputs_; }
  void clear_outputs() { outputs_ = {}; }

 private:
  struct Request {
    Bytes operation;
    std::uint32_t id;
    Nanoseconds requested_at;
    std::uint64_t occurrence;
    unsigned attempt = 1;
  };
  struct Transfer {
    Bytes operation;
    std::array<std::optional<Request>, terminal_capacity> requests{};
    bool transmission_error = false;
    unsigned primary_error_node = terminal_capacity;
    std::optional<unsigned> suppressed_receiver;
    Nanoseconds end;
  };
  void enqueue(unsigned sender, const Request& request);
  void arbitrate(Nanoseconds now);
  bool has_pending() const;
  const Request& head(unsigned node) const;
  Request take_head(unsigned node);
  Nanoseconds bit_time() const { return ns_per_s / *bitrate_; }

  unsigned active_nodes_ = 2, queue_capacity_ = 4, retry_limit_ = 1;
  std::array<Bytes, terminal_capacity> outputs_;
  std::array<Bytes, terminal_capacity> notifications_;
  std::array<std::deque<Request>, terminal_capacity> queues_;
  // The automatic retransmission slot is separate from the finite FIFO: a
  // frame on the wire and its one reserved retry do not consume that FIFO.
  std::array<std::optional<Request>, terminal_capacity> retries_;
  // Only identifiers named by the finite schedule need occurrence counters.
  // One counter per rule keeps the transactional Bus copy small.
  std::array<std::uint64_t, max_fault_rules> rule_occurrences_{};
  std::optional<Transfer> on_wire_;
  std::array<FaultRule, max_fault_rules> fault_rules_{};
  std::array<bool, max_fault_rules> fault_consumed_{};
  unsigned fault_rule_count_ = 0;
  std::optional<std::uint32_t> bitrate_;
  std::array<bool, terminal_capacity> configured_{};
  std::array<bool, terminal_capacity> discards_on_loss_{};
  // First possible SOF after the previous frame's intermission.
  Nanoseconds idle_from_ = 0;
};
}  // namespace can
