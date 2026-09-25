// Declared per-instance capacity and measured heap, reported separately.
// Fills one instance to its declared limits through the FMI entry points and
// counts the C++ heap bytes it requests. The count excludes allocator overhead
// and says nothing about process or OS memory isolation. argv[1] receives the
// report as JSON.
#include "bus.hpp"
#include "fmi3Functions.h"
#include "profile.hpp"

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <new>
#include <vector>

namespace {
std::size_t live = 0, peak = 0;

void* allocate(std::size_t size) {
  // Keep the size in front of the block; 16 bytes preserve malloc alignment.
  auto* block = static_cast<std::size_t*>(std::malloc(size + 16));
  if (!block) throw std::bad_alloc();
  block[0] = size;
  live += size;
  peak = std::max(peak, live);
  return block + 2;
}
void release(void* pointer) noexcept {
  if (!pointer) return;
  auto* block = static_cast<std::size_t*>(pointer) - 2;
  live -= block[0];
  std::free(block);
}
}  // namespace

void* operator new(std::size_t size) { return allocate(size); }
void* operator new[](std::size_t size) { return allocate(size); }
void operator delete(void* pointer) noexcept { release(pointer); }
void operator delete[](void* pointer) noexcept { release(pointer); }
void operator delete(void* pointer, std::size_t) noexcept { release(pointer); }
void operator delete[](void* pointer, std::size_t) noexcept { release(pointer); }

namespace {
using Bytes = std::vector<std::uint8_t>;

void require(bool condition, const char* what) {
  if (condition) return;
  std::fprintf(stderr, "capacity: %s\n", what);
  std::abort();
}
void ok(fmi3Status status, const char* what) { require(status == fmi3OK, what); }
void append(Bytes& to, const Bytes& op) { to.insert(to.end(), op.begin(), op.end()); }
Bytes frame(std::uint32_t id) {
  return {0x10, 0, 0, 0, 24, 0, 0, 0, std::uint8_t(id), std::uint8_t(id >> 8), 0, 0,
          0, 0, 8, 0, 1, 2, 3, 4, 5, 6, 7, 8};
}
// An unknown operation whose Format Error report fills the terminal's share.
Bytes largest_corrupt() {
  Bytes op(can::format_error_capacity - 10);
  op[0] = 0xad;
  op[1] = 0xde;
  op[4] = std::uint8_t(op.size());
  op[5] = std::uint8_t(op.size() >> 8);
  return op;
}
void deliver(fmi3Instance instance, unsigned node, const Bytes& data) {
  const fmi3ValueReference clock = profile::terminal_stride * node + profile::rx_clock,
                           binary = profile::terminal_stride * node + profile::rx_data;
  const fmi3Clock active = fmi3True;
  const size_t size = data.size();
  const fmi3Binary value = data.data();
  ok(fmi3SetClock(instance, &clock, 1, &active), "Rx Clock");
  ok(fmi3SetBinary(instance, &binary, 1, &size, &value, 1), "Rx Binary");
}
void update(fmi3Instance instance) {
  fmi3Boolean flags[5];
  fmi3Float64 next;
  ok(fmi3UpdateDiscreteStates(instance, &flags[0], &flags[1], &flags[2], &flags[3],
                              &flags[4], &next),
     "event");
}
}  // namespace

