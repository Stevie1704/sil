#include "interceptor.hpp"

#include <algorithm>
#include <stdexcept>
#include <utility>

namespace sil {

InterceptorPlan::InterceptorPlan(uint64_t duration_ns,
                                 std::vector<Step> steps)
    : duration_ns_(duration_ns), steps_(std::move(steps)) {}

InterceptorPlan::InterceptorPlan(const InterceptorPlan &other)
    : duration_ns_(other.duration_ns_), steps_(other.steps_) {
  for (Step &step : steps_) step.window_count = 0;
}

InterceptorPlan::Verdict InterceptorPlan::apply(
    uint64_t now_ns, std::vector<uint8_t> &bytes) {
  const auto in_window = [now_ns](const Step &step) {
    return now_ns >= step.start_ns &&
           (!step.has_end || now_ns < step.end_ns);
  };

  // Drop decisions are made before any delay. A dropped message is never
  // shifted, rewritten, recorded, or delivered.
  for (Step &step : steps_) {
    if (!in_window(step)) continue;
    if (step.kind == Kind::Drop) return {true, now_ns};
    if (step.kind == Kind::DropNth) {
      if (++step.window_count % step.parameter == 0) return {true, now_ns};
    }
  }

  // Delays are evaluated in declaration order and saturate instead of
  // wrapping. Every matching delay uses the actual publish time for its
  // window, while the shifted value composes through the plan.
  uint64_t visible_ns = now_ns;
  for (const Step &step : steps_) {
    if (step.kind != Kind::Delay || !in_window(step)) continue;
    if (step.parameter > UINT64_MAX - visible_ns)
      visible_ns = UINT64_MAX;
    else
      visible_ns += step.parameter;
  }

  // The run covers [0, duration_ns); a message at or beyond the boundary is
  // suppressed before override, recording, and delivery.
  if (visible_ns >= duration_ns_) return {true, visible_ns};

  for (const Step &step : steps_) {
    if (step.kind != Kind::Override || !in_window(step)) continue;
    if (step.override_offset > bytes.size() ||
        step.override_bytes.size() > bytes.size() - step.override_offset)
      throw std::out_of_range(
          "compiled interceptor override exceeds message bytes");
    std::copy(step.override_bytes.begin(), step.override_bytes.end(),
              bytes.begin() + step.override_offset);
  }
  return {false, visible_ns};
}

}  // namespace sil
