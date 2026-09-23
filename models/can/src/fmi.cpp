#include "bus.hpp"
#include "fmi3Functions.h"
#include "profile.hpp"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <memory>

namespace {
static_assert(profile::terminal_count == 2, "the model core supports exactly two terminals");
enum class Mode { instantiated, initialization, event, step, terminated, error };
struct EventState {
  std::array<can::Bytes, profile::terminal_count> inputs;
  std::array<bool, profile::terminal_count> written{}, rx{}, tx{};
  bool evaluated = false;

  bool pending() const {
    const auto active = [](const auto& flags) {
      return std::any_of(flags.begin(), flags.end(), [](bool flag) { return flag; });
    };
    return evaluated || active(written) || active(rx) || active(tx);
  }
};
struct Instance {
  can::Bus bus;
  Mode mode = Mode::instantiated;
  can::Nanoseconds time = 0;
  EventState event;
  std::array<fmi3IntervalQualifier, 2> interval{fmi3IntervalNotYetKnown, fmi3IntervalNotYetKnown};
  fmi3InstanceEnvironment environment = nullptr;
  fmi3LogMessageCallback log = nullptr;
};
void require(bool condition, const char* message) {
  if (!condition) throw std::runtime_error(message);
}
void require_mode(const Instance& i, Mode expected) {
  require(i.mode == expected, "invalid FMI lifecycle state");
}
template<class F> fmi3Status invoke_fmi(fmi3Instance instance, F body) noexcept {
  if (!instance) return fmi3Error;
  auto& i = *static_cast<Instance*>(instance);
  try {
    require(i.mode != Mode::error, "instance is in Error state; reset or free it");
    body(i);
    return fmi3OK;
  } catch (const std::exception& e) {
    i.mode = Mode::error;
    try { if (i.log) i.log(i.environment, fmi3Error, "error", e.what()); } catch (...) {}
  } catch (...) {
    i.mode = Mode::error;
  }
  return fmi3Error;
}
// FMI time is Float64 seconds; the bus counts whole nanoseconds. The supported
// range keeps every instant exact in both, so nothing is silently rounded.
can::Nanoseconds nanoseconds(double seconds) {
  require(std::isfinite(seconds) && seconds >= 0 &&
          seconds * can::ns_per_s <= double(can::max_time), "time outside the supported range");
  return std::llround(seconds * can::ns_per_s);
}
can::Nanoseconds remaining(const Instance& i) {
  const auto due = i.bus.next_completion();
  return due ? *due - i.time : 0;
}
void require_initialization_or_event(const Instance& i) {
  require(i.mode == Mode::initialization || i.mode == Mode::event,
          "operation requires Initialization or Event Mode");
}
void evaluate(Instance& i) {
  if (i.event.evaluated) return;
  require_mode(i, Mode::event);
  auto next = i.bus;
  const auto before = next.next_completion();
  // Each event commits all inputs together, so a second request cannot hide
  // behind terminal order. A frame completes before same-instant requests
  // are scheduled; completion is only legal at the frame's end.
  require(i.event.tx[0] == i.event.tx[1], "both countdown Clocks must activate together");
  if (i.event.tx[0]) next.complete(i.time);
  for (unsigned n = 0; n < profile::terminal_count; ++n) {
    require(i.event.rx[n] == i.event.written[n], "input Binary and Clock must be supplied together");
    if (i.event.rx[n]) next.receive(n, i.event.inputs[n], i.time);
  }
  const auto after = next.next_completion();
  if (after != before) i.interval.fill(after ? fmi3IntervalChanged : fmi3IntervalNotYetKnown);
  i.bus = std::move(next);
  i.event.evaluated = true;
}
unsigned terminal(fmi3ValueReference vr, unsigned member) {
  require(vr / profile::terminal_stride < profile::terminal_count &&
          vr % profile::terminal_stride == member, "unknown value reference");
  return vr / profile::terminal_stride;
}
template<class WriteValue>
void read_intervals(Instance& i, const fmi3ValueReference vr[], size_t n,
                    fmi3IntervalQualifier qualifiers[], WriteValue write_value) {
  require_initialization_or_event(i);
  require(!n || (vr && qualifiers), "null interval result");
  for (size_t k = 0; k < n; ++k) {
    auto node = terminal(vr[k], profile::tx_clock);
    write_value(k);
    qualifiers[k] = i.interval[node];
    if (i.interval[node] == fmi3IntervalChanged) i.interval[node] = fmi3IntervalUnchanged;
  }
}
}  // namespace

