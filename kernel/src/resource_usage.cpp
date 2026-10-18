#include "resource_usage.hpp"

namespace sil::diagnostics {

namespace {

uint64_t nanoseconds(const timeval &t) {
  return uint64_t(t.tv_sec) * 1000000000ULL + uint64_t(t.tv_usec) * 1000ULL;
}

}  // namespace

ResourceUsage from_rusage(const rusage &usage, MaxRssUnit unit) {
  ResourceUsage result;
  result.cpu = {nanoseconds(usage.ru_utime), nanoseconds(usage.ru_stime)};
  if (usage.ru_maxrss > 0)
    result.peak_rss_bytes =
        uint64_t(usage.ru_maxrss) * (unit == MaxRssUnit::Kibibytes ? 1024 : 1);
  return result;
}

std::optional<CpuTime> disjoint_cpu_total(
    const std::vector<std::optional<CpuTime>> &scopes) {
  CpuTime total;
  for (const std::optional<CpuTime> &scope : scopes) {
    if (!scope) return std::nullopt;
    total.user_ns += scope->user_ns;
    total.system_ns += scope->system_ns;
  }
  return total;
}

}  // namespace sil::diagnostics
