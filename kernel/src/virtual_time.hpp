#pragma once

#include <cstdint>
#include <optional>

namespace sil {

/**
 * The virtual time `delta_ns` after `from_ns`, or nothing when that instant
 * cannot be represented.
 *
 * Virtual time is unsigned and never runs backwards, so every addition on it
 * is a boundary the kernel has to answer for rather than wrap through. The
 * three sites that add to it read the empty result as their own end of the
 * line: a task whose next activation is unrepresentable is complete, a
 * message whose post-latency visibility is unrepresentable is never
 * delivered, and an interceptor delay saturates at UINT64_MAX so the plan's
 * run-duration truncation suppresses it.
 */
inline std::optional<uint64_t> virtual_time_after(uint64_t from_ns,
                                                    uint64_t delta_ns) {
  if (delta_ns > UINT64_MAX - from_ns) return std::nullopt;
  return from_ns + delta_ns;
}

}  // namespace sil