extern "C" {
const char* fmi3GetVersion() { return "3.0"; }
fmi3Instance fmi3InstantiateCoSimulation(fmi3String name, fmi3String token,
    fmi3String, fmi3Boolean visible, fmi3Boolean, fmi3Boolean eventModeUsed,
    fmi3Boolean, const fmi3ValueReference[], size_t required,
    fmi3InstanceEnvironment environment, fmi3LogMessageCallback log,
    fmi3IntermediateUpdateCallback) {
  try {
    if (!name || !*name || !token || std::strcmp(token, profile::token) ||
        visible || !eventModeUsed || required) return nullptr;
    auto i = std::make_unique<Instance>();
    i->environment = environment;
    i->log = log;
    return i.release();
  } catch (...) { return nullptr; }
}
void fmi3FreeInstance(fmi3Instance instance) { delete static_cast<Instance*>(instance); }
fmi3Status fmi3Reset(fmi3Instance instance) {
  if (!instance) return fmi3Error;
  auto& i = *static_cast<Instance*>(instance);
  auto environment = i.environment;
  auto log = i.log;
  i = Instance{};
  i.environment = environment;
  i.log = log;
  return fmi3OK;
}
fmi3Status fmi3SetDebugLogging(fmi3Instance instance, fmi3Boolean, size_t n,
                              const fmi3String[]) {
  return invoke_fmi(instance, [&](Instance&) { require(n == 0, "no log categories are configurable"); });
}
fmi3Status fmi3EnterInitializationMode(fmi3Instance instance, fmi3Boolean,
    fmi3Float64, fmi3Float64 start, fmi3Boolean stopDefined, fmi3Float64 stop) {
  return invoke_fmi(instance, [&](Instance& i) {
    require_mode(i, Mode::instantiated);
    require(!stopDefined || (std::isfinite(stop) && stop > start), "invalid experiment time");
    i.time = nanoseconds(start);
    i.mode = Mode::initialization;
  });
}
fmi3Status fmi3ExitInitializationMode(fmi3Instance instance) {
  return invoke_fmi(instance, [](Instance& i) { require_mode(i, Mode::initialization);
    // Initial assignments are values, not Clock activations. The first event
    // starts with no outstanding write or transmission request.
    i.event = {};
    i.mode = Mode::event; });
}
fmi3Status fmi3EnterEventMode(fmi3Instance instance) {
  return invoke_fmi(instance, [](Instance& i) { require_mode(i, Mode::step); i.mode = Mode::event; });
}
fmi3Status fmi3EnterStepMode(fmi3Instance instance) {
  return invoke_fmi(instance, [](Instance& i) {
    require_mode(i, Mode::event);
    require(!i.event.pending(), "finish the discrete update before Step Mode");
    i.mode = Mode::step;
  });
}
fmi3Status fmi3Terminate(fmi3Instance instance) {
  return invoke_fmi(instance, [](Instance& i) {
    require(i.mode == Mode::step || i.mode == Mode::event, "invalid termination state");
    i.mode = Mode::terminated;
  });
}
fmi3Status fmi3DoStep(fmi3Instance instance, fmi3Float64 current, fmi3Float64 step,
    fmi3Boolean, fmi3Boolean* event, fmi3Boolean* terminate, fmi3Boolean* early,
    fmi3Float64* last) {
  return invoke_fmi(instance, [&](Instance& i) {
    require_mode(i, Mode::step);
    require(event && terminate && early && last, "null DoStep result");
    const auto end = nanoseconds(current + step);
    require(nanoseconds(current) == i.time && end > i.time, "invalid communication interval");
    const auto due = i.bus.next_completion();
    require(!due || end <= *due, "step passes pending countdown instant");
    i.time = end;
    *event = *terminate = *early = false;
    *last = current + step;
  });
}
fmi3Status fmi3SetBinary(fmi3Instance instance, const fmi3ValueReference vr[],
    size_t n, const size_t sizes[], const fmi3Binary values[], size_t count) {
  return invoke_fmi(instance, [&](Instance& i) {
    require_initialization_or_event(i);
    require(!i.event.evaluated && n == count && (!n || (vr && sizes && values)), "invalid Binary input call");
    for (size_t k = 0; k < n; ++k) {
      auto node = terminal(vr[k], profile::rx_data);
      require(i.mode == Mode::initialization || i.event.rx[node], "activate input Clock before Binary write");
      require((i.mode == Mode::initialization || !i.event.written[node]) &&
              sizes[k] <= profile::max_binary_size && (!sizes[k] || values[k]), "invalid or duplicate Binary input");
      i.event.inputs[node].clear();
      if (sizes[k]) i.event.inputs[node].assign(values[k], values[k] + sizes[k]);
      i.event.written[node] = true;
    }
  });
}
fmi3Status fmi3SetClock(fmi3Instance instance, const fmi3ValueReference vr[],
    size_t n, const fmi3Clock values[]) {
  return invoke_fmi(instance, [&](Instance& i) {
    require_mode(i, Mode::event);
    require(!i.event.evaluated && (!n || (vr && values)), "invalid Clock input call");
    for (size_t k = 0; k < n; ++k) {
      require(vr[k] / profile::terminal_stride < profile::terminal_count &&
              (vr[k] % profile::terminal_stride == profile::rx_clock ||
               vr[k] % profile::terminal_stride == profile::tx_clock), "unknown input Clock");
      require(values[k], "only Clock activation is supported");
      auto& clock = vr[k] % profile::terminal_stride == profile::rx_clock
                        ? i.event.rx[vr[k] / profile::terminal_stride]
                        : i.event.tx[vr[k] / profile::terminal_stride];
      require(!clock, "duplicate Clock activation");
      clock = true;
    }
  });
}
fmi3Status fmi3GetBinary(fmi3Instance instance, const fmi3ValueReference vr[],
    size_t n, size_t sizes[], fmi3Binary values[], size_t count) {
  return invoke_fmi(instance, [&](Instance& i) {
    require_initialization_or_event(i);
    require(n == count && (!n || (vr && sizes && values)), "invalid Binary output call");
    for (size_t k = 0; k < n; ++k) {
      auto node = terminal(vr[k], profile::tx_data);
      if (i.mode == Mode::event) {
        require(i.event.tx[node], "output Binary requires active countdown Clock");
        evaluate(i);
      }
      // Initialization has no transfers; calculated outputs are empty.
      const auto& bytes = i.bus.outputs()[node];
      sizes[k] = bytes.size();
      values[k] = bytes.data();
    }
  });
}
fmi3Status fmi3UpdateDiscreteStates(fmi3Instance instance, fmi3Boolean* update,
    fmi3Boolean* terminate, fmi3Boolean* nominals, fmi3Boolean* values,
    fmi3Boolean* next, fmi3Float64* time) {
  return invoke_fmi(instance, [&](Instance& i) {
    require(update && terminate && nominals && values && next && time, "null discrete-state result");
    evaluate(i);
    *update = *terminate = *nominals = *values = *next = false;
    *time = 0;
    i.bus.clear_outputs();
    i.event = {};
  });
}
fmi3Status fmi3GetIntervalFraction(fmi3Instance instance, const fmi3ValueReference vr[],
    size_t n, fmi3UInt64 counters[], fmi3UInt64 resolutions[], fmi3IntervalQualifier qualifiers[]) {
  return invoke_fmi(instance, [&](Instance& i) {
    require(!n || (counters && resolutions), "null interval result");
    read_intervals(i, vr, n, qualifiers, [&](size_t k) {
      counters[k] = fmi3UInt64(remaining(i));
      resolutions[k] = can::ns_per_s;
    });
  });
}
fmi3Status fmi3GetIntervalDecimal(fmi3Instance instance, const fmi3ValueReference vr[],
    size_t n, fmi3Float64 intervals[], fmi3IntervalQualifier qualifiers[]) {
  return invoke_fmi(instance, [&](Instance& i) {
    require(!n || intervals, "null interval result");
    read_intervals(i, vr, n, qualifiers, [&](size_t k) {
      intervals[k] = double(remaining(i)) / can::ns_per_s;
    });
  });
}
fmi3Status fmi3GetFloat64(fmi3Instance instance, const fmi3ValueReference vr[],
    size_t n, fmi3Float64 values[], size_t count) {
  return invoke_fmi(instance, [&](Instance& i) {
    require(n == count && (!n || (vr && values)), "invalid Float64 output call");
    for (size_t k = 0; k < n; ++k) {
      require(vr[k] == profile::time, "unknown Float64 value reference");
      values[k] = double(i.time) / can::ns_per_s;
    }
  });
}
}  // extern "C"
