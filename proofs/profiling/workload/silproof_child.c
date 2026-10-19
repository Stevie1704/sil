/* Short-lived child of the profiling proof's Process participant (#314).
 *
 *     silproof_child INDEX
 *
 * runs silproof_child_spin once and prints two decimal numbers on stdout:
 * its libc CLOCK_MONOTONIC reading and the spin result. A child of a shimmed
 * participant inherits the Clock shim, so its reading is the parent's Step
 * Virtual time. If a profiler removes the shim from the environment, the
 * reading changes, and so does the Recording.
 */
#include "silproof.h"

#define SILPROOF_CHILD_ITERATIONS 20000000ULL

SILPROOF_HOT static uint64_t silproof_child_spin(uint64_t seed,
                                                 uint64_t iterations) {
  SILPROOF_SPIN_BODY(seed, iterations, 0x1337ULL, 0x1Fu);
}

int main(int argc, char **argv) {
  if (argc != 2) {
    fprintf(stderr, "usage: silproof_child INDEX\n");
    return 2;
  }
  uint64_t index = strtoull(argv[1], NULL, 10);
  uint64_t begin = silproof_host_ns();
  uint64_t result = silproof_child_spin(index, SILPROOF_CHILD_ITERATIONS);
  uint64_t end = silproof_host_ns();
  silproof_interval("child", index, "silproof_child_spin", begin, end);
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  printf("%llu %llu\n",
         (unsigned long long)ts.tv_sec * 1000000000ULL +
             (unsigned long long)ts.tv_nsec,
         (unsigned long long)result);
  return 0;
}
