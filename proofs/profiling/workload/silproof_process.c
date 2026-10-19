/* Shared library of the profiling proof's Process participant (issue #314).
 *
 * process_adapter.py loads it with ctypes, so it is a library that the
 * participant loads at run time, not at exec. Each Step calls
 * silproof_process_step: it runs silproof_process_spin, then
 * silproof_process_alloc_site, which allocates and frees
 * SILPROOF_PROCESS_ALLOCS blocks of SILPROOF_PROCESS_BLOCK bytes. It also
 * reads CLOCK_MONOTONIC through libc. In a shimmed participant, that read is
 * the Step's Virtual time; the participant publishes it, so the Recording
 * shows whether the Clock shim stays active under a profiler.
 */
#include "silproof.h"

#define SILPROOF_PROCESS_ITERATIONS 3000000ULL
#define SILPROOF_PROCESS_ALLOCS 8
#define SILPROOF_PROCESS_BLOCK 2048

SILPROOF_HOT static uint64_t silproof_process_spin(uint64_t seed,
                                                   uint64_t iterations) {
  SILPROOF_SPIN_BODY(seed, iterations, 0x2545ULL, 0x7Fu);
}

SILPROOF_HOT static void silproof_process_alloc_site(void) {
  for (int i = 0; i < SILPROOF_PROCESS_ALLOCS; i++) {
    void *block = malloc(SILPROOF_PROCESS_BLOCK);
    if (!block) return;
    silproof_touch(block, SILPROOF_PROCESS_BLOCK);
    free(block);
  }
}

uint64_t silproof_process_step(uint64_t step, uint64_t *clock_now_ns) {
  uint64_t begin = silproof_host_ns();
  uint64_t result = silproof_process_spin(step, SILPROOF_PROCESS_ITERATIONS);
  uint64_t end = silproof_host_ns();
  silproof_interval("process", step, "silproof_process_spin", begin, end);
  silproof_process_alloc_site();
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  *clock_now_ns = (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
  return result;
}
