/* Shared parts of the profiling-proof workload (issue #314).
 *
 * Every workload binary has hot functions with known names and allocation
 * sites with known counts. It also writes one interval line per hot call to a
 * sidecar file, so the proof can join profiler samples to the host intervals.
 *
 * Sidecar line (tab-separated):
 *   role  index  function  begin_ns  end_ns  pid  tid
 * begin_ns and end_ns are host CLOCK_MONOTONIC, read with the raw syscall.
 * The raw syscall goes past the Clock shim, which serves only the libc entry
 * points. So a shimmed process writes host time, not Virtual time.
 *
 * The sidecar directory comes from SILPROOF_SIDECAR_DIR. The proof sets it in
 * the runner environment, not in the Manifest. Without it, nothing is
 * written. Writing uses a stack buffer and write(2), so the sidecar adds no
 * heap allocation to the counts that the proof checks.
 */
#ifndef SILPROOF_H
#define SILPROOF_H

#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#if defined(__linux__)
#include <sys/syscall.h>
#elif defined(__APPLE__)
#include <mach/mach_time.h>
#endif

/* Keep each hot function a separate, unmerged, uninlined symbol. GCC's
 * identical-code folding and cloning would otherwise rename or merge it. */
#if defined(__clang__)
#define SILPROOF_HOT __attribute__((noinline, used))
#else
#define SILPROOF_HOT __attribute__((noipa, used))
#endif

/* Host monotonic time that the Clock shim does not virtualize. */
static inline uint64_t silproof_host_ns(void) {
#if defined(__linux__)
  struct timespec ts;
  syscall(SYS_clock_gettime, CLOCK_MONOTONIC, &ts);
  return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
#else
  /* Only for a local smoke run: the shim interposes every clock_gettime ID
   * that measures elapsed time on macOS. */
  mach_timebase_info_data_t base;
  mach_timebase_info(&base);
  return mach_absolute_time() * base.numer / base.denom;
#endif
}

static inline long silproof_tid(void) {
#if defined(__linux__)
  return (long)syscall(SYS_gettid);
#else
  return (long)getpid();
#endif
}

/* Appends one interval line to <dir>/<role>-<pid>.tsv. */
static inline void silproof_interval(const char *role, uint64_t index,
                                     const char *function, uint64_t begin_ns,
                                     uint64_t end_ns) {
  const char *dir = getenv("SILPROOF_SIDECAR_DIR");
  if (!dir || !*dir) return;
  char path[4096];
  int n = snprintf(path, sizeof path, "%s/%s-%ld.tsv", dir, role,
                   (long)getpid());
  if (n < 0 || (size_t)n >= sizeof path) return;
  int fd = open(path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
  if (fd < 0) return;
  char line[512];
  n = snprintf(line, sizeof line, "%s\t%llu\t%s\t%llu\t%llu\t%ld\t%ld\n", role,
               (unsigned long long)index, function,
               (unsigned long long)begin_ns, (unsigned long long)end_ns,
               (long)getpid(), silproof_tid());
  if (n > 0 && (size_t)n < sizeof line) (void)!write(fd, line, (size_t)n);
  close(fd);
}

/* A deterministic busy loop: the same iterations give the same result. */
#define SILPROOF_SPIN_BODY(seed, iterations, k1, k2)                 \
  do {                                                               \
    uint64_t x_ = (seed) | 1u;                                       \
    for (uint64_t i_ = 0; i_ < (iterations); i_++) {                 \
      x_ ^= x_ << 13;                                                \
      x_ ^= x_ >> 7;                                                 \
      x_ ^= x_ << 17;                                                \
      x_ += (k1) + (i_ & (k2));                                      \
    }                                                                \
    return x_;                                                       \
  } while (0)

/* Keeps a heap block alive to the optimizer: GCC may remove an unused
 * malloc/free pair. */
static inline void silproof_touch(void *block, size_t size) {
  memset(block, 0xA5, size);
  __asm__ volatile("" : : "r"(block) : "memory");
}

#endif /* SILPROOF_H */
