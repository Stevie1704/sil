// The diagnostics collector's resource conversions and CPU aggregation (#301),
// pinned with synthetic rusage values whose expected bytes and nanoseconds are
// worked out by hand.
#include "resource_usage.hpp"

#include <sys/resource.h>

#include <cstdint>
#include <iostream>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

using sil::diagnostics::CpuTime;
using sil::diagnostics::MaxRssUnit;
using sil::diagnostics::disjoint_cpu_total;
using sil::diagnostics::from_rusage;

void check(bool condition, const std::string &message) {
  if (!condition) throw std::runtime_error(message);
}

rusage synthetic(long user_s, long user_us, long system_s, long system_us,
                 long maxrss) {
  rusage usage{};
  usage.ru_utime.tv_sec = user_s;
  usage.ru_utime.tv_usec = int(user_us);
  usage.ru_stime.tv_sec = system_s;
  usage.ru_stime.tv_usec = int(system_us);
  usage.ru_maxrss = maxrss;
  return usage;
}

/** timeval microseconds become nanoseconds; Linux KiB become bytes. */
void test_linux_units() {
  const auto usage = from_rusage(synthetic(1, 250000, 0, 3, 2048),
                                 MaxRssUnit::Kibibytes);
  check(usage.cpu.user_ns == 1250000000, "user 1.25 s");
  check(usage.cpu.system_ns == 3000, "system 3 us");
  check(usage.peak_rss_bytes == std::optional<uint64_t>(2097152),
        "2048 KiB is 2 MiB");
}

/** macOS reports ru_maxrss in bytes already. */
void test_macos_units() {
  const auto usage =
      from_rusage(synthetic(0, 0, 0, 0, 9437184), MaxRssUnit::Bytes);
  check(usage.peak_rss_bytes == std::optional<uint64_t>(9437184),
        "bytes stay bytes");
  check(usage.cpu.user_ns == 0 && usage.cpu.system_ns == 0, "zero CPU");
}

/** An unreported high-water mark is missing, never zero. */
void test_unreported_peak_is_missing() {
  check(!from_rusage(synthetic(0, 1, 0, 1, 0), MaxRssUnit::Kibibytes)
             .peak_rss_bytes,
        "zero maxrss is not a measurement");
  check(!from_rusage(synthetic(0, 1, 0, 1, -1), MaxRssUnit::Bytes)
             .peak_rss_bytes,
        "negative maxrss is not a measurement");
}

/** Disjoint scopes add up; one missing scope makes the total missing. */
void test_disjoint_cpu_total() {
  const std::vector<std::optional<CpuTime>> scopes = {
      CpuTime{41000000, 12000000}, CpuTime{9000000, 1000000}};
  const std::optional<CpuTime> total = disjoint_cpu_total(scopes);
  check(total && total->user_ns == 50000000, "user total");
  check(total && total->system_ns == 13000000, "system total");

  check(!disjoint_cpu_total({CpuTime{1, 1}, std::nullopt}),
        "a missing scope cannot be counted as zero");
  const std::optional<CpuTime> runner_only = disjoint_cpu_total({CpuTime{5, 6}});
  check(runner_only && runner_only->user_ns == 5 && runner_only->system_ns == 6,
        "a Run without Process participants totals the runner");
}

}  // namespace

int main() {
  try {
    test_linux_units();
    test_macos_units();
    test_unreported_peak_is_missing();
    test_disjoint_cpu_total();
  } catch (const std::exception &e) {
    std::cerr << "FAIL: " << e.what() << "\n";
    return 1;
  }
  std::cout << "resource usage: ok\n";
  return 0;
}
