// Native checks through the exported FMI 3.0 C entry points: lifecycle and
// release of owned state, isolation of concurrent instances, Binary output and
// logging-callback lifetime, and a bounded malformed-input corpus. Built with
// sanitizers by qualify.sh; argv[1] receives the corpus summary as JSON.
#include "fmi3Functions.h"
#include "operations.hpp"
#include "profile.hpp"

#include <algorithm>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstdio>
#include <cstring>
#include <limits>
#include <map>
#include <optional>
#include <source_location>
#include <string>
#include <tuple>
#include <vector>

namespace {
using namespace operations;
using Nanoseconds = std::uint64_t;
constexpr double ns_per_s = 1e9;

std::uint32_t u32(const Bytes& b, std::size_t at) {
  return std::uint32_t(b[at]) | (std::uint32_t(b[at + 1]) << 8) |
         (std::uint32_t(b[at + 2]) << 16) | (std::uint32_t(b[at + 3]) << 24);
}
fmi3ValueReference ref(unsigned node, unsigned member) {
  return profile::terminal_stride * node + member;
}

struct Log {
  std::vector<std::tuple<fmi3Status, std::string, std::string>> entries;
};
void record(fmi3InstanceEnvironment environment, fmi3Status status,
            fmi3String category, fmi3String message) {
  assert(environment && category && message);
  // The strings belong to the FMU and live only for this call; copy them.
  static_cast<Log*>(environment)->entries.emplace_back(status, category, message);
}

// One instance, freed on scope exit whatever state it is in.
class Fmu {
 public:
  explicit Fmu(Log* log = nullptr, const char* token = profile::token)
      : instance_(fmi3InstantiateCoSimulation(
            "bus", token, nullptr, fmi3False, fmi3True, fmi3True, fmi3False,
            nullptr, 0, log, log ? record : nullptr, nullptr)) {}
  ~Fmu() { fmi3FreeInstance(instance_); }
  Fmu(const Fmu&) = delete;
  Fmu& operator=(const Fmu&) = delete;
  fmi3Instance get() const { return instance_; }

  fmi3Status set(fmi3ValueReference vr, double value) {
    return fmi3SetFloat64(instance_, &vr, 1, &value, 1);
  }
  fmi3Status enter(double start = 0) {
    return fmi3EnterInitializationMode(instance_, fmi3False, 0, start, fmi3False, 0);
  }
  fmi3Status start(double at = 0) {
    const auto entered = enter(at);
    return entered == fmi3OK ? fmi3ExitInitializationMode(instance_) : entered;
  }
  fmi3Status deliver(unsigned node, const Bytes& data) {
    const fmi3ValueReference clock = ref(node, profile::rx_clock),
                             binary = ref(node, profile::rx_data);
    const fmi3Clock active = fmi3True;
    if (auto status = fmi3SetClock(instance_, &clock, 1, &active); status != fmi3OK)
      return status;
    const size_t size = data.size();
    const fmi3Binary value = data.data();
    return fmi3SetBinary(instance_, &binary, 1, &size, &value, 1);
  }
  fmi3Status update() {
    fmi3Boolean flags[5];
    fmi3Float64 next = -1;
    const auto status = fmi3UpdateDiscreteStates(instance_, &flags[0], &flags[1], &flags[2],
                                                 &flags[3], &flags[4], &next);
    // The model never asks for another iteration or a time event.
    if (status == fmi3OK) assert(!flags[0] && !flags[1] && !flags[4]);
    return status;
  }
  fmi3Status activate(unsigned nodes) {
    std::vector<fmi3ValueReference> clocks;
    for (unsigned n = 0; n < nodes; ++n) clocks.push_back(ref(n, profile::tx_clock));
    fmi3Clock active[profile::terminal_count];  // fmi3Clock is bool: no vector<bool>
    std::fill_n(active, nodes, fmi3True);
    return fmi3SetClock(instance_, clocks.data(), nodes, active);
  }
  fmi3Status outputs(unsigned nodes, std::vector<Bytes>& result) {
    std::vector<fmi3ValueReference> refs;
    for (unsigned n = 0; n < nodes; ++n) refs.push_back(ref(n, profile::tx_data));
    std::vector<size_t> sizes(nodes);
    std::vector<fmi3Binary> values(nodes);
    const auto status = fmi3GetBinary(instance_, refs.data(), nodes, sizes.data(),
                                      values.data(), nodes);
    result.clear();
    for (unsigned n = 0; status == fmi3OK && n < nodes; ++n)
      result.emplace_back(values[n], values[n] + sizes[n]);
    return status;
  }
  // The countdown state of every active Tx Clock, which must agree.
  fmi3Status interval(unsigned nodes, Nanoseconds& counter,
                      fmi3IntervalQualifier& qualifier) {
    std::vector<fmi3ValueReference> refs;
    for (unsigned n = 0; n < nodes; ++n) refs.push_back(ref(n, profile::tx_clock));
    std::vector<fmi3UInt64> counters(nodes), resolutions(nodes);
    std::vector<fmi3IntervalQualifier> qualifiers(nodes);
    const auto status = fmi3GetIntervalFraction(instance_, refs.data(), nodes, counters.data(),
                                                resolutions.data(), qualifiers.data());
    if (status != fmi3OK) return status;
    for (unsigned n = 0; n < nodes; ++n) {
      assert(counters[n] == counters[0] && qualifiers[n] == qualifiers[0]);
      assert(resolutions[n] == fmi3UInt64(ns_per_s));
    }
    counter = counters[0];
    qualifier = qualifiers[0];
    return status;
  }
  fmi3Status advance(Nanoseconds from, Nanoseconds to) {
    if (auto status = fmi3EnterStepMode(instance_); status != fmi3OK) return status;
    fmi3Boolean event, terminate, early;
    fmi3Float64 last;
    const auto status = fmi3DoStep(instance_, from / ns_per_s, (to - from) / ns_per_s,
                                   fmi3True, &event, &terminate, &early, &last);
    if (status != fmi3OK) return status;
    return fmi3EnterEventMode(instance_);
  }

