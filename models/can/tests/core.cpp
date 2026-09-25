#include "bus.hpp"
#include "operations.hpp"

#include <cassert>
#include <exception>

namespace {
using namespace operations;
can::Bytes confirm(std::uint32_t id) {
  return {0x20, 0, 0, 0, 12, 0, 0, 0, std::uint8_t(id), std::uint8_t(id >> 8), 0, 0};
}
can::Bytes bus_error(std::uint32_t id, std::uint8_t flag, bool sender) {
  return {0x31, 0, 0, 0, 15, 0, 0, 0, std::uint8_t(id),
          std::uint8_t(id >> 8), 0, 0, 1, flag, std::uint8_t(sender)};
}
can::Bytes format_error(const can::Bytes& op) {
  const auto length = std::uint32_t(10 + op.size());
  can::Bytes report{0x01, 0, 0, 0, std::uint8_t(length), std::uint8_t(length >> 8), 0, 0,
                    std::uint8_t(op.size()), std::uint8_t(op.size() >> 8)};
  report.insert(report.end(), op.begin(), op.end());
  return report;
}
void send(can::Bus& bus, unsigned terminal, const can::Bytes& operations, can::Nanoseconds now) {
  can::Inputs inputs;
  inputs[terminal] = operations;
  bus.receive(inputs, now);
}
template<class F> bool rejects(F body) {
  try {
    body();
  } catch (const std::exception&) {
    return true;
  }
  return false;
}
can::Bus configured(std::uint32_t rate) {
  can::Bus bus;
  send(bus, 0, bitrate(rate), 0);
  send(bus, 1, bitrate(rate), 0);
  return bus;
}

void wire_lengths() {
  // Hand-derived in README.md: ID 0 with all-zero data.
  assert(can::frame_bits(0, {}) == 50);
  assert(can::frame_bits(0, can::Bytes(1)) == 56);
  assert(can::frame_bits(0, can::Bytes(8)) == 124);
  // Cross-checked against tests/wire.py.
  assert(can::frame_bits(1, can::Bytes{1, 2, 3, 4}) == 83);
  assert(can::frame_bits(can::max_classical_identifier, can::Bytes(8, 0xff)) == 123);
  assert(can::frame_bits(0x555, can::Bytes{0, 1, 2, 3, 4, 5, 6, 7}) == 117);
}

void burst_and_mid_transmission_request() {
  auto bus = configured(500000);  // 2000 ns per bit
  const auto a = frame(0, can::Bytes(8)), b = frame(can::max_classical_identifier, can::Bytes(8, 0xff)),
             c = frame(1, {1, 2, 3, 4});
  send(bus, 0, a, 1000);
  assert(bus.next_event() == 1000 + 124 * 2000);
  send(bus, 0, b, 50000);  // waits: an in-progress frame is never preempted
  assert(bus.next_event() == 249000);
  bus.complete(249000);
  assert(bus.outputs()[0] == confirm(0) && bus.outputs()[1] == a);
  bus.clear_outputs();
  const auto b_end = 249000 + 3 * 2000 + 123 * 2000;
  assert(bus.next_event() == 255000);
  bus.receive({}, 255000);
  assert(bus.next_event() == b_end);
  send(bus, 1, c, 300000);  // arrives while b is on the wire
  bus.complete(b_end);
  assert(bus.outputs()[0] == confirm(can::max_classical_identifier) && bus.outputs()[1] == b);
  bus.clear_outputs();
  const auto c_end = b_end + 3 * 2000 + 83 * 2000;
  assert(bus.next_event() == 507000);
  bus.receive({}, 507000);
  assert(bus.next_event() == c_end);
  assert(rejects([&] { bus.complete(c_end - 1); }));
  bus.complete(c_end);
  assert(bus.outputs()[0] == c && bus.outputs()[1] == confirm(1));
  assert(!bus.next_event());
}

void arbitration_opportunities() {
  const auto a = frame(0, {}), b = frame(1, {});
  {  // Arrival at a frame end, before its completion: waits for intermission.
    auto bus = configured(125000);  // 8000 ns per bit
    send(bus, 0, a, 0);
    send(bus, 1, b, 400000);
    bus.complete(400000);
    assert(bus.next_event() == 424000);
    bus.receive({}, 424000);
    assert(bus.next_event() == 424000 + 47 * 8000);
  }
  {  // Arrival during intermission starts when it ends; later on arrival.
    auto bus = configured(125000);
    send(bus, 0, a, 0);
    bus.complete(400000);
    send(bus, 1, b, 410000);
    assert(bus.next_event() == 424000);
    bus.receive({}, 424000);
    assert(bus.next_event() == 424000 + 47 * 8000);
    bus.complete(424000 + 47 * 8000);
    send(bus, 0, a, 900000);
    assert(bus.next_event() == 1300000);
  }
}

void contention_and_queues() {
  can::Bus bus;
  bus.configure(3, 2);
  can::Inputs configuration;
  const auto rate = bitrate(125000);
  for (unsigned n = 0; n < 3; ++n) configuration[n] = rate;
  bus.receive(configuration, 0);
  const auto low = frame(0x300, {3}), medium = frame(0x200, {2}),
             high = frame(0x100, {1}), urgent = frame(0, {});
  can::Inputs simultaneous;
  simultaneous[0] = low;
  simultaneous[1] = high;
  simultaneous[2] = medium;
  bus.receive(simultaneous, 1000);
  const auto high_end = 1000 + can::Nanoseconds(can::frame_bits(0x100, can::Bytes{1})) * 8000;
  assert(bus.next_event() == high_end);
  send(bus, 0, urgent, 2000);  // cannot preempt the frame already on the wire
  bus.complete(high_end);
  assert(bus.outputs()[0] == high && bus.outputs()[1] == confirm(0x100) &&
         bus.outputs()[2] == high);
  bus.clear_outputs();
  const auto next = high_end + 24000;
  bus.receive({}, next);
  const auto medium_end = next + can::Nanoseconds(can::frame_bits(0x200, can::Bytes{2})) * 8000;
  assert(bus.next_event() == medium_end);  // Node1 FIFO hides its later ID 0.
  bus.complete(medium_end);
  assert(bus.outputs()[2] == confirm(0x200));
  bus.clear_outputs();
  bus.receive({}, medium_end + 24000);
  const auto low_end = medium_end + 24000 + can::Nanoseconds(can::frame_bits(0x300, can::Bytes{3})) * 8000;
  bus.complete(low_end);
  assert(bus.outputs()[0] == confirm(0x300));
  bus.clear_outputs();
  bus.receive({}, low_end + 24000);
  const auto urgent_end = low_end + 24000 + can::Nanoseconds(can::frame_bits(0, {})) * 8000;
  bus.complete(urgent_end);
  assert(bus.outputs()[0] == confirm(0));
  assert(!bus.next_event());
}

void discard_equal_id_and_bounds() {
  auto bus = configured(125000);
  send(bus, 1, discard(), 0);
  const auto a = frame(0, {}), b = frame(1, {});
  can::Inputs inputs;
  inputs[0] = a;
  inputs[1] = b;
  bus.receive(inputs, 0);
  bus.complete(400000);
  can::Bytes lost{0x30, 0, 0, 0, 12, 0, 0, 0, 1, 0, 0, 0};
  lost.insert(lost.end(), a.begin(), a.end());
  assert(bus.outputs()[0] == confirm(0) && bus.outputs()[1] == lost);
  assert(!bus.next_event());
  auto identical = configured(125000);
  inputs[0] = a;
  inputs[1] = a;
  identical.receive(inputs, 0);
  identical.complete(400000);
  assert(identical.outputs()[0] == confirm(0) && identical.outputs()[1] == confirm(0));
  auto different = configured(125000);
  const auto conflicting = frame(0, {1});
  inputs[1] = conflicting;
  assert(rejects([&] { different.receive(inputs, 0); }));
  assert(!different.next_event());
  {  // Different data under an ID that loses to a lower ID is never sent.
    can::Bus discarded;
    discarded.configure(3, 2);
    const auto rate = bitrate(125000), first = frame(1, {1}), second = frame(1, {2});
    can::Inputs configs;
    configs[0] = configs[1] = configs[2] = rate;
    discarded.receive(configs, 0);
    send(discarded, 0, discard(), 0);
    send(discarded, 1, discard(), 0);
    can::Inputs offered;
    offered[0] = first;
    offered[1] = second;
    offered[2] = a;
    discarded.receive(offered, 0);
    discarded.complete(400000);
    assert(discarded.outputs()[0] == lost && discarded.outputs()[1] == lost);
    assert(discarded.outputs()[2] == confirm(0));
    assert(!discarded.next_event());
  }
  {  // Buffered equal-ID conflict is rejected when that ID next wins.
    can::Bus buffered;
    buffered.configure(3, 2);
    const auto rate = bitrate(125000), first = frame(1, {1}), second = frame(1, {2});
    can::Inputs configs;
    configs[0] = configs[1] = configs[2] = rate;
    buffered.receive(configs, 0);
    can::Inputs offered;
    offered[0] = first;
    offered[1] = second;
    offered[2] = a;
    buffered.receive(offered, 0);
    buffered.complete(400000);
    assert(rejects([&] { buffered.receive({}, 424000); }));
    assert(buffered.next_event() == 424000);
  }
  auto one = configured(125000);
  one.configure(1, 1);
  assert(rejects([&] { send(one, 1, a, 0); }));
  send(one, 0, a, 0);
  send(one, 0, b, 1);
  assert(rejects([&] { send(one, 0, b, 2); }));  // pending capacity after wire frame
  assert(!one.outputs()[1].size());
}

void finite_priority_stream() {
  auto bus = configured(125000);
  const auto high = frame(0, {}), low = frame(1, {});
  can::Inputs first;
  first[0] = high;
  first[1] = low;
  bus.receive(first, 0);
  can::Nanoseconds end = 400000;
  for (unsigned round = 0; round < 3; ++round) {
    // Each newly queued high-priority head wins the next opportunity.
    if (round < 2) send(bus, 0, high, end);
    bus.complete(end);
    assert(bus.outputs()[0] == confirm(0) && bus.outputs()[1] == high);
    bus.clear_outputs();
    bus.receive({}, end + 24000);
    end += 424000;
  }
  assert(bus.next_event() == end - 424000 + 24000 + 47 * 8000);
  const auto low_end = *bus.next_event();
  bus.complete(low_end);
  assert(bus.outputs()[0] == low && bus.outputs()[1] == confirm(1));
  assert(!bus.next_event());
}

can::FaultRuleInput transmission_error(unsigned sender_node, std::uint32_t id,
                                       can::Nanoseconds request_time,
                                       std::uint64_t occurrence, unsigned attempt) {
  return {std::uint64_t(can::FaultKind::transmission_error), sender_node,
          0, id, std::uint64_t(request_time), std::uint64_t(request_time),
          occurrence, attempt};
}

void scheduled_error_retry_and_exhaustion() {
  const auto request = frame(1, {1, 2, 3, 4});
  const can::Nanoseconds first_request = 300000000;
  const can::Nanoseconds bit_time = 10000;
  const can::Nanoseconds duration =
      can::frame_bits(1, std::array<std::uint8_t, 4>{1, 2, 3, 4}) * bit_time;
  const can::Nanoseconds first_end = first_request + duration;
  const can::Nanoseconds retry_start = first_end + can::intermission_bits * bit_time;
  const can::Nanoseconds retry_end = retry_start + duration;
  const auto first_error = transmission_error(1, 1, first_request, 1, 1);
  {
    can::Bus bus;
    const std::array rules{first_error};
    bus.configure(2, 4, 1, rules.size(), rules);
    can::Inputs configuration;
    const auto rate = bitrate(100000);
    configuration[0] = configuration[1] = rate;
    bus.receive(configuration, 0);
    send(bus, 0, request, first_request);
    assert(bus.next_event() == first_end);
    bus.complete(first_end);
    assert(bus.outputs()[0] == bus_error(1, 1, true));
    assert(bus.outputs()[1] == bus_error(1, 2, false));
    bus.clear_outputs();
    bus.receive({}, retry_start);
    assert(bus.next_event() == retry_end);
    bus.complete(retry_end);
    assert(bus.outputs()[0] == confirm(1));
    assert(bus.outputs()[1] == request);
    assert(!bus.next_event());
  }
  {
    can::Bus bus;
    const std::array rules{
        first_error,
        transmission_error(1, 1, first_request, 1, 2),
    };
    bus.configure(2, 4, 1, rules.size(), rules);
    can::Inputs configuration;
    const auto rate = bitrate(100000);
    configuration[0] = configuration[1] = rate;
    bus.receive(configuration, 0);
    send(bus, 0, request, first_request);
    bus.complete(first_end);
    assert(bus.outputs()[0] == bus_error(1, 1, true));
    bus.clear_outputs();
    bus.receive({}, retry_start);
    bus.complete(retry_end);
    assert(bus.outputs()[0] == bus_error(1, 1, true));
    assert(bus.outputs()[1] == bus_error(1, 2, false));
    assert(!bus.next_event());  // the second error exhausts the retry bound
  }
}

void receiver_delivery_suppression_is_not_a_bus_error() {
  can::Bus bus;
  const can::FaultRuleInput rule{
      std::uint64_t(can::FaultKind::receiver_delivery_suppression),
      1, 2, 1, 1000, 1000, 1, 1};
  const std::array rules{rule};
  bus.configure(2, 4, 1, rules.size(), rules);
  can::Inputs configuration;
  const auto rate = bitrate(100000);
  configuration[0] = configuration[1] = rate;
  bus.receive(configuration, 0);
  send(bus, 0, frame(1, {1, 2, 3, 4}), 1000);
  bus.complete(1000 + can::frame_bits(1, std::array<std::uint8_t, 4>{1, 2, 3, 4}) * 10000);
  assert(bus.outputs()[0] == confirm(1));
  assert(bus.outputs()[1].empty());
  assert(!bus.next_event());
}

void discard_policy_applies_when_a_retry_loses_arbitration() {
  can::Bus bus;
  const auto request = frame(1, {1, 2, 3, 4});
  const can::Nanoseconds bit_time = 10000;
  const can::Nanoseconds request_time = 1000;
  const can::Nanoseconds first_end = request_time +
      can::frame_bits(1, std::array<std::uint8_t, 4>{1, 2, 3, 4}) * bit_time;
  const can::Nanoseconds retry_start =
      first_end + can::intermission_bits * bit_time;
  const can::FaultRuleInput rule = transmission_error(1, 1, request_time, 1, 1);
  const std::array rules{rule};
  bus.configure(2, 4, 1, rules.size(), rules);

  auto node1_configuration = bitrate(100000);
  const auto discard_policy = discard();
  node1_configuration.insert(node1_configuration.end(),
                             discard_policy.begin(), discard_policy.end());
  can::Inputs configurations;
  configurations[0] = node1_configuration;
  const auto node2_configuration = bitrate(100000);
  configurations[1] = node2_configuration;
  bus.receive(configurations, 0);
  send(bus, 0, request, request_time);
  bus.complete(first_end);
  assert(bus.outputs()[0] == bus_error(1, 1, true));
  bus.clear_outputs();

  const auto higher_priority_request = frame(0, {});
  send(bus, 1, higher_priority_request, first_end + 1);
  assert(bus.next_event() == retry_start);
  bus.receive({}, retry_start);
  const can::Nanoseconds winner_end = retry_start +
      can::frame_bits(0, std::span<const std::uint8_t>{}) * bit_time;
  bus.complete(winner_end);
  can::Bytes expected_loser{0x30, 0, 0, 0, 12, 0, 0, 0, 1, 0, 0, 0};
  expected_loser.insert(expected_loser.end(), higher_priority_request.begin(),
                        higher_priority_request.end());
  assert(bus.outputs()[0] == expected_loser);
  assert(bus.outputs()[1] == confirm(0));
  assert(!bus.next_event());
}

void four_declared_terminals() {
  can::Bus bus;
  assert(rejects([&] { bus.configure(0, 1); }));
  assert(rejects([&] { bus.configure(5, 1); }));
  assert(rejects([&] { bus.configure(4, 0); }));
  assert(rejects([&] { bus.configure(4, 65); }));
  bus.configure(4, 1);
  const auto rate = bitrate(125000), op = frame(0, {});
  can::Inputs configs;
  for (auto& input : configs) input = rate;
  bus.receive(configs, 0);
  send(bus, 3, op, 0);
  bus.complete(400000);
  for (unsigned node = 0; node < 3; ++node) assert(bus.outputs()[node] == op);
  assert(bus.outputs()[3] == confirm(0));
}

void timing_configuration() {
  for (std::uint32_t rate : {0u, 9999u, 83333u, 1000001u, 2000000u})
    assert(rejects([&] { can::Bus fresh; send(fresh, 0, bitrate(rate), 0); }));
  for (std::uint32_t rate : {10000u, 125000u, 800000u, 1000000u}) configured(rate);
  auto bus = configured(500000);
  send(bus, 1, bitrate(500000), 0);
  assert(rejects([&] { send(bus, 1, bitrate(250000), 0); }));
  assert(rejects([&] { can::Bus fresh; send(fresh, 0, frame(1, {}), 0); }));
  {  // A terminal that has not agreed on the bitrate blocks transmission.
    can::Bus half;
    send(half, 0, bitrate(125000), 0);
    assert(rejects([&] { send(half, 0, frame(1, {}), 0); }));
    assert(rejects([&] { send(half, 1, bitrate(500000), 0); }));
  }
  // One event commits all inputs: a peer's configuration in the same event
  // counts, whichever terminal transmits.
  for (unsigned sender : {0u, 1u}) {
    can::Bus event;
    can::Bytes transmit = bitrate(125000), peer = bitrate(125000);
    const auto op = frame(1, {});
    transmit.insert(transmit.end(), op.begin(), op.end());
    can::Inputs inputs;
    inputs[sender] = transmit;
    inputs[1 - sender] = peer;
    event.receive(inputs, 0);
    assert(event.next_event() == 47 * 8000);
  }
  assert(rejects([&] { send(bus, 0, frame(1, {}), -1); }));
  assert(rejects([&] { send(bus, 0, frame(1, {}), can::max_time); }));
  send(bus, 0, frame(1, {}), can::max_time - 1000000);
  assert(rejects([&] { bus.complete(can::max_time); }));
}

// Every bus event the adapter would activate, until the bus is idle.
void drain(can::Bus& bus) {
  while (auto due = bus.next_event()) {
    bus.tick(*due);
    for (const auto& output : bus.outputs()) assert(output.size() <= can::max_operation_buffer);
    bus.clear_outputs();
    bus.receive({}, *due);
  }
}

void corrupt_operations_get_format_errors() {
  const can::Bytes unknown{0xad, 0xde, 0, 0, 8, 0, 0, 0};
  {  // A corrupt operation with a sound length is reported and skipped.
    auto bus = configured(125000);
    send(bus, 0, unknown + frame(1, {}), 1000);
    assert(bus.next_event() == 1001);
    bus.tick(1001);
    assert(bus.outputs()[0] == format_error(unknown) && bus.outputs()[1].empty());
    bus.clear_outputs();
    assert(bus.next_event() == 1000 + 47 * 8000);
  }
  auto transmit = frame(1, {1});
  auto id_beyond_standard = transmit, long_payload = frame(1, can::Bytes(9)),
       ide_not_boolean = transmit, rtr_not_boolean = transmit,
       length_mismatch = transmit;
  id_beyond_standard[9] = 0x08;
  ide_not_boolean[12] = 2;
  rtr_not_boolean[13] = 2;
  length_mismatch[14] = 2;
  const can::Bytes no_kind{0x40, 0, 0, 0, 8, 0, 0, 0},
      unknown_kind{0x40, 0, 0, 0, 9, 0, 0, 0, 9},
      short_bitrate{0x40, 0, 0, 0, 12, 0, 0, 0, 1, 0x48, 0xe8, 0x01},
      undefined_policy{0x40, 0, 0, 0, 10, 0, 0, 0, 4, 3},
      short_confirm{0x20, 0, 0, 0, 8, 0, 0, 0},
      long_status{0x41, 0, 0, 0, 10, 0, 0, 0, 0, 0},
      short_fd_bitrate{0x40, 0, 0, 0, 9, 0, 0, 0, 2},
      // CAN FD: ID, IDE, BRS, ESI, DL; CAN XL: ID, IDE, SEC, SDT, VCID, AF, DL.
      fd_ide_not_boolean{0x11, 0, 0, 0, 17, 0, 0, 0, 1, 0, 0, 0, 2, 0, 0, 0, 0},
      fd_brs_not_boolean{0x11, 0, 0, 0, 17, 0, 0, 0, 1, 0, 0, 0, 0, 2, 0, 0, 0},
      fd_invalid_length{0x11, 0, 0, 0, 26, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 9, 0,
                        0, 0, 0, 0, 0, 0, 0, 0},
      xl_sec_not_boolean{0x12, 0, 0, 0, 23, 0, 0, 0, 1, 0, 0, 0, 0, 2, 0, 0,
                         0, 0, 0, 0, 1, 0, 7},
      xl_without_data{0x12, 0, 0, 0, 22, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0,
                      0, 0, 0, 0, 0, 0};
  for (const auto& op : {unknown, id_beyond_standard, long_payload, ide_not_boolean,
                         rtr_not_boolean, length_mismatch, no_kind, unknown_kind,
                         short_bitrate, undefined_policy, short_confirm, long_status,
                         short_fd_bitrate, fd_ide_not_boolean, fd_brs_not_boolean,
                         fd_invalid_length, xl_sec_not_boolean, xl_without_data}) {
    can::Bus fresh;  // Reporting needs no agreed bitrate.
    send(fresh, 0, op, 0);
    assert(fresh.next_event() == 1);
    fresh.tick(1);
    assert(fresh.outputs()[0] == format_error(op) && fresh.outputs()[1].empty());
    assert(!fresh.next_event());
  }
  // Without a sound length the rest of the buffer cannot be split, so the
  // whole remainder is the corrupt operation; earlier operations commit.
  const can::Bytes truncated_header{0x10, 0, 0},
      short_length{0x10, 0, 0, 0, 7, 0, 0, 0},
      beyond_buffer{0x10, 0, 0, 0, 17, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0};
  for (const auto& rest : {truncated_header, short_length, beyond_buffer,
                           short_length + frame(1, {})}) {
    auto bus = configured(125000);
    send(bus, 0, frame(0, {}) + rest, 0);
    assert(bus.next_event() == 1);
    bus.tick(1);
    assert(bus.outputs()[0] == format_error(rest));
    bus.clear_outputs();
    assert(bus.next_event() == 400000);
  }
  {  // Reports from several events of one instant are delivered together.
    can::Bus bus;
    send(bus, 0, unknown, 5);
    send(bus, 1, unknown, 5);
    send(bus, 0, no_kind, 5);
    bus.tick(6);
    assert(bus.outputs()[0] == format_error(unknown) + format_error(no_kind));
    assert(bus.outputs()[1] == format_error(unknown));
  }
  {  // A report due at a frame end follows that frame's own operations.
    auto bus = configured(125000);
    send(bus, 0, frame(0, {}), 0);
    send(bus, 1, unknown, 399999);
    bus.tick(400000);
    assert(bus.outputs()[0] == confirm(0));
    assert(bus.outputs()[1] == frame(0, {}) + format_error(unknown));
  }
  {  // Reports are bounded so a terminal's output never exceeds its maxSize.
    can::Bus bus;
    can::Bytes largest{0xad, 0xde, 0, 0, 0, 0, 0, 0};
    largest.resize(can::format_error_capacity - 10);
    largest[4] = std::uint8_t(largest.size());
    largest[5] = std::uint8_t(largest.size() >> 8);
    send(bus, 0, largest, 0);
    bus.tick(1);
    assert(bus.outputs()[0] == format_error(largest));
    auto oversized = largest;
    oversized.push_back(0);
    oversized[4] = std::uint8_t(oversized.size());
    oversized[5] = std::uint8_t(oversized.size() >> 8);
    can::Bus full;
    assert(rejects([&] { send(full, 0, oversized, 0); }));
    assert(!full.next_event());
    send(full, 0, largest, 0);
    assert(rejects([&] { send(full, 0, unknown, 0); }));
  }
  {  // No instant follows the last supported one.
    can::Bus bus;
    assert(rejects([&] { send(bus, 0, unknown, can::max_time); }));
    send(bus, 0, unknown, 7);
    assert(rejects([&] { bus.tick(8 - 1); }));
    assert(rejects([&] { bus.receive({}, 9); }));  // the due report was skipped
  }
}

void unsupported_operations_fail() {
  auto extended = frame(1, {}), remote = frame(1, {});
  extended[12] = 1;
  remote[13] = 1;
  // Each at the exact Length of its FMI-LS-BUS layout, with no data.
  const auto defined = [](std::uint8_t code, std::uint8_t length) {
    can::Bytes op(length);
    op[0] = code;
    op[4] = length;
    return op;
  };
  auto xl_one_byte = defined(0x12, 23);
  xl_one_byte[20] = 1;  // CAN XL carries 1..2048 data bytes
  auto fd_twelve_bytes = defined(0x11, 17 + 12);
  fd_twelve_bytes[15] = 12;  // a CAN FD length above Classical CAN's 8
  const can::Bytes fd_bitrate{0x40, 0, 0, 0, 13, 0, 0, 0, 2, 0xa0, 0x86, 0x01, 0},
      xl_bitrate{0x40, 0, 0, 0, 13, 0, 0, 0, 3, 0xa0, 0x86, 0x01, 0};
  for (const auto& op : {extended, remote, defined(0x01, 10), defined(0x11, 17),
                         fd_twelve_bytes, xl_one_byte, confirm(1), defined(0x30, 12),
                         defined(0x31, 15), defined(0x41, 9), defined(0x42, 8), fd_bitrate,
                         xl_bitrate, bitrate(83333)}) {
    auto bus = configured(125000);
    assert(rejects([&] { send(bus, 0, frame(0, {}) + op, 0); }));
    assert(!bus.next_event());  // the whole event stays uncommitted
  }
}

void malformed_operations_do_not_commit() {
  const auto op = frame(1, {1, 2, 3, 4});
  // Exercise every truncation and every single-byte mutation under sanitizers.
  // A failed event leaves the core transaction uncommitted; a reported one
  // drains within bounded output.
  for (std::size_t size = 0; size <= op.size(); ++size) {
    for (std::size_t at = 0; at < size; ++at) {
      for (unsigned value = 0; value < 256; ++value) {
        auto bus = configured(100000);
        auto input = can::Bytes(op.begin(), op.begin() + size);
        input[at] = static_cast<std::uint8_t>(value);
        try {
          send(bus, 0, input, 0);
        } catch (const std::exception&) {
          assert(!bus.next_event());
          assert(bus.outputs()[0].empty() && bus.outputs()[1].empty());
          continue;
        }
        drain(bus);
      }
    }
  }
}
}  // namespace