int main(int argc, char** argv) {
  require(argc == 2, "usage: capacity REPORT.json");
  constexpr unsigned nodes = profile::terminal_count;
  // Every buffer is built before the baseline, so only the instance counts.
  std::vector<Bytes> first(nodes), second(nodes), staged(nodes);
  const Bytes rate{0x40, 0, 0, 0, 13, 0, 0, 0, 1, 0x48, 0xe8, 0x01, 0};  // 125 kbit/s
  const Bytes discard{0x40, 0, 0, 0, 10, 0, 0, 0, 4, 2};
  for (unsigned node = 0; node < nodes; ++node) {
    append(first[node], rate);
    for (unsigned k = 0; k < can::max_queue_capacity; ++k)
      append(first[node], frame(1 + node * can::max_queue_capacity + k));
    // The first arbitration puts node 0's head on the wire; refill its queue.
    if (node == 0) append(second[node], frame(1 + nodes * can::max_queue_capacity));
    append(second[node], largest_corrupt());
    while (staged[node].size() + discard.size() <= profile::max_binary_size)
      append(staged[node], discard);
  }
  const std::size_t baseline = live;

  auto instance = fmi3InstantiateCoSimulation("bus", profile::token, nullptr, fmi3False,
                                              fmi3False, fmi3True, fmi3False, nullptr, 0,
                                              nullptr, nullptr, nullptr);
  require(instance, "instantiate");
  const std::size_t instantiated = live - baseline;
  {
    std::vector<fmi3ValueReference> refs{profile::active_nodes, profile::queue_capacity,
                                         profile::fault_retry_limit, profile::fault_rule_count};
    std::vector<fmi3Float64> values{nodes, can::max_queue_capacity, can::max_fault_retries,
                                    can::max_fault_rules};
    for (unsigned rule = 0; rule < can::max_fault_rules; ++rule) {
      // Rules that match no offered request: stored, never consumed.
      const auto base = profile::fault_rule_base + rule * profile::fault_rule_stride;
      const std::pair<unsigned, double> fields[] = {
          {profile::fault_rule_kind, 1}, {profile::fault_rule_sender, 1},
          {profile::fault_rule_receiver, 0}, {profile::fault_rule_identifier, 2047},
          {profile::fault_rule_first_request, 0}, {profile::fault_rule_last_request, 0},
          {profile::fault_rule_occurrence, 1 + rule}, {profile::fault_rule_attempt, 1}};
      for (const auto& [offset, value] : fields) {
        refs.push_back(base + offset);
        values.push_back(value);
      }
    }
    ok(fmi3SetFloat64(instance, refs.data(), refs.size(), values.data(), values.size()),
       "parameters");
  }
  ok(fmi3EnterInitializationMode(instance, fmi3False, 0, 0, fmi3False, 0), "initialization");
  ok(fmi3ExitInitializationMode(instance), "exit initialization");
  const std::size_t configured = live - baseline;

  for (unsigned node = 0; node < nodes; ++node) deliver(instance, node, first[node]);
  update(instance);
  for (unsigned node = 0; node < nodes; ++node) deliver(instance, node, second[node]);
  update(instance);
  for (unsigned node = 0; node < nodes; ++node) deliver(instance, node, staged[node]);
  const std::size_t at_capacity = live - baseline;
  peak = live;
  update(instance);  // copies the bus to commit the event atomically
  const std::size_t event_peak = peak - baseline;
  {  // The measured state is the declared limit: one more frame fails.
    const auto beyond = frame(0x7ff);
    deliver(instance, 1, beyond);
    fmi3Boolean flags[5];
    fmi3Float64 next;
    require(fmi3UpdateDiscreteStates(instance, &flags[0], &flags[1], &flags[2], &flags[3],
                                     &flags[4], &next) == fmi3Error,
            "queues were not full");
  }

  fmi3FreeInstance(instance);
  const std::size_t after_free = live - baseline;
  require(after_free == 0, "free left instance heap behind");

  FILE* out = std::fopen(argv[1], "w");
  require(out, "open report");
  std::fprintf(out,
               "{\n"
               "  \"declared\": {\n"
               "    \"terminals\": %u,\n"
               "    \"pending_frames_per_terminal\": %u,\n"
               "    \"frames_on_wire\": 1,\n"
               "    \"retry_slots_per_terminal\": 1,\n"
               "    \"binary_max_size_bytes\": %u,\n"
               "    \"format_error_report_bytes_per_terminal\": %zu,\n"
               "    \"fault_rules\": %u,\n"
               "    \"automatic_retries\": %u,\n"
               "    \"events_per_instant\": 256\n"
               "  },\n"
               "  \"measured_cxx_heap_bytes\": {\n"
               "    \"method\": \"replaced operator new/delete; requested bytes, no allocator overhead\",\n"
               "    \"instantiated\": %zu,\n"
               "    \"configured_idle\": %zu,\n"
               "    \"queues_reports_and_inputs_full\": %zu,\n"
               "    \"peak_during_event_at_capacity\": %zu,\n"
               "    \"after_free\": %zu\n"
               "  }\n"
               "}\n",
               nodes, can::max_queue_capacity, profile::max_binary_size,
               can::format_error_capacity, can::max_fault_rules, can::max_fault_retries,
               instantiated, configured, at_capacity, event_peak, after_free);
  std::fclose(out);
}
