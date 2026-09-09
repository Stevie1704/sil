#include "copy_counters.hpp"

#ifdef SIL_COPY_COUNTERS

#include <sys/resource.h>

#include <cstdio>
#include <cstdlib>

namespace sil::counters {

namespace {

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

void write_site(std::FILE *out, const char *name, const Site &site,
                const char *tail) {
  std::fprintf(out, "  \"%s\": {\"count\": %llu, \"bytes\": %llu}%s\n", name,
               (unsigned long long)site.count, (unsigned long long)site.bytes,
               tail);
}

}  // namespace

Counters::~Counters() {
  const char *path = std::getenv("SIL_COPY_COUNTERS_OUT");
  if (!path) return;
  std::FILE *out = std::fopen(path, "w");
  if (!out) return;

  std::fprintf(out, "{\n");
  write_site(out, "caller_to_kernel", caller_to_kernel, ",");
  write_site(out, "subscriber_copy", subscriber_copy, ",");
  write_site(out, "recorded", recorded, ",");
  write_site(out, "arena_write", arena_write, ",");
  write_site(out, "arena_read", arena_read, ",");
  write_site(out, "inline_encode", inline_encode, ",");
  write_site(out, "inline_decode", inline_decode, ",");

  // The kernel's own resource use, separate from the participant processes it
  // spawns: RUSAGE_CHILDREN in the driver cannot tell the two apart.
  rusage usage;
  getrusage(RUSAGE_SELF, &usage);
  std::fprintf(out,
               "  \"kernel_user_s\": %.6f,\n"
               "  \"kernel_system_s\": %.6f,\n"
               "  \"kernel_max_rss_bytes\": %llu\n}\n",
               seconds(usage.ru_utime), seconds(usage.ru_stime),
               (unsigned long long)max_rss_bytes(usage));
  std::fclose(out);
}

Counters &counters() {
  static Counters instance;
  return instance;
}

}  // namespace sil::counters

#endif
