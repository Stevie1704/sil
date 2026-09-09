#pragma once

// Payload-copy instrumentation for the routing baseline (issue #61).
//
// Every function here compiles to nothing unless SIL_COPY_COUNTERS is defined,
// so the production runner carries no counter, no branch, and no atomic. The
// separate `sil-run-instrumented` target defines it and writes one JSON object
// to the path in SIL_COPY_COUNTERS_OUT at exit.
//
// The point is to state copy counts rather than infer them from timing: each
// call site names one place where payload bytes are duplicated, and the run
// reports how many times each was reached and how many bytes crossed it.

#include <cstddef>
#include <cstdint>

namespace sil::counters {

#ifdef SIL_COPY_COUNTERS

struct Site {
  uint64_t count = 0;
  uint64_t bytes = 0;

  void hit(size_t len) {
    count++;
    bytes += len;
  }
};

struct Counters {
  Site caller_to_kernel;   // publisher's bytes into the kernel's message
  Site subscriber_copy;    // that message duplicated per subscriber queue
  Site recorded;           // payload bytes handed to the Recording sink
  Site arena_write;        // kernel -> Arena, shared-memory transport
  Site arena_read;         // Arena -> kernel, shared-memory transport
  Site inline_encode;      // payload -> base64 on the step line
  Site inline_decode;      // base64 -> payload off the step line

  ~Counters();
};

// One instance per process, destroyed after main so the dump sees the final
// totals. The reference is what every call site below touches.
Counters &counters();

inline void count_caller_copy(size_t len) { counters().caller_to_kernel.hit(len); }
inline void count_subscriber_copy(size_t len) { counters().subscriber_copy.hit(len); }
inline void count_recorded(size_t len) { counters().recorded.hit(len); }
inline void count_arena_write(size_t len) { counters().arena_write.hit(len); }
inline void count_arena_read(size_t len) { counters().arena_read.hit(len); }
inline void count_inline_encode(size_t len) { counters().inline_encode.hit(len); }
inline void count_inline_decode(size_t len) { counters().inline_decode.hit(len); }

#else

inline void count_caller_copy(size_t) {}
inline void count_subscriber_copy(size_t) {}
inline void count_recorded(size_t) {}
inline void count_arena_write(size_t) {}
inline void count_arena_read(size_t) {}
inline void count_inline_encode(size_t) {}
inline void count_inline_decode(size_t) {}

#endif

}  // namespace sil::counters
