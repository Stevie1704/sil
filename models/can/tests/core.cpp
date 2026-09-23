#include "bus.hpp"

#include <cassert>
#include <exception>

namespace {
can::Bytes frame(std::uint32_t id, can::Bytes data) {
  const auto length = std::uint8_t(16 + data.size());
  can::Bytes op{0x10, 0, 0, 0, length, 0, 0, 0,
                std::uint8_t(id), std::uint8_t(id >> 8), 0, 0,
                0, 0, std::uint8_t(data.size()), 0};
  op.insert(op.end(), data.begin(), data.end());
  return op;
}
can::Bytes bitrate(std::uint32_t rate) {
  return {0x40, 0, 0, 0, 13, 0, 0, 0, 1, std::uint8_t(rate), std::uint8_t(rate >> 8),
          std::uint8_t(rate >> 16), std::uint8_t(rate >> 24)};
}
can::Bytes confirm(std::uint32_t id) {
  return {0x20, 0, 0, 0, 12, 0, 0, 0, std::uint8_t(id), std::uint8_t(id >> 8), 0, 0};
}
can::Bytes bus_error(std::uint32_t id, std::uint8_t flag, bool sender) {
  return {0x31, 0, 0, 0, 15, 0, 0, 0, std::uint8_t(id),
          std::uint8_t(id >> 8), 0, 0, 1, flag, std::uint8_t(sender)};
}
can::Bytes discard() { return {0x40, 0, 0, 0, 10, 0, 0, 0, 4, 2}; }
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
  inputs[1] = frame(0, {1});
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
  configurations[1] = bitrate(100000);
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

void malformed_operations_do_not_commit() {
  const auto op = frame(1, {1, 2, 3, 4});
  // Exercise every truncation and every single-byte mutation under UBSan.
  // A parser failure must leave the core transaction uncommitted.
  for (std::size_t size = 0; size <= op.size(); ++size) {
    for (std::size_t at = 0; at < size; ++at) {
      for (unsigned value = 0; value < 256; ++value) {
        auto bus = configured(100000);
        auto input = can::Bytes(op.begin(), op.begin() + size);
        input[at] = static_cast<std::uint8_t>(value);
        try {
          send(bus, 0, input, 0);
          if (auto end = bus.next_event()) bus.complete(*end);
        } catch (const std::exception&) {
          assert(!bus.next_event());
          assert(bus.outputs()[0].empty() && bus.outputs()[1].empty());
        }
      }
    }
  }
}
}  // namespace

int main() {
  wire_lengths();
  burst_and_mid_transmission_request();
  arbitration_opportunities();
  contention_and_queues();
  discard_equal_id_and_bounds();
  finite_priority_stream();
  scheduled_error_retry_and_exhaustion();
  receiver_delivery_suppression_is_not_a_bus_error();
  four_declared_terminals();
  timing_configuration();
  malformed_operations_do_not_commit();
}
