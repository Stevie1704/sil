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

// The core owns CAN semantics; the adapter supplies complete FMI event inputs.
class Bus {
 public:
  void configure(unsigned active_nodes, unsigned queue_capacity);
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
  struct Request { Bytes operation; std::uint32_t id; };
  struct Transfer {
    Bytes operation;
    std::array<bool, terminal_capacity> senders{};
    Nanoseconds end;
  };
  void enqueue(unsigned sender, const Request& request);
  void arbitrate(Nanoseconds now);
  bool has_pending() const;
  Nanoseconds bit_time() const { return ns_per_s / *bitrate_; }

  unsigned active_nodes_ = 2, queue_capacity_ = 4;
  std::array<Bytes, terminal_capacity> outputs_;
  std::array<Bytes, terminal_capacity> notifications_;
  std::array<std::deque<Request>, terminal_capacity> queues_;
  std::optional<Transfer> on_wire_;
  std::optional<std::uint32_t> bitrate_;
  std::array<bool, terminal_capacity> configured_{};
  std::array<bool, terminal_capacity> discards_on_loss_{};
  // First possible SOF after the previous frame's intermission.
  Nanoseconds idle_from_ = 0;
};
}  // namespace can
