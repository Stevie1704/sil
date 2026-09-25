// FMI-LS-BUS CAN operations the native tests offer to the bus.
#pragma once

#include <cstdint>
#include <vector>

namespace operations {
using Bytes = std::vector<std::uint8_t>;

inline Bytes frame(std::uint32_t id, Bytes data) {
  const auto length = std::uint8_t(16 + data.size());
  Bytes op{0x10, 0, 0, 0, length, 0, 0, 0, std::uint8_t(id), std::uint8_t(id >> 8), 0, 0,
           0, 0, std::uint8_t(data.size()), 0};
  op.insert(op.end(), data.begin(), data.end());
  return op;
}
inline Bytes bitrate(std::uint32_t rate) {
  return {0x40, 0, 0, 0, 13, 0, 0, 0, 1, std::uint8_t(rate), std::uint8_t(rate >> 8),
          std::uint8_t(rate >> 16), std::uint8_t(rate >> 24)};
}
inline Bytes discard() { return {0x40, 0, 0, 0, 10, 0, 0, 0, 4, 2}; }
inline Bytes operator+(Bytes left, const Bytes& right) {
  left.insert(left.end(), right.begin(), right.end());
  return left;
}
}  // namespace operations
