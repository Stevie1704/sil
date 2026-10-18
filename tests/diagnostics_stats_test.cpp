// The diagnostics collector's statistics, pinned with synthetic durations
// whose expected values come from closed forms and hand-worked examples, not
// from the collector's own arithmetic (#300).
#include "diagnostics_stats.hpp"

#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

using sil::diagnostics::DurationDistribution;
using sil::diagnostics::Percentile;

void check(bool condition, const std::string &message) {
  if (!condition) throw std::runtime_error(message);
}

void check_near(double actual, double expected, double tolerance,
                const std::string &message) {
  check(std::fabs(actual - expected) <= tolerance,
        message + ": got " + std::to_string(actual) + ", expected " +
            std::to_string(expected));
}

DurationDistribution one_to(uint64_t n) {
  DurationDistribution d(1);
  for (uint64_t i = 1; i <= n; i++) d.add(i - 1, i * 1000, i);
  return d;
}

/** 1..100: closed-form moments and the nearest-rank percentiles. */
void test_moments_and_nearest_rank() {
  const DurationDistribution d = one_to(100);
  check(d.count() == 100, "count");
  check(d.sum_ns() == 5050, "sum");
  check(d.min_ns() == 1 && d.max_ns() == 100, "extrema");
  check_near(*d.mean_ns(), 50.5, 1e-9, "mean");
  // Population standard deviation of 1..n is sqrt((n^2 - 1) / 12).
  check_near(*d.stddev_ns(), std::sqrt(9999.0 / 12.0), 1e-9, "stddev");
  check(!d.approximate(), "100 samples are exact");
  check(d.percentile(Percentile::P50).value_ns == 50, "p50 of 1..100");
  check(d.percentile(Percentile::P95).value_ns == 95, "p95 of 1..100");
  check(d.percentile(Percentile::P99).value_ns == 99, "p99 of 1..100");
}

/** Nearest rank is ceil(p * n / 100), never an interpolation. */
void test_nearest_rank_rounds_up() {
  // Five values, inserted out of order: p50 rank ceil(2.5) = 3.
  DurationDistribution d(1);
  const uint64_t values[] = {40, 10, 50, 20, 30};
  for (uint64_t i = 0; i < 5; i++) d.add(i, i, values[i]);
  check(d.percentile(Percentile::P50).value_ns == 30, "p50 of five");
  // A single sample is its own median.
  DurationDistribution single(1);
  single.add(0, 0, 7);
  check(single.percentile(Percentile::P50).value_ns == 7, "p50 of one");
  check(*single.stddev_ns() == 0.0, "stddev of one");
}

/** p95 needs 20 completed samples and p99 needs 100. */
void test_tail_minimum_counts() {
  check(!one_to(19).percentile(Percentile::P95).value_ns, "p95 at 19");
  check(one_to(19).percentile(Percentile::P95).unavailable_reason ==
            std::string("insufficient_samples"),
        "p95 reason at 19");
  check(one_to(20).percentile(Percentile::P95).value_ns == 19, "p95 at 20");
  check(!one_to(99).percentile(Percentile::P99).value_ns, "p99 at 99");
  check(one_to(100).percentile(Percentile::P99).value_ns.has_value(),
        "p99 at 100");
}

/** No completed activation: every statistic is absent, with a reason. */
void test_empty_distribution() {
  const DurationDistribution d(1);
  check(d.count() == 0 && d.sum_ns() == 0, "empty count and sum");
  check(!d.mean_ns() && !d.stddev_ns(), "empty moments");
  check(!d.percentile(Percentile::P50).value_ns, "empty p50");
  check(d.percentile(Percentile::P50).unavailable_reason ==
            std::string("no_completed_activations"),
        "empty reason");
  check(d.slowest().empty() && d.samples().empty(), "empty retained data");
}

/** Storage stays bounded while the moments and extrema stay exact. */
void test_bounded_storage() {
  const uint64_t n = 100000;
  DurationDistribution d(42);
  for (uint64_t i = 0; i < n; i++) d.add(i, i, i + 1);
  check(d.count() == n, "exact count above the reservoir");
  check(d.sum_ns() == n * (n + 1) / 2, "exact sum above the reservoir");
  check(d.min_ns() == 1 && d.max_ns() == n, "exact extrema");
  check(d.samples().size() == DurationDistribution::kReservoirCapacity,
        "reservoir holds 8192");
  check(d.approximate(), "quantiles above 8192 are estimates");
  check(d.slowest().size() == DurationDistribution::kSlowestRetained,
        "20 slowest");
  check(d.slowest().front().duration_ns == n, "slowest first");
  check(d.slowest().front().activation_index == n - 1, "slowest index");
  // Every retained sample came from the input, and none twice: the reservoir
  // replaces, it never duplicates.
  const std::set<uint64_t> distinct(d.samples().begin(), d.samples().end());
  check(distinct.size() == d.samples().size(), "no duplicated sample");
  check(*distinct.begin() >= 1 && *distinct.rbegin() <= n, "samples in range");
  // A uniform reservoir of 1..n keeps a mean near (n + 1) / 2. The seed is
  // fixed, so this is a deterministic check, not a noisy one.
  double sum = 0;
  for (const uint64_t sample : d.samples()) sum += double(sample);
  check_near(sum / double(d.samples().size()), double(n + 1) / 2.0,
             0.03 * double(n), "reservoir is uniform");
  const auto p50 = d.percentile(Percentile::P50).value_ns;
  check(p50 && *p50 > n / 2 - n / 20 && *p50 < n / 2 + n / 20,
        "estimated median");
}

/** The same seed retains the same samples; another seed differs. */
void test_reservoir_is_seeded() {
  DurationDistribution a(7), b(7), c(8);
  for (uint64_t i = 0; i < 20000; i++) {
    a.add(i, i, i);
    b.add(i, i, i);
    c.add(i, i, i);
  }
  check(a.samples() == b.samples(), "same seed, same samples");
  check(a.samples() != c.samples(), "another seed, other samples");
  check(a.seed() == 7, "seed is reported");
}

/** Equal durations keep the earliest activations, slowest first. */
void test_slowest_tie_break() {
  DurationDistribution d(1);
  for (uint64_t i = 0; i < 30; i++) d.add(i, i * 10, i == 25 ? 9 : 5);
  const auto slowest = d.slowest();
  check(slowest.size() == 20, "twenty retained");
  check(slowest[0].activation_index == 25 && slowest[0].duration_ns == 9,
        "longest first");
  check(slowest[0].virtual_time_ns == 250, "virtual time kept");
  for (size_t i = 1; i < slowest.size(); i++)
    check(slowest[i].activation_index == i - 1,
          "ties ordered by activation index");
}

/** Sampling draws from a private generator, never from the C library's. */
void test_sampling_leaves_the_c_library_generator_alone() {
  std::srand(1234);
  const int expected = std::rand();
  std::srand(1234);
  DurationDistribution d(1);
  for (uint64_t i = 0; i < 50000; i++) d.add(i, i, i);
  check(std::rand() == expected, "std::rand sequence unchanged");
}

}  // namespace

int main() {
  try {
    test_moments_and_nearest_rank();
    test_nearest_rank_rounds_up();
    test_tail_minimum_counts();
    test_empty_distribution();
    test_bounded_storage();
    test_reservoir_is_seeded();
    test_slowest_tie_break();
    test_sampling_leaves_the_c_library_generator_alone();
  } catch (const std::exception &e) {
    std::cerr << "FAIL: " << e.what() << "\n";
    return 1;
  }
  std::cout << "diagnostics stats: all tests passed\n";
  return 0;
}
