#pragma once

// Host resource values for the diagnostics collector (#301).
//
// Only `sil-run-diagnostics` compiles this file. It converts one `rusage` into
// the report's units and adds CPU time across disjoint scopes. Peak resident
// values are never added: each is the high-water mark of one process, and the
// largest processes of two scopes need not have peaked at the same time.

#include <sys/resource.h>

#include <cstdint>
#include <optional>
#include <vector>

namespace sil::diagnostics {

// The unit the host kernel uses for ru_maxrss.
enum class MaxRssUnit { Kibibytes, Bytes };

struct CpuTime {
  uint64_t user_ns = 0;
  uint64_t system_ns = 0;
};

struct ResourceUsage {
  CpuTime cpu;
  // Missing when the host reports no high-water mark (zero or negative).
  std::optional<uint64_t> peak_rss_bytes;
};

ResourceUsage from_rusage(const rusage &usage, MaxRssUnit unit);

// The sum of CPU time over scopes that do not overlap. A missing scope makes
// the total missing: counting it as zero would understate the Run.
std::optional<CpuTime> disjoint_cpu_total(
    const std::vector<std::optional<CpuTime>> &scopes);

}  // namespace sil::diagnostics
