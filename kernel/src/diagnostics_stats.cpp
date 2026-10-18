#include "diagnostics_stats.hpp"

#include <algorithm>
#include <cmath>

namespace sil::diagnostics {

DurationDistribution::DurationDistribution(uint64_t seed)
    : seed_(seed), rng_state_(seed) {}

// splitmix64: a private generator, so sampling cannot advance any generator
// a Participant shares with the runner process.
uint64_t DurationDistribution::next_random() {
  uint64_t z = (rng_state_ += 0x9E3779B97F4A7C15ULL);
  z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
  z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
  return z ^ (z >> 31);
}

void DurationDistribution::add(uint64_t activation_index,
                               uint64_t virtual_time_ns,
                               uint64_t duration_ns) {
  if (count_ == 0) {
    min_ns_ = max_ns_ = duration_ns;
  } else {
    min_ns_ = std::min(min_ns_, duration_ns);
    max_ns_ = std::max(max_ns_, duration_ns);
  }
  count_++;
  sum_ns_ += duration_ns;
  // Welford's update gives a stable variance without storing values. Its
  // running mean rounds at every step, so mean_ns() uses the exact sum.
  const double delta = double(duration_ns) - running_mean_;
  running_mean_ += delta / double(count_);
  m2_ += delta * (double(duration_ns) - running_mean_);

  // Algorithm R: the k-th value replaces a random slot with probability
  // capacity / k, so every value is retained with the same probability.
  if (samples_.size() < kReservoirCapacity) {
    samples_.push_back(duration_ns);
  } else {
    const uint64_t slot = next_random() % count_;
    if (slot < kReservoirCapacity) samples_[slot] = duration_ns;
  }
  keep_if_slow({activation_index, virtual_time_ns, duration_ns});
}

void DurationDistribution::keep_if_slow(const SlowActivation &activation) {
  // Activations arrive in index order, so a later equal duration sorts after
  // every earlier one and never displaces it.
  const auto slower = [](const SlowActivation &a, const SlowActivation &b) {
    if (a.duration_ns != b.duration_ns) return a.duration_ns > b.duration_ns;
    return a.activation_index < b.activation_index;
  };
  if (slowest_.size() == kSlowestRetained &&
      !slower(activation, slowest_.back()))
    return;
  slowest_.insert(std::upper_bound(slowest_.begin(), slowest_.end(),
                                   activation, slower),
                  activation);
  if (slowest_.size() > kSlowestRetained) slowest_.pop_back();
}

namespace {

// The double nearest to numerator / denominator (ties to even), for any
// 64-bit operands. A plain double division rounds the numerator first once
// it exceeds 2^53, and an integer part plus a fraction rounds twice. Here the
// numerator is scaled until the integer quotient has at least 55 bits, so
// that quotient plus a sticky bit for the remainder carries everything one
// correct rounding to 53 bits needs.
double correctly_rounded_quotient(uint64_t numerator, uint64_t denominator) {
  if (numerator == 0) return 0.0;
  using u128 = unsigned __int128;
  const u128 at_least = u128(denominator) << 54;
  u128 scaled = numerator;
  int shift = 0;
  while (scaled < at_least) {
    scaled <<= 1;
    shift++;
  }
  u128 quotient = scaled / denominator;
  if (scaled % denominator != 0) quotient |= 1;
  return std::ldexp(double(quotient), -shift);
}

}  // namespace

std::optional<double> DurationDistribution::mean_ns() const {
  if (count_ == 0) return std::nullopt;
  return correctly_rounded_quotient(sum_ns_, count_);
}

std::optional<double> DurationDistribution::stddev_ns() const {
  if (count_ == 0) return std::nullopt;
  return std::sqrt(m2_ / double(count_));
}

PercentileValue DurationDistribution::percentile(Percentile p) const {
  if (count_ == 0) return {std::nullopt, "no_completed_activations"};
  uint64_t rank_percent = 50;
  uint64_t minimum = 1;
  if (p == Percentile::P95) {
    rank_percent = 95;
    minimum = kP95MinimumSamples;
  } else if (p == Percentile::P99) {
    rank_percent = 99;
    minimum = kP99MinimumSamples;
  }
  if (count_ < minimum) return {std::nullopt, "insufficient_samples"};
  std::vector<uint64_t> sorted = samples_;
  std::sort(sorted.begin(), sorted.end());
  // Nearest rank, ceil(p * n / 100), in integers so no rounding can move it.
  const uint64_t n = sorted.size();
  const uint64_t rank = std::max<uint64_t>(1, (rank_percent * n + 99) / 100);
  return {sorted[rank - 1], nullptr};
}

}  // namespace sil::diagnostics
