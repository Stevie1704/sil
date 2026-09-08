#include "virtual_time.hpp"

#include <cstdint>
#include <iostream>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

using sil::advance_virtual_time;

/** Fail the test process with a useful message when an invariant is false. */
void check(bool condition, const std::string &message) {
  if (!condition) throw std::runtime_error(message);
}

std::string describe(uint64_t from_ns, uint64_t delta_ns) {
  return std::to_string(from_ns) + " + " + std::to_string(delta_ns);
}

/** Every boundary of the unsigned virtual-time addition, in one table. */
void test_boundaries() {
  struct Case {
    uint64_t from_ns;
    uint64_t delta_ns;
    std::optional<uint64_t> expected;
  };
  const std::vector<Case> cases = {
      {0, 0, 0},
      {0, 1, 1},
      {5, 7, 12},
      {0, UINT64_MAX, UINT64_MAX},
      {UINT64_MAX, 0, UINT64_MAX},
      {1, UINT64_MAX - 1, UINT64_MAX},
      {UINT64_MAX - 1, 1, UINT64_MAX},
      {2, UINT64_MAX - 1, std::nullopt},
      {UINT64_MAX - 1, 2, std::nullopt},
      {UINT64_MAX, 1, std::nullopt},
      {1, UINT64_MAX, std::nullopt},
      {UINT64_MAX, UINT64_MAX, std::nullopt},
      // The hostile registration from issue #50: offset 5, period UINT64_MAX.
      {5, UINT64_MAX, std::nullopt},
  };
  for (const Case &c : cases) {
    const std::optional<uint64_t> actual =
        advance_virtual_time(c.from_ns, c.delta_ns);
    check(actual.has_value() == c.expected.has_value(),
          describe(c.from_ns, c.delta_ns) +
              ": representability did not match the table");
    check(!actual || *actual == *c.expected,
          describe(c.from_ns, c.delta_ns) + " gave " +
              std::to_string(*actual) + ", expected " +
              std::to_string(*c.expected));
  }
}

/** No accepted addition ever moves virtual time backwards. */
void test_never_runs_backwards() {
  const std::vector<uint64_t> points = {0,
                                        1,
                                        2,
                                        1000,
                                        UINT64_MAX / 2,
                                        UINT64_MAX - 2,
                                        UINT64_MAX - 1,
                                        UINT64_MAX};
  for (uint64_t from_ns : points)
    for (uint64_t delta_ns : points) {
      const std::optional<uint64_t> actual =
          advance_virtual_time(from_ns, delta_ns);
      check(!actual || *actual >= from_ns,
            describe(from_ns, delta_ns) + " wrapped to an earlier instant");
    }
}

}  // namespace

int main() {
  try {
    test_boundaries();
    test_never_runs_backwards();
  } catch (const std::exception &e) {
    std::cerr << "virtual time test failed: " << e.what() << '\n';
    return 1;
  }
  return 0;
}
