#pragma once

#include <array>
#include <cstdint>
#include <span>
#include <stdexcept>
#include <vector>

namespace can {
using Bytes = std::vector<std::uint8_t>;

// No FMI or SiL dependencies: the adapter supplies logical event boundaries.
class Bus {
 public:
  static constexpr unsigned bitrate = 100000;
  static constexpr double transfer_seconds = 0.001;
  bool pending() const { return pending_; }
  const std::array<Bytes, 2>& outputs() const { return outputs_; }
  void receive(unsigned terminal, std::span<const std::uint8_t> operations);
  void complete();
  void clear_outputs() { outputs_ = {}; }

 private:
  std::array<Bytes, 2> outputs_;
  Bytes frame_;
  unsigned sender_ = 0;
  bool pending_ = false;
};
}  // namespace can