 private:
  fmi3Instance instance_;
};

void expect(fmi3Status status, fmi3Status expected, std::source_location where) {
  if (status == expected) return;
  std::fprintf(stderr, "%s:%u: expected status %d, got %d\n", where.file_name(),
               unsigned(where.line()), int(expected), int(status));
  std::abort();
}
void ok(fmi3Status status, std::source_location where = std::source_location::current()) {
  expect(status, fmi3OK, where);
}
void failed(fmi3Status status, std::source_location where = std::source_location::current()) {
  expect(status, fmi3Error, where);
}

struct Request {
  Nanoseconds at;
  unsigned node;
  Bytes data;
};
using Trace = std::vector<std::tuple<Nanoseconds, unsigned, Bytes>>;

// An independent event-driven master, advanced one event at a time so that
// several instances can be driven with interleaved calls.
class Master {
 public:
  Master(Fmu& fmu, unsigned nodes, std::vector<Request> requests, Nanoseconds until)
      : fmu_(fmu), nodes_(nodes), requests_(std::move(requests)), until_(until) {}

  // One event at the current instant, then one Step to the next stop.
  bool step() {
    if (done_) return false;
    std::map<unsigned, Bytes> offered;
    for (const auto& request : requests_)
      if (request.at == now_) offered[request.node] = offered[request.node] + request.data;
    for (const auto& [node, data] : offered) ok(fmu_.deliver(node, data));
    if (due_ == now_) {
      ok(fmu_.activate(nodes_));
      std::vector<Bytes> outputs;
      ok(fmu_.outputs(nodes_, outputs));
      for (unsigned n = 0; n < nodes_; ++n)
        if (!outputs[n].empty()) trace_.emplace_back(now_, n, outputs[n]);
    }
    ok(fmu_.update());
    Nanoseconds counter;
    fmi3IntervalQualifier qualifier;
    ok(fmu_.interval(nodes_, counter, qualifier));
    if (qualifier == fmi3IntervalChanged) due_ = now_ + counter;
    else if (qualifier == fmi3IntervalNotYetKnown) due_.reset();
    auto stop = until_;
    for (const auto& request : requests_)
      if (request.at > now_) stop = std::min(stop, request.at);
    if (due_) stop = std::min(stop, *due_);
    if (now_ == until_) {
      done_ = true;
      return false;
    }
    ok(fmu_.advance(now_, stop));
    now_ = stop;
    return true;
  }
  const Trace& run() {
    while (step()) {}
    return trace_;
  }
  const Trace& trace() const { return trace_; }

