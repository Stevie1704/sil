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
// carries each route's last live depth plus high-water/drop/failure state. It
// is test instrumentation, not the external metrics surface deferred to #63.

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
