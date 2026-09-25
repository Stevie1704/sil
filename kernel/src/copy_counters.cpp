#include "copy_counters.hpp"

#ifdef SIL_COPY_COUNTERS

#include <sys/resource.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <map>
#include <string>

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
  struct RouteKey {
    std::string channel;
    std::string subscriber;

    bool operator<(const RouteKey &other) const {
      if (channel != other.channel) return channel < other.channel;
      return subscriber < other.subscriber;
    }
  };

  struct Route {
    uint64_t current_depth = 0;
    uint64_t high_water_depth = 0;
    uint64_t dropped_newest = 0;
    uint64_t overflow_failures = 0;
  };

  uint64_t count[size_t(Site::kSiteCount)] = {};
  uint64_t bytes[size_t(Site::kSiteCount)] = {};
  std::map<RouteKey, Route> routes;
  uint64_t replay_read_buffer_high_water = 0;
  int exit_code = 0;

  ~Totals();
};

void write_json_string(std::FILE *out, const std::string &value) {
  std::fputc('"', out);
  for (const unsigned char ch : value) {
    switch (ch) {
      case '"': std::fputs("\\\"", out); break;
      case '\\': std::fputs("\\\\", out); break;
      case '\b': std::fputs("\\b", out); break;
      case '\f': std::fputs("\\f", out); break;
      case '\n': std::fputs("\\n", out); break;
      case '\r': std::fputs("\\r", out); break;
      case '\t': std::fputs("\\t", out); break;
      default:
        if (ch < 0x20)
          std::fprintf(out, "\\u%04x", static_cast<unsigned>(ch));
        else
          std::fputc(ch, out);
    }
  }
  std::fputc('"', out);
}

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

  // The Run's exit code first: a reader that stops there still knows whether
  // the counters below describe a complete Run or a failed one.
  std::fprintf(out, "{\n  \"run_exit_code\": %d,\n", exit_code);

  std::fprintf(out, "  \"deterministic\": {\n");
  for (size_t i = 0; i < size_t(Site::kSiteCount); i++)
    std::fprintf(out, "    \"%s\": {\"count\": %llu, \"bytes\": %llu},\n",
                 kSiteNames[i], (unsigned long long)count[i],
                 (unsigned long long)bytes[i]);

  std::fprintf(out, "    \"routes\": [");
  bool first = true;
  for (const auto &[key, route] : routes) {
    if (!first) std::fputc(',', out);
    first = false;
    std::fputs("\n      {\"channel\": ", out);
    write_json_string(out, key.channel);
    std::fputs(", \"subscriber\": ", out);
    write_json_string(out, key.subscriber);
    std::fprintf(out,
                 ", \"current_depth\": %llu, \"high_water_depth\": %llu, "
                 "\"dropped_newest\": %llu, \"overflow_failures\": %llu}",
                 (unsigned long long)route.current_depth,
                 (unsigned long long)route.high_water_depth,
                 (unsigned long long)route.dropped_newest,
                 (unsigned long long)route.overflow_failures);
  }
  if (!routes.empty()) std::fputs("\n    ", out);
  std::fprintf(out, "],\n");
  std::fprintf(out,
               "    \"replay_read_buffer\": {\"high_water_bytes\": %llu}\n"
               "  },\n",
               (unsigned long long)replay_read_buffer_high_water);

  // The kernel's own resource use, separate from the participant processes it
  // spawns: RUSAGE_CHILDREN in the driver cannot tell the two apart. It varies
  // between two Runs of the same Manifest, so it is reported apart from the
  // counters that do not.
  rusage usage;
  if (getrusage(RUSAGE_SELF, &usage) != 0) usage = rusage{};
  std::fprintf(out,
               "  \"observational\": {\n"
               "    \"kernel_user_s\": %.6f,\n"
               "    \"kernel_system_s\": %.6f,\n"
               "    \"kernel_max_rss_bytes\": %llu\n  }\n}\n",
               seconds(usage.ru_utime), seconds(usage.ru_stime),
               (unsigned long long)max_rss_bytes(usage));
  if (std::fclose(out) != 0)
    std::perror("sil-run-instrumented: cannot close copy counters");
}

Totals &totals() {
  static Totals instance;
  return instance;
}

template <typename Update>
void update_route(const std::string &channel, const std::string &subscriber,
                  Update update) noexcept {
  try {
    update(totals().routes[{channel, subscriber}]);
  } catch (...) {
    // Instrumentation must never replace or abort the Run it observes.
  }
}

}  // namespace

void record_exit_code(int code) noexcept {
  try {
    // Also the point where the report's own object is created for a Run that
    // counted nothing, so a Manifest error still produces a report that says so.
    totals().exit_code = code;
  } catch (...) {
    // Instrumentation must never replace or abort the Run it observes.
  }
}

void count(Site site, size_t len) {
  Totals &t = totals();
  t.count[size_t(site)]++;
  t.bytes[size_t(site)] += len;
}

void route_created(const std::string &channel,
                   const std::string &subscriber) noexcept {
  update_route(channel, subscriber, [](Totals::Route &) {});
}

void route_depth(const std::string &channel, const std::string &subscriber,
                 size_t depth) noexcept {
  update_route(channel, subscriber, [depth](Totals::Route &route) {
    route.current_depth = depth;
    if (depth > route.high_water_depth) route.high_water_depth = depth;
  });
}

void route_dropped_newest(const std::string &channel,
                          const std::string &subscriber) noexcept {
  update_route(channel, subscriber,
               [](Totals::Route &route) { route.dropped_newest++; });
}

void route_overflow_failure(const std::string &channel,
                            const std::string &subscriber) noexcept {
  update_route(channel, subscriber,
               [](Totals::Route &route) { route.overflow_failures++; });
}

void replay_read_buffer(size_t bytes) noexcept {
  try {
    uint64_t &high = totals().replay_read_buffer_high_water;
    if (bytes > high) high = bytes;
  } catch (...) {
    // Instrumentation must never replace or abort the Run it observes.
  }
}

}  // namespace sil::counters

#endif
