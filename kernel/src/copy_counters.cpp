#include "copy_counters.hpp"

#ifdef SIL_COPY_COUNTERS

#include <sys/resource.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>

namespace sil::counters {

namespace {

// One name per Site, in Site order: the enum is the single list of sites, so a
// new one cannot be counted without also being reported.
const char *const kSiteNames[] = {
    "caller_to_kernel", "subscriber_copy", "recorded", "arena_write",
    "arena_read",       "inline_encode",   "inline_decode",
};
static_assert(sizeof kSiteNames / sizeof *kSiteNames ==
              size_t(Site::kSiteCount));

// ru_maxrss is bytes on macOS and kilobytes on Linux; the report states bytes.
uint64_t max_rss_bytes(const rusage &usage) {
#ifdef __APPLE__
  return uint64_t(usage.ru_maxrss);
#else
  return uint64_t(usage.ru_maxrss) * 1024;
#endif
}

double seconds(const timeval &t) {
  return double(t.tv_sec) + double(t.tv_usec) / 1e6;
}

struct Totals {
  uint64_t count[size_t(Site::kSiteCount)] = {};
  uint64_t bytes[size_t(Site::kSiteCount)] = {};

  ~Totals();
};

// The report is written from a static destructor, after main has returned and
// after every other kernel object is gone. stdio and getrusage are all this
// needs; pulling the JSON library in here would mean allocating and unwinding
// at a point where nothing is left to report a failure to.
Totals::~Totals() {
  const char *path = std::getenv("SIL_COPY_COUNTERS_OUT");
  if (!path) return;
  std::FILE *out = std::fopen(path, "w");
  if (!out) {
    // An unreported baseline that looks like a clean run is worse than a noisy
    // one: say so on stderr, where the driver's failure output already goes.
    std::perror("sil-run-instrumented: cannot write copy counters");
    return;
  }

  std::fprintf(out, "{\n");
  for (size_t i = 0; i < size_t(Site::kSiteCount); i++)
    std::fprintf(out, "  \"%s\": {\"count\": %llu, \"bytes\": %llu},\n",
                 kSiteNames[i], (unsigned long long)count[i],
                 (unsigned long long)bytes[i]);

  // The kernel's own resource use, separate from the participant processes it
  // spawns: RUSAGE_CHILDREN in the driver cannot tell the two apart.
  rusage usage;
  if (getrusage(RUSAGE_SELF, &usage) != 0) usage = rusage{};
  std::fprintf(out,
               "  \"kernel_user_s\": %.6f,\n"
               "  \"kernel_system_s\": %.6f,\n"
               "  \"kernel_max_rss_bytes\": %llu\n}\n",
               seconds(usage.ru_utime), seconds(usage.ru_stime),
               (unsigned long long)max_rss_bytes(usage));
  if (std::fclose(out) != 0)
    std::perror("sil-run-instrumented: cannot close copy counters");
}

Totals &totals() {
  static Totals instance;
  return instance;
}

}  // namespace

void count(Site site, size_t len) {
  Totals &t = totals();
  t.count[size_t(site)]++;
  t.bytes[size_t(site)] += len;
}

}  // namespace sil::counters

#endif