void configured_buffer_capacity() {
  can::Bus bus;
  assert(rejects([&] { bus.configure(2, 4, 1, 0, {}, can::min_operation_buffer - 1); }));
  assert(rejects([&] { bus.configure(2, 4, 1, 0, {}, can::max_operation_buffer + 1); }));
  // 61 bytes fit a 64-byte capacity; one more 16-byte operation does not.
  const auto within = bitrate(125000) + frame(1, {}) + frame(2, {}) + frame(3, {});
  const auto over = within + frame(4, {});
  for (const auto& [input, accepted] : {std::pair{within, true}, std::pair{over, false}}) {
    can::Bus sized;
    sized.configure(2, 8, 1, 0, {}, 64);
    send(sized, 1, bitrate(125000), 0);
    assert(rejects([&] { send(sized, 0, input, 0); }) != accepted);
  }
  // Format Error reports share what a frame end leaves of the capacity (28).
  can::Bytes largest{0xad, 0xde, 0, 0, 18, 0, 0, 0};
  largest.resize(18);
  can::Bus reports;
  reports.configure(2, 4, 1, 0, {}, 64);
  send(reports, 0, largest, 0);
  reports.tick(1);
  assert(reports.outputs()[0] == format_error(largest));
  auto oversized = largest;
  oversized.push_back(0);
  oversized[4] = 19;
  can::Bus full;
  full.configure(2, 4, 1, 0, {}, 64);
  assert(rejects([&] { send(full, 0, oversized, 0); }));
}

int main() {
  wire_lengths();
  burst_and_mid_transmission_request();
  arbitration_opportunities();
  contention_and_queues();
  discard_equal_id_and_bounds();
  finite_priority_stream();
  scheduled_error_retry_and_exhaustion();
  receiver_delivery_suppression_is_not_a_bus_error();
  discard_policy_applies_when_a_retry_loses_arbitration();
  four_declared_terminals();
  timing_configuration();
  corrupt_operations_get_format_errors();
  unsupported_operations_fail();
  malformed_operations_do_not_commit();
  configured_buffer_capacity();
}
