#include "bus.hpp"
#include "fmi3Functions.h"

#include <cmath>
#include <cstring>
#include <limits>
#include <memory>

namespace {
enum class Mode { instantiated, initialization, event, step, terminated, error };
struct Instance {
  can::Bus bus;
  Mode mode = Mode::instantiated;
  double time = 0, due = 0;
  std::array<can::Bytes, 2> inputs;
  std::array<bool, 2> written{}, rx{}, tx{};
  std::array<fmi3IntervalQualifier, 2> interval{fmi3IntervalNotYetKnown, fmi3IntervalNotYetKnown};
  bool evaluated = false;
  fmi3InstanceEnvironment environment = nullptr;
  fmi3LogMessageCallback log = nullptr;
};
void require(bool condition, const char* message) {
  if (!condition) throw std::runtime_error(message);
}
void mode(const Instance& i, Mode expected) {
  require(i.mode == expected, "invalid FMI lifecycle state");
}
template<class F> fmi3Status call(fmi3Instance instance, F body) noexcept {
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
void evaluate(Instance& i) {
  if (i.evaluated) return;
  mode(i, Mode::event);
  auto next = i.bus;
  // Each event commits all inputs together, so a second request cannot hide
  // behind terminal order. Completion is only legal at the declared instant.
  require(i.tx[0] == i.tx[1], "both countdown Clocks must activate together");
  if (i.tx[0]) {
    require(next.pending() && std::abs(i.time - i.due) < 1e-12,
            "countdown activated away from its declared instant");
    next.complete();
  }
  for (unsigned n = 0; n < 2; ++n) {
    require(i.rx[n] == i.written[n], "input Binary and Clock must be supplied together");
    if (i.rx[n]) next.receive(n, i.inputs[n]);
  }
  if (i.tx[0]) i.interval.fill(fmi3IntervalNotYetKnown);
  if (next.pending() && (!i.bus.pending() || i.tx[0])) {
    i.due = i.time + can::Bus::transfer_seconds;
    i.interval.fill(fmi3IntervalChanged);
  }
  i.bus = std::move(next);
  i.evaluated = true;
}
unsigned terminal(fmi3ValueReference vr, unsigned member) {
  require(vr == member || vr == member + 4, "unknown value reference");
  return vr / 4;
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
    if (!name || !*name || !token || std::strcmp(token, "sil-can-smoke-1") ||
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
  return call(instance, [&](Instance&) { require(n == 0, "no log categories are configurable"); });
}
fmi3Status fmi3EnterInitializationMode(fmi3Instance instance, fmi3Boolean,
    fmi3Float64, fmi3Float64 start, fmi3Boolean stopDefined, fmi3Float64 stop) {
  return call(instance, [&](Instance& i) {
    mode(i, Mode::instantiated);
    require(std::isfinite(start) && (!stopDefined || (std::isfinite(stop) && stop > start)),
            "invalid experiment time");
    i.time = start;
    i.mode = Mode::initialization;
  });
}
fmi3Status fmi3ExitInitializationMode(fmi3Instance instance) {
  return call(instance, [](Instance& i) { mode(i, Mode::initialization); i.mode = Mode::event; });
}
fmi3Status fmi3EnterEventMode(fmi3Instance instance) {
  return call(instance, [](Instance& i) { mode(i, Mode::step); i.mode = Mode::event; });
}
fmi3Status fmi3EnterStepMode(fmi3Instance instance) {
  return call(instance, [](Instance& i) {
    mode(i, Mode::event);
    require(!i.evaluated && !i.rx[0] && !i.rx[1] && !i.tx[0] && !i.tx[1] &&
            !i.written[0] && !i.written[1], "finish the discrete update before Step Mode");
    i.mode = Mode::step;
  });
}
fmi3Status fmi3Terminate(fmi3Instance instance) {
  return call(instance, [](Instance& i) {
    require(i.mode == Mode::step || i.mode == Mode::event, "invalid termination state");
    i.mode = Mode::terminated;
  });
}
fmi3Status fmi3DoStep(fmi3Instance instance, fmi3Float64 current, fmi3Float64 step,
    fmi3Boolean, fmi3Boolean* event, fmi3Boolean* terminate, fmi3Boolean* early,
    fmi3Float64* last) {
  return call(instance, [&](Instance& i) {
    mode(i, Mode::step);
    require(event && terminate && early && last, "null DoStep result");
    require(std::isfinite(current) && std::isfinite(step) && step > 0 &&
            std::abs(current - i.time) < 1e-12 && std::isfinite(current + step) &&
            current + step > current, "invalid communication interval");
    require(!i.bus.pending() || current + step <= i.due + 1e-12,
            "step passes pending countdown instant");
    i.time = current + step;
    *event = *terminate = *early = false;
    *last = i.time;
  });
}
fmi3Status fmi3SetBinary(fmi3Instance instance, const fmi3ValueReference vr[],
    size_t n, const size_t sizes[], const fmi3Binary values[], size_t count) {
  return call(instance, [&](Instance& i) {
    require(i.mode == Mode::event || i.mode == Mode::initialization, "Binary input requires Event or Initialization Mode");
    require(!i.evaluated && n == count && (!n || (vr && sizes && values)), "invalid Binary input call");
    for (size_t k = 0; k < n; ++k) {
      auto node = terminal(vr[k], 0);
      require(i.mode == Mode::initialization || i.rx[node], "activate input Clock before Binary write");
      require(!i.written[node] && sizes[k] <= 2048 && (!sizes[k] || values[k]), "invalid or duplicate Binary input");
      i.inputs[node].clear();
      if (sizes[k]) i.inputs[node].assign(values[k], values[k] + sizes[k]);
      i.written[node] = true;
    }
  });
}
fmi3Status fmi3SetClock(fmi3Instance instance, const fmi3ValueReference vr[],
    size_t n, const fmi3Clock values[]) {
  return call(instance, [&](Instance& i) {
    mode(i, Mode::event);
    require(!i.evaluated && (!n || (vr && values)), "invalid Clock input call");
    for (size_t k = 0; k < n; ++k) {
      require(vr[k] < 8 && (vr[k] % 4 == 2 || vr[k] % 4 == 3), "unknown input Clock");
      require(values[k], "only Clock activation is supported");
      auto& clock = vr[k] % 4 == 2 ? i.rx[vr[k] / 4] : i.tx[vr[k] / 4];
      require(!clock, "duplicate Clock activation");
      clock = true;
    }
  });
}
fmi3Status fmi3GetBinary(fmi3Instance instance, const fmi3ValueReference vr[],
    size_t n, size_t sizes[], fmi3Binary values[], size_t count) {
  return call(instance, [&](Instance& i) {
    mode(i, Mode::event);
    require(n == count && (!n || (vr && sizes && values)), "invalid Binary output call");
    for (size_t k = 0; k < n; ++k) {
      auto node = terminal(vr[k], 1);
      require(i.tx[node], "output Binary requires active countdown Clock");
      evaluate(i);
      const auto& bytes = i.bus.outputs()[node];
      sizes[k] = bytes.size();
      values[k] = bytes.data();
    }
  });
}
fmi3Status fmi3UpdateDiscreteStates(fmi3Instance instance, fmi3Boolean* update,
    fmi3Boolean* terminate, fmi3Boolean* nominals, fmi3Boolean* values,
    fmi3Boolean* next, fmi3Float64* time) {
  return call(instance, [&](Instance& i) {
    require(update && terminate && nominals && values && next && time, "null discrete-state result");
    evaluate(i);
    *update = *terminate = *nominals = *values = *next = false;
    *time = 0;
    i.bus.clear_outputs();
    i.inputs = {};
    i.written = i.rx = i.tx = {};
    i.evaluated = false;
  });
}
fmi3Status fmi3GetIntervalFraction(fmi3Instance instance, const fmi3ValueReference vr[],
    size_t n, fmi3UInt64 counters[], fmi3UInt64 resolutions[], fmi3IntervalQualifier qualifiers[]) {
  return call(instance, [&](Instance& i) {
    require(i.mode == Mode::event || i.mode == Mode::initialization, "interval query requires Event or Initialization Mode");
    require(!n || (vr && counters && resolutions && qualifiers), "null interval result");
    for (size_t k = 0; k < n; ++k) {
      auto node = terminal(vr[k], 3);
      counters[k] = 1; resolutions[k] = 1000;
      qualifiers[k] = i.interval[node];
      if (i.interval[node] == fmi3IntervalChanged) i.interval[node] = fmi3IntervalUnchanged;
    }
  });
}
fmi3Status fmi3GetIntervalDecimal(fmi3Instance instance, const fmi3ValueReference vr[],
    size_t n, fmi3Float64 intervals[], fmi3IntervalQualifier qualifiers[]) {
  return call(instance, [&](Instance& i) {
    require(i.mode == Mode::event || i.mode == Mode::initialization, "interval query requires Event or Initialization Mode");
    require(!n || (vr && intervals && qualifiers), "null interval result");
    for (size_t k = 0; k < n; ++k) {
      auto node = terminal(vr[k], 3);
      intervals[k] = can::Bus::transfer_seconds;
      qualifiers[k] = i.interval[node];
      if (i.interval[node] == fmi3IntervalChanged) i.interval[node] = fmi3IntervalUnchanged;
    }
  });
}
fmi3Status fmi3GetFloat64(fmi3Instance instance, const fmi3ValueReference vr[],
    size_t n, fmi3Float64 values[], size_t count) {
  return call(instance, [&](Instance& i) {
    require(n == count && (!n || (vr && values)), "invalid Float64 output call");
    for (size_t k = 0; k < n; ++k) {
      require(vr[k] == 1024, "unknown Float64 value reference");
      values[k] = i.time;
    }
  });
}
}  // extern "C"
