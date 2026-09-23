#include "bus.hpp"

#include <cassert>
#include <exception>

int main() {
  const can::Bytes frame{0x10, 0, 0, 0, 20, 0, 0, 0,
                         1, 0, 0, 0, 0, 0, 4, 0, 1, 2, 3, 4};
  // Exercise every truncation and every single-byte mutation under UBSan.
  // A parser failure must leave the core transaction uncommitted.
  for (std::size_t size = 0; size <= frame.size(); ++size) {
    for (std::size_t at = 0; at < size; ++at) {
      for (unsigned value = 0; value < 256; ++value) {
        can::Bus bus;
        auto input = can::Bytes(frame.begin(), frame.begin() + size);
        input[at] = static_cast<std::uint8_t>(value);
        try {
          bus.receive(0, input);
          if (bus.pending()) bus.complete();
        } catch (const std::exception&) {
          assert(!bus.pending());
          assert(bus.outputs()[0].empty() && bus.outputs()[1].empty());
        }
      }
    }
  }
  can::Bus bus;
  auto competing = frame;
  competing.insert(competing.end(), frame.begin(), frame.end());
  try {
    bus.receive(0, competing);
    return 1;
  } catch (const std::exception&) {
    assert(!bus.pending());
  }
  bus.receive(0, frame);
  bus.complete();
  assert(bus.outputs()[1] == frame);
  assert(bus.outputs()[0] == can::Bytes({0x20, 0, 0, 0, 12, 0, 0, 0, 1, 0, 0, 0}));
}
