#pragma once

#include <array>
#include <cstdint>
#include <optional>
#include <span>
#include <stdexcept>
#include <vector>

namespace can {
using Bytes = std::vector<std::uint8_t>;
// Bus time in whole nanoseconds from the start of the Run.
using Nanoseconds = std::int64_t;

inline constexpr Nanoseconds ns_per_s = 1000000000;
// Supported rates divide one second into whole-nanosecond bit times.
inline constexpr std::uint32_t min_bitrate = 10000, max_bitrate = 1000000;
inline constexpr unsigned intermission_bits = 3;
// Beyond 2^50 ns (about 13 days) an FMI Float64 time no longer round-trips to
// the nanosecond, so later instants are rejected rather than rounded.
inline constexpr Nanoseconds max_time = Nanoseconds(1) << 50;

// SOF through the last EOF bit of an 11-bit data frame, with exact stuffing.
unsigned frame_bits(std::uint32_t id, std::span<const std::uint8_t> data);

// No FMI or SiL dependencies: the adapter supplies logical event instants.
class Bus {
 public:
  void receive(unsigned terminal, std::span<const std::uint8_t> operations, Nanoseconds now);
  // Delivers the frame whose last EOF bit ends at `now`.
  void complete(Nanoseconds now);
  std::optional<Nanoseconds> next_completion() const;
  const std::array<Bytes, 2>& outputs() const { return outputs_; }
  void clear_outputs() { outputs_ = {}; }

 private:
  struct Transfer {
    Bytes operation;
    unsigned sender;
    Nanoseconds start, end;
  };
  void schedule(unsigned sender, std::span<const std::uint8_t> operation, Nanoseconds now);
  Nanoseconds bit_time() const { return ns_per_s / *bitrate_; }

  std::array<Bytes, 2> outputs_;
  // At most two: the frame on the wire and the one waiting for the next
  // arbitration opportunity. Starts and ends are fixed when scheduled.
  std::vector<Transfer> transfers_;
  std::optional<std::uint32_t> bitrate_;
  Nanoseconds idle_from_ = 0;  // end of the last intermission
};
}  // namespace can
