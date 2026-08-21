#pragma once

#include <cstddef>
#include <cstdint>
#include <memory>
#include <vector>

namespace sil {

struct InterceptorSpec;
struct SchemaSpec;

/**
 * The compiled interceptor plan for one channel.
 *
 * A plan is compiled while the manifest is loaded. Runtime evaluation has one
 * entry point and deliberately keeps its ordering private:
 * drop/drop_nth, then delay, then the half-open run-duration truncation, then
 * override. Delays saturate at UINT64_MAX and compose in declared order.
 *
 * The plan never owns sequence numbers. The engine still advances its global
 * publish order for every suppressed message, while channel sequence numbers
 * advance only for messages that survive the plan.
 */
class InterceptorPlan {
 public:
  /** The visibility result of applying this channel's interceptor plan. */
  struct Verdict {
    bool suppressed;
    uint64_t visible_ns;
  };

  /**
   * Applies the complete plan to one message in place.
   *
   * `now_ns` is the message's actual publish time. Window matching always uses
   * that time, including for messages whose visible time is later shifted by a
   * delay. `bytes` must have the channel schema's declared size.
   */
  Verdict apply(uint64_t now_ns, std::vector<uint8_t> &bytes);

 private:
  enum class Kind : uint8_t { Drop, DropNth, Delay, Override };

  struct Step {
    Kind kind;
    uint64_t start_ns;
    uint64_t end_ns;
    bool has_end;
    uint64_t parameter;
    uint64_t window_count;
    size_t override_offset;
    std::vector<uint8_t> override_bytes;
  };

  InterceptorPlan(uint64_t duration_ns, std::vector<Step> steps);

  friend std::shared_ptr<InterceptorPlan> compile_interceptor_plan(
      uint64_t duration_ns, const SchemaSpec &schema,
      const std::vector<InterceptorSpec> &specs);

  uint64_t duration_ns_;
  std::vector<Step> steps_;
};

/** Compile one channel's validated manifest declarations into a runtime plan. */
std::shared_ptr<InterceptorPlan> compile_interceptor_plan(
    uint64_t duration_ns, const SchemaSpec &schema,
    const std::vector<InterceptorSpec> &specs);

}  // namespace sil
