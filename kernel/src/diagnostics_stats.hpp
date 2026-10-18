#pragma once

// Bounded statistics over completed activation durations (#300).
//
// Only `sil-run-diagnostics` compiles this file. One distribution keeps exact
// count, sum, extrema and running moments, a uniform reservoir of at most
// kReservoirCapacity durations for the quantiles, and the kSlowestRetained
// slowest activations. Its storage depends on those two bounds, never on the
// number of activations in the Run.

#include <cstddef>
#include <cstdint>
#include <optional>
#include <vector>

namespace sil::diagnostics {

enum class Percentile { P50, P95, P99 };

// A percentile has either a value or a reason why it does not.
struct PercentileValue {
  std::optional<uint64_t> value_ns;
  const char *unavailable_reason = nullptr;
};

struct SlowActivation {
  uint64_t activation_index;
  uint64_t virtual_time_ns;
  uint64_t duration_ns;
};

class DurationDistribution {
 public:
  static constexpr size_t kReservoirCapacity = 8192;
  static constexpr size_t kSlowestRetained = 20;
  // The tail percentiles need this many completed samples. The minimums only
  // keep a tail from being a single value; they do not make it reliable.
  static constexpr uint64_t kP95MinimumSamples = 20;
  static constexpr uint64_t kP99MinimumSamples = 100;

  // `seed` drives the private reservoir generator and is reported with it.
  explicit DurationDistribution(uint64_t seed);

  void add(uint64_t activation_index, uint64_t virtual_time_ns,
           uint64_t duration_ns);

  uint64_t count() const { return count_; }
  uint64_t sum_ns() const { return sum_ns_; }
  // Meaningful only when count() > 0.
  uint64_t min_ns() const { return min_ns_; }
  uint64_t max_ns() const { return max_ns_; }
  std::optional<double> mean_ns() const;
  // Population standard deviation.
  std::optional<double> stddev_ns() const;

  // Nearest-rank over the retained samples: exact up to kReservoirCapacity
  // completed activations, an estimate above it.
  PercentileValue percentile(Percentile p) const;
  bool approximate() const { return count_ > kReservoirCapacity; }
  const std::vector<uint64_t> &samples() const { return samples_; }
  uint64_t seed() const { return seed_; }

  // Slowest first; equal durations in activation-index order.
  const std::vector<SlowActivation> &slowest() const { return slowest_; }

 private:
  uint64_t next_random();
  void keep_if_slow(const SlowActivation &activation);

  uint64_t seed_;
  uint64_t rng_state_;
  uint64_t count_ = 0;
  uint64_t sum_ns_ = 0;
  uint64_t min_ns_ = 0;
  uint64_t max_ns_ = 0;
  double mean_ = 0;
  double m2_ = 0;
  std::vector<uint64_t> samples_;
  std::vector<SlowActivation> slowest_;
};

}  // namespace sil::diagnostics