 private:
  Fmu& fmu_;
  unsigned nodes_;
  std::vector<Request> requests_;
  Nanoseconds until_;
  Nanoseconds now_ = 0;
  std::optional<Nanoseconds> due_;
  Trace trace_;
  bool done_ = false;
};

// Two differently configured buses, one of them with a fault schedule.
struct Scenario {
  unsigned nodes;
  std::vector<std::pair<fmi3ValueReference, double>> parameters;
  std::vector<Request> requests;
  Nanoseconds until;
};
Scenario two_node_burst() {
  return {2, {}, {{0, 0, bitrate(125000)}, {0, 1, bitrate(125000)},
                  {1000, 0, frame(0x10, {1, 2})}, {1000, 1, frame(0x20, {})},
                  {50000, 0, frame(0x30, {3})}},
          3000000};
}
Scenario three_node_faulted() {
  const auto rule = [](unsigned field) {
    return fmi3ValueReference(profile::fault_rule_base + field);
  };
  return {3,
          {{profile::active_nodes, 3}, {profile::queue_capacity, 2},
           {profile::fault_rule_count, 1},
           {rule(profile::fault_rule_kind), 1}, {rule(profile::fault_rule_sender), 2},
           {rule(profile::fault_rule_receiver), 0}, {rule(profile::fault_rule_identifier), 5},
           {rule(profile::fault_rule_first_request), 5000},
           {rule(profile::fault_rule_last_request), 5000},
           {rule(profile::fault_rule_occurrence), 1}, {rule(profile::fault_rule_attempt), 1}},
          {{0, 0, bitrate(500000)}, {0, 1, bitrate(500000)}, {0, 2, bitrate(500000) + discard()},
           {5000, 1, frame(5, {9})}, {5000, 2, frame(3, {})}, {5000, 0, frame(7, {})},
           {7000, 0, Bytes{0xad, 0xde, 0, 0, 8, 0, 0, 0}}},
          2000000};
}
void configure(Fmu& fmu, const Scenario& scenario) {
  for (const auto& [vr, value] : scenario.parameters) ok(fmu.set(vr, value));
  ok(fmu.start());
}
Trace alone(const Scenario& scenario) {
  Fmu fmu;
  configure(fmu, scenario);
  Master master(fmu, scenario.nodes, scenario.requests, scenario.until);
  return master.run();
}

void instances_are_isolated() {
  const auto a = two_node_burst(), b = three_node_faulted();
  const auto expected_a = alone(a), expected_b = alone(b);
  assert(expected_a.size() == 6 && expected_b.size() >= 9);
  // Interleave every call of three live instances: A, B, and a third one
  // driven into Error state. Queues, fault state, clocks and counters of one
  // must not reach another.
  Fmu first, second, broken;
  configure(first, a);
  configure(second, b);
  configure(broken, a);
  Master ma(first, a.nodes, a.requests, a.until), mb(second, b.nodes, b.requests, b.until);
  bool broken_live = true;
  for (bool more = true; more;) {
    more = ma.step();
    if (broken_live) {
      ok(broken.deliver(0, bitrate(125000) + frame(1, {}) + frame(1, {}) + frame(1, {}) +
                                frame(1, {}) + frame(1, {}) + frame(1, {})));
      failed(broken.update());  // five pending frames exceed the default queue of 4
      failed(broken.update());  // Error state is sticky
      broken_live = false;
    }
    more = mb.step() || more;
  }
  assert(ma.trace() == expected_a);
  assert(mb.trace() == expected_b);
  // The broken instance recovers by reset, independently of its peers.
  ok(fmi3Reset(broken.get()));
  configure(broken, a);
  assert(Master(broken, a.nodes, a.requests, a.until).run() == expected_a);
}

void lifecycle_releases_state() {
  // Unsupported instantiations return null rather than a partial instance.
  assert(!Fmu(nullptr, "other-token").get());
  assert(!fmi3InstantiateCoSimulation("bus", profile::token, nullptr, fmi3False, fmi3False,
                                      fmi3False, fmi3False, nullptr, 0, nullptr, nullptr,
                                      nullptr));
  assert(!fmi3InstantiateCoSimulation("", profile::token, nullptr, fmi3False, fmi3False,
                                      fmi3True, fmi3False, nullptr, 0, nullptr, nullptr,
                                      nullptr));
  fmi3FreeInstance(nullptr);
  const auto scenario = two_node_burst();
  const auto expected = alone(scenario);
  for (unsigned cycle = 0; cycle < 200; ++cycle) {
    {  // Normal termination, then free.
      Fmu fmu;
      configure(fmu, scenario);
      assert(Master(fmu, 2, scenario.requests, scenario.until).run() == expected);
      ok(fmi3Terminate(fmu.get()));
      failed(fmi3Terminate(fmu.get()));
    }
    {  // Initialization failure: a declared rule whose fields were never set.
      Fmu fmu;
      ok(fmu.set(profile::fault_rule_count, 1));
      ok(fmu.enter());
      failed(fmi3ExitInitializationMode(fmu.get()));
    }
    {  // Free with traffic queued, on the wire and a report due.
      Fmu fmu;
      configure(fmu, scenario);
      ok(fmu.deliver(0, bitrate(125000) + frame(1, {1}) + frame(2, {2}) +
                            Bytes{0xad, 0xde, 0, 0, 8, 0, 0, 0}));
      ok(fmu.deliver(1, bitrate(125000) + frame(3, {3})));
      ok(fmu.update());
    }
    {  // Reset from every state restores the fresh behavior.
      Fmu fmu;
      for (unsigned state = 0; state < 4; ++state) {
        ok(fmi3Reset(fmu.get()));
        if (state >= 1) configure(fmu, scenario);
        if (state >= 2) ok(fmu.deliver(0, frame(1, {})));
        if (state >= 3) failed(fmu.update());  // unconfigured bitrate: Error state
      }
      ok(fmi3Reset(fmu.get()));
      configure(fmu, scenario);
      assert(Master(fmu, 2, scenario.requests, scenario.until).run() == expected);
    }
  }
}

void output_and_callback_lifetime() {
  Log log;
  {
    Fmu fmu(&log);
    ok(fmu.start());
    ok(fmu.deliver(0, bitrate(125000) + frame(0, {})));
    ok(fmu.deliver(1, bitrate(125000)));
    ok(fmu.update());
    ok(fmu.advance(0, 400000));
    ok(fmu.activate(2));
    const fmi3ValueReference refs[] = {ref(0, profile::tx_data), ref(1, profile::tx_data)};
    size_t sizes[2];
    fmi3Binary values[2];
    ok(fmi3GetBinary(fmu.get(), refs, 2, sizes, values, 2));
    const Bytes confirmation(values[0], values[0] + sizes[0]);
    const Bytes delivered(values[1], values[1] + sizes[1]);
    assert(confirmation.size() == 12 && delivered == frame(0, {}));
    // The FMU owns the buffers; they stay valid and unchanged across further
    // calls of this instance until the update that ends the activation.
    size_t again_sizes[2];
    fmi3Binary again[2];
    ok(fmi3GetBinary(fmu.get(), refs, 2, again_sizes, again, 2));
    assert(again[0] == values[0] && again[1] == values[1]);
    Nanoseconds counter;
    fmi3IntervalQualifier qualifier;
    ok(fmu.interval(2, counter, qualifier));
    fmi3Float64 time;
    const fmi3ValueReference time_ref = profile::time;
    ok(fmi3GetFloat64(fmu.get(), &time_ref, 1, &time, 1));
    assert(Bytes(values[0], values[0] + sizes[0]) == confirmation);
    assert(Bytes(values[1], values[1] + sizes[1]) == delivered);
    ok(fmu.update());
    assert(log.entries.empty());  // successful calls log nothing
    failed(fmu.deliver(3, frame(0, {})));  // inactive terminal
    assert(log.entries.size() == 1);
    const auto& [status, category, message] = log.entries[0];
    assert(status == fmi3Error && category == "error" && !message.empty());
    failed(fmu.update());
    assert(log.entries.size() == 2);
  }
  // A null callback is permitted; errors are then only statuses.
  Fmu silent;
  failed(silent.deliver(0, frame(0, {})));
}

void invalid_arguments() {
  Fmu fmu;
  const fmi3ValueReference vr = ref(0, profile::rx_data);
  const double nan = std::numeric_limits<double>::quiet_NaN();
  // Every implemented entry point refuses a null instance.
  failed(fmi3EnterInitializationMode(nullptr, fmi3False, 0, 0, fmi3False, 0));
  failed(fmi3Reset(nullptr));
  failed(fmi3SetFloat64(nullptr, &vr, 0, nullptr, 0));
  failed(fmi3UpdateDiscreteStates(nullptr, nullptr, nullptr, nullptr, nullptr, nullptr,
                                   nullptr));
  // Unsupported capabilities fail explicitly and never touch their arguments.
  failed(fmi3GetFMUState(fmu.get(), nullptr));
  failed(fmi3SetInt32(fmu.get(), &vr, 1, nullptr, 1));
  failed(fmi3EnterConfigurationMode(fmu.get()));
  for (double value : {nan, -1.0, 1.5, 5.0, 1e300, std::numeric_limits<double>::infinity()}) {
    Fmu parameter;
    failed(parameter.set(profile::active_nodes, value));
  }
  for (double start : {nan, -1e-9, 2e6, std::numeric_limits<double>::infinity()}) {
    Fmu timed;
    failed(timed.enter(start));
  }
  {
    Fmu counts;
    const double value = 2;
    failed(fmi3SetFloat64(counts.get(), &vr, 1, &value, 2));  // n != count
  }
  {
    Fmu nulls;
    failed(fmi3SetFloat64(nulls.get(), nullptr, 1, nullptr, 1));
  }
  {
    Fmu unknown;
    failed(unknown.set(99999, 1));
  }
  {
    Fmu started;
    ok(started.start());
    const Bytes oversized(profile::max_binary_size + 1);
    failed(started.deliver(0, oversized));
  }
  {
    Fmu started;
    ok(started.start());
    const fmi3Clock active = fmi3True;
    const fmi3ValueReference unknown_clock = 4 * profile::terminal_stride + profile::rx_clock;
    failed(fmi3SetClock(started.get(), &unknown_clock, 1, &active));
  }
  {
    Fmu started;
    ok(started.start());
    size_t size = 1;
    const fmi3Binary value = nullptr;  // non-empty size with no data
    const fmi3Clock active = fmi3True;
    const fmi3ValueReference clock = ref(0, profile::rx_clock);
    ok(fmi3SetClock(started.get(), &clock, 1, &active));
    failed(fmi3SetBinary(started.get(), &vr, 1, &size, &value, 1));
  }
  {
    Fmu stepping;
    ok(stepping.start());
    ok(fmi3EnterStepMode(stepping.get()));
    fmi3Boolean event, terminate, early;
    fmi3Float64 last;
    failed(fmi3DoStep(stepping.get(), 0, nan, fmi3True, &event, &terminate, &early, &last));
  }
  {
    Fmu stepping;
    ok(stepping.start());
    ok(fmi3EnterStepMode(stepping.get()));
    failed(fmi3DoStep(stepping.get(), 0, 1e-3, fmi3True, nullptr, nullptr, nullptr, nullptr));
  }
}

void same_instant_events_are_bounded() {
  Fmu fmu;
  ok(fmu.start());
  ok(fmu.deliver(0, bitrate(125000) + frame(0, {})));
  ok(fmu.deliver(1, bitrate(125000)));
  ok(fmu.update());
  // Repeated activations at one instant: each Transmit queues behind the
  // frame on the wire until the default queue capacity of 4 is exhausted.
  for (unsigned request = 0; request < 4; ++request) {
    ok(fmu.deliver(0, frame(1 + request, {})));
    ok(fmu.update());
  }
  Fmu events;
  ok(events.start());
  for (unsigned event = 0; event < 256; ++event) ok(events.update());
  failed(events.update());  // the 257th event of one instant
  Fmu stepped;
  ok(stepped.start());
  for (unsigned event = 0; event < 256; ++event) ok(stepped.update());
  ok(stepped.advance(0, 1000));  // a new instant restarts the bound
  for (unsigned event = 0; event < 256; ++event) ok(stepped.update());
  failed(stepped.update());
  ok(fmu.deliver(0, frame(9, {})));
  failed(fmu.update());  // a fifth pending frame on one node
}

// Each Tx output is a whole sequence of operations the model produces.
void require_well_formed_outputs(const std::vector<Bytes>& outputs) {
  for (const auto& output : outputs) {
    assert(output.size() <= profile::max_binary_size);
    for (std::size_t at = 0; at < output.size();) {
      assert(output.size() - at >= 8);
      const auto code = u32(output, at), length = u32(output, at + 4);
      assert(code == 0x01 || code == 0x10 || code == 0x20 || code == 0x30 || code == 0x31);
      assert(length >= 8 && length <= output.size() - at);
      at += length;
    }
  }
}

std::vector<Bytes> corpus() {
  const Bytes unknown{0xad, 0xde, 0, 0, 8, 0, 0, 0};
  const std::vector<Bytes> seeds{bitrate(125000), discard(), frame(1, {1, 2, 3, 4}),
                                 frame(0x7ff, Bytes(8, 0xff)), unknown,
                                 frame(2, {}) + discard() + frame(3, {7})};
  std::vector<Bytes> cases;
  for (const auto& seed : seeds) {
    cases.push_back(seed);
    for (std::size_t size = 0; size < seed.size(); ++size)
      cases.emplace_back(seed.begin(), seed.begin() + size);
    for (std::size_t at = 0; at < seed.size(); ++at)
      for (std::uint8_t value : {0x00, 0x01, 0x02, 0x07, 0x08, 0x10, 0x7f, 0x80, 0xff}) {
        auto mutated = seed;
        mutated[at] = value;
        cases.push_back(mutated);
      }
    for (std::uint32_t length : {0u, 7u, 8u, std::uint32_t(seed.size() - 1),
                                 std::uint32_t(seed.size() + 1), 0xffffffffu}) {
      auto mutated = seed;
      std::memcpy(mutated.data() + 4, &length, 4);  // the little-endian host of the profile
      cases.push_back(mutated);
    }
  }
  return cases;
}

void malformed_corpus(const char* summary) {
  unsigned accepted = 0, reported = 0, rejected = 0;
  const auto cases = corpus();
  for (const auto& input : cases) {
    Fmu fmu;
    ok(fmu.start());
    ok(fmu.deliver(1, bitrate(125000)));
    ok(fmu.deliver(0, bitrate(125000) + input));
    if (fmu.update() != fmi3OK) {
      ++rejected;
      failed(fmu.update());  // sticky until reset
      ok(fmi3Reset(fmu.get()));
      ok(fmu.start());
      continue;
    }
    // Drive every bus event the input caused until the bus is idle.
    bool format_error = false;
    Nanoseconds now = 0, counter;
    fmi3IntervalQualifier qualifier;
    ok(fmu.interval(2, counter, qualifier));
    while (qualifier != fmi3IntervalNotYetKnown) {
      ok(fmu.advance(now, now + counter));
      now += counter;
      ok(fmu.activate(2));
      std::vector<Bytes> outputs;
      ok(fmu.outputs(2, outputs));
      require_well_formed_outputs(outputs);
      format_error = format_error || (!outputs[0].empty() && outputs[0][0] == 0x01);
      ok(fmu.update());
      ok(fmu.interval(2, counter, qualifier));
    }
    ++(format_error ? reported : accepted);
  }
  assert(accepted && reported && rejected);
  if (FILE* out = std::fopen(summary, "w")) {
    std::fprintf(out,
                 "{\n  \"cases\": %zu,\n  \"accepted\": %u,\n  \"format_error_reported\": %u,\n"
                 "  \"rejected_fmi3Error\": %u\n}\n",
                 cases.size(), accepted, reported, rejected);
    std::fclose(out);
  }
}
}  // namespace

int main(int argc, char** argv) {
  assert(argc == 2);
  lifecycle_releases_state();
  instances_are_isolated();
  output_and_callback_lifetime();
  invalid_arguments();
  same_instant_events_are_bounded();
  malformed_corpus(argv[1]);
}
