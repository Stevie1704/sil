#pragma once

// Payload-copy instrumentation for the routing baseline (issue #61).
//
// Every function here compiles to nothing unless SIL_COPY_COUNTERS is defined,
// so the production runner carries no counter, no branch, and no atomic. The
// separate `sil-run-instrumented` target defines it and writes one JSON object
// to the path in SIL_COPY_COUNTERS_OUT at exit.
//
// The point is to state copy counts rather than infer them from timing. Each
// Site names one place where payload bytes are duplicated, and the run reports
// how many times each was reached and how many bytes crossed it.

#include <cstddef>

namespace sil::counters {

enum class Site {
  kCallerToKernel,  // publisher's bytes into the kernel's Message
  kSubscriberCopy,  // that Message duplicated per subscriber queue
  kRecorded,        // payload bytes handed to the Recording sink
  kArenaWrite,      // kernel -> Arena, shared-memory Transport
  kArenaRead,       // Arena -> kernel, shared-memory Transport
  kInlineEncode,    // payload -> base64 on the step line
  kInlineDecode,    // base64 -> payload off the step line
  kSiteCount,
};

#ifdef SIL_COPY_COUNTERS
void count(Site site, size_t len);
#else
inline void count(Site, size_t) {}
#endif

}  // namespace sil::counters
