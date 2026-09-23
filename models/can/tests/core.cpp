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
  bus.receive(0, bitrate(rate), 0);
  return bus;
}

void wire_lengths() {
  // Hand-derived in README.md: ID 0 with all-zero data.
  assert(can::frame_bits(0, {}) == 50);
  assert(can::frame_bits(0, can::Bytes(1)) == 56);
  assert(can::frame_bits(0, can::Bytes(8)) == 124);
  // Cross-checked against tests/wire.py.
  assert(can::frame_bits(1, can::Bytes{1, 2, 3, 4}) == 83);
  assert(can::frame_bits(0x7ff, can::Bytes(8, 0xff)) == 123);
  assert(can::frame_bits(0x555, can::Bytes{0, 1, 2, 3, 4, 5, 6, 7}) == 117);
}

void burst_and_mid_transmission_request() {
  auto bus = configured(500000);  // 2000 ns per bit
  const auto a = frame(0, can::Bytes(8)), b = frame(0x7ff, can::Bytes(8, 0xff)),
             c = frame(1, {1, 2, 3, 4});
  bus.receive(0, a, 1000);
  assert(bus.next_completion() == 1000 + 124 * 2000);
  bus.receive(0, b, 50000);  // waits: an in-progress frame is never preempted
  assert(bus.next_completion() == 249000);
  bus.complete(249000);
  assert(bus.outputs()[0] == confirm(0) && bus.outputs()[1] == a);
  bus.clear_outputs();
  const auto b_end = 249000 + 3 * 2000 + 123 * 2000;
  assert(bus.next_completion() == b_end);
  bus.receive(1, c, 300000);  // arrives while b is on the wire
  bus.complete(b_end);
  assert(bus.outputs()[0] == confirm(0x7ff) && bus.outputs()[1] == b);
  bus.clear_outputs();
  const auto c_end = b_end + 3 * 2000 + 83 * 2000;
  assert(bus.next_completion() == c_end);
  assert(rejects([&] { bus.complete(c_end - 1); }));
  bus.complete(c_end);
  assert(bus.outputs()[0] == c && bus.outputs()[1] == confirm(1));
  assert(!bus.next_completion());
}

void arbitration_opportunities() {
  const auto a = frame(0, {}), b = frame(1, {});
  {  // Arrival at a frame end, before its completion: waits for intermission.
    auto bus = configured(125000);  // 8000 ns per bit
    bus.receive(0, a, 0);
    bus.receive(1, b, 400000);
    bus.complete(400000);
    assert(bus.next_completion() == 424000 + 47 * 8000);
  }
  {  // Arrival during intermission starts when it ends; later on arrival.
    auto bus = configured(125000);
    bus.receive(0, a, 0);
    bus.complete(400000);
    bus.receive(1, b, 410000);
    assert(bus.next_completion() == 424000 + 47 * 8000);
    bus.complete(424000 + 47 * 8000);
    bus.receive(0, a, 900000);
    assert(bus.next_completion() == 1300000);
  }
  {  // Two frames eligible at one opportunity need arbitration (issue #154).
    auto bus = configured(125000);
    bus.receive(0, a, 0);
    assert(rejects([&] { bus.receive(1, b, 0); }));
    bus.receive(1, b, 10);
    assert(rejects([&] { bus.receive(0, a, 20); }));
    auto both = a;
    both.insert(both.end(), b.begin(), b.end());
    auto idle = configured(125000);
    assert(rejects([&] { idle.receive(0, both, 0); }));
    assert(!idle.next_completion());
  }
}

void timing_configuration() {
  for (std::uint32_t rate : {0u, 9999u, 83333u, 1000001u, 2000000u})
    assert(rejects([&] { can::Bus().receive(0, bitrate(rate), 0); }));
  for (std::uint32_t rate : {10000u, 125000u, 800000u, 1000000u}) configured(rate);
  auto bus = configured(500000);
  bus.receive(1, bitrate(500000), 0);
  assert(rejects([&] { bus.receive(1, bitrate(250000), 0); }));
  assert(rejects([&] { can::Bus().receive(0, frame(1, {}), 0); }));
  assert(rejects([&] { bus.receive(0, frame(1, {}), -1); }));
  assert(rejects([&] { bus.receive(0, frame(1, {}), can::max_time); }));
  bus.receive(0, frame(1, {}), can::max_time - 1000000);
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
          bus.receive(0, input, 0);
          if (auto end = bus.next_completion()) bus.complete(*end);
        } catch (const std::exception&) {
          assert(!bus.next_completion());
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
  timing_configuration();
  malformed_operations_do_not_commit();
}
