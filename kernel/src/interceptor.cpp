#include "interceptor.hpp"

#include <algorithm>
#include <cstring>
#include <map>
#include <stdexcept>
#include <string>
#include <utility>

#include "manifest.hpp"

namespace sil {

namespace {

// This table is used only while compiling an override. The resulting plan
// stores bytes, so no type lookup or numeric conversion is needed at publish.
struct TypeLayout {
  size_t size;
  enum class Representation { Unsigned, Signed, Float } representation;
};

const std::map<std::string, TypeLayout> kTypeLayouts = {
    {"u8", {1, TypeLayout::Representation::Unsigned}},
    {"u16", {2, TypeLayout::Representation::Unsigned}},
    {"u32", {4, TypeLayout::Representation::Unsigned}},
    {"u64", {8, TypeLayout::Representation::Unsigned}},
    {"i8", {1, TypeLayout::Representation::Signed}},
    {"i16", {2, TypeLayout::Representation::Signed}},
    {"i32", {4, TypeLayout::Representation::Signed}},
    {"i64", {8, TypeLayout::Representation::Signed}},
    {"f32", {4, TypeLayout::Representation::Float}},
    {"f64", {8, TypeLayout::Representation::Float}}};

std::vector<uint8_t> encode_override(const TypeLayout &layout, double value) {
  uint64_t bits = 0;
  switch (layout.representation) {
    case TypeLayout::Representation::Unsigned:
      bits = static_cast<uint64_t>(value);
      break;
    case TypeLayout::Representation::Signed:
      bits = static_cast<uint64_t>(static_cast<int64_t>(value));
      break;
    case TypeLayout::Representation::Float:
      if (layout.size == 4) {
        float f = static_cast<float>(value);
        std::memcpy(&bits, &f, sizeof(f));
      } else {
        std::memcpy(&bits, &value, sizeof(value));
      }
      break;
  }

  std::vector<uint8_t> bytes(layout.size);
  for (size_t i = 0; i < layout.size; i++)
    bytes[i] = static_cast<uint8_t>(bits >> (8 * i));
  return bytes;
}

}  // namespace

InterceptorPlan::InterceptorPlan(uint64_t duration_ns,
                                 std::vector<Step> steps)
    : duration_ns_(duration_ns), steps_(std::move(steps)) {}

std::shared_ptr<InterceptorPlan> compile_interceptor_plan(
    uint64_t duration_ns, const SchemaSpec &schema,
    const std::vector<InterceptorSpec> &specs) {
  const auto compile_kind = [](const std::string &kind) {
    if (kind == "drop") return InterceptorPlan::Kind::Drop;
    if (kind == "drop_nth") return InterceptorPlan::Kind::DropNth;
    if (kind == "delay") return InterceptorPlan::Kind::Delay;
    if (kind == "override") return InterceptorPlan::Kind::Override;
    throw std::logic_error("invalid interceptor kind during compilation");
  };

  std::vector<InterceptorPlan::Step> steps;
  steps.reserve(specs.size());

  for (const InterceptorSpec &spec : specs) {
    InterceptorPlan::Step step{compile_kind(spec.kind), spec.start_ns, 0,
                               spec.end_ns.has_value(), 0, 0, 0, {}};
    if (step.has_end) step.end_ns = *spec.end_ns;

    if (step.kind == InterceptorPlan::Kind::Delay) {
      if (!spec.delay_ns)
        throw std::logic_error("delay interceptor missing delay_ns");
      step.parameter = *spec.delay_ns;
    } else if (step.kind == InterceptorPlan::Kind::DropNth) {
      if (!spec.n) throw std::logic_error("drop_nth interceptor missing n");
      step.parameter = *spec.n;
    } else if (step.kind == InterceptorPlan::Kind::Override) {
      size_t offset = 0;
      const FieldSpec *field = nullptr;
      TypeLayout layout{};
      for (const FieldSpec &candidate : schema.fields) {
        auto type = kTypeLayouts.find(candidate.type);
        if (type == kTypeLayouts.end())
          throw std::logic_error("unknown field type during compilation");
        if (candidate.name == spec.field) {
          field = &candidate;
          layout = type->second;
          break;
        }
        offset += type->second.size *
                  (candidate.count == 0 ? 1 : candidate.count);
      }
      if (!field)
        throw std::logic_error("override field missing during compilation");
      if (field->count != 0)
        throw std::logic_error("array override during compilation");
      step.override_offset = offset;
      step.override_bytes = encode_override(layout, spec.value);
    }
    steps.push_back(std::move(step));
  }

  return std::shared_ptr<InterceptorPlan>(
      new InterceptorPlan(duration_ns, std::move(steps)));
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
    std::copy(step.override_bytes.begin(), step.override_bytes.end(),
              bytes.begin() + step.override_offset);
  }
  return {false, visible_ns};
}

}  // namespace sil
