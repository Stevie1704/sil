#pragma once

// Routing test instrumentation for the copy baseline (#61) and bounded
// subscriber routes (#75).
//
// Every function here compiles to nothing unless SIL_COPY_COUNTERS is defined,
// so the production runner carries no counter, no branch, and no atomic. The
// separate `sil-run-instrumented` target defines it and writes one JSON object
// to the path in SIL_COPY_COUNTERS_OUT at exit.
//
// The report states copy counts rather than inferring them from timing and also
// carries each route's last live depth plus high-water/drop/failure state. Per
// the decision on #63 this is the only counter surface there is: no external
// metrics surface exists, and none of these names carries a stability promise.
//
// Its three top-level members say how each value may be read (#82):
//   run_exit_code   the Run's own exit code, so a partial report from a failed
//                   Run cannot be read as a clean one.
//   deterministic   the counters and route state, which two Runs of the same
//                   Manifest repeat exactly. A repeated-run test compares this
//                   subtree wholesale.
//   observational   the kernel's wall-clock and RSS use, which varies per Run
//                   and is therefore never asserted on.

#include <cstddef>
#include <string>

namespace sil::counters {

enum class Site {
  kCallerToKernel,  // publisher's bytes into the kernel's Message
  kSubscriberCopy,  // that Message duplicated per subscriber route
  kRecorded,        // payload bytes handed to the Recording sink
  kArenaWrite,      // kernel -> Arena, shared-memory Transport
  kArenaRead,       // Arena -> kernel, shared-memory Transport
  kInlineEncode,    // payload -> base64 on the step line
  kInlineDecode,    // base64 -> payload off the step line
  kSiteCount,
};

#ifdef SIL_COPY_COUNTERS
// Called once with `main`'s return value, before the report is written.
void record_exit_code(int code) noexcept;
void count(Site site, size_t len);
void route_created(const std::string &channel,
                   const std::string &subscriber) noexcept;
void route_depth(const std::string &channel, const std::string &subscriber,
                 size_t depth) noexcept;
void route_dropped_newest(const std::string &channel,
                          const std::string &subscriber) noexcept;
void route_overflow_failure(const std::string &channel,
                            const std::string &subscriber) noexcept;
#else
inline void record_exit_code(int) noexcept {}
inline void count(Site, size_t) {}
inline void route_created(const std::string &,
                          const std::string &) noexcept {}
inline void route_depth(const std::string &, const std::string &,
                        size_t) noexcept {}
inline void route_dropped_newest(const std::string &,
                                 const std::string &) noexcept {}
inline void route_overflow_failure(const std::string &,
                                   const std::string &) noexcept {}
#endif

}  // namespace sil::counters
