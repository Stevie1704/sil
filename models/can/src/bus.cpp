#include "bus.hpp"

namespace can {
namespace {
std::uint32_t u32(std::span<const std::uint8_t> b, std::size_t at) {
  return std::uint32_t(b[at]) | (std::uint32_t(b[at + 1]) << 8) |
         (std::uint32_t(b[at + 2]) << 16) | (std::uint32_t(b[at + 3]) << 24);
}
void require(bool condition, const char* message) {
  if (!condition) throw std::runtime_error(message);
}
}  // namespace

void Bus::receive(unsigned terminal, std::span<const std::uint8_t> operations) {
  require(terminal < 2, "exactly two terminals are supported");
  // Validate the complete transaction before committing any frame.
  auto next = *this;
  while (!operations.empty()) {
    require(operations.size() >= 8, "truncated operation header");
    const auto code = u32(operations, 0), length = u32(operations, 4);
    require(length >= 8 && length <= operations.size(), "invalid operation length");
    const auto op = operations.first(length);
    if (code == 0x40) {
      require(length >= 9, "missing configuration kind");
      if (op[8] == 1) {
        require(length == 13, "invalid bitrate configuration length");
        require(u32(op, 9) == bitrate, "only 100000 bit/s is supported");
      } else if (op[8] == 4) {
        require(length == 10 && op[9] == 1,
                "only BufferAndRetransmit configuration is supported");
      } else {
        throw std::runtime_error("unsupported configuration kind");
      }
    } else if (code == 0x10) {
      require(length >= 16, "truncated Classical CAN operation");
      const unsigned size = unsigned(op[14]) | (unsigned(op[15]) << 8);
      require(size <= 8 && length == 16 + size, "invalid Classical CAN payload length");
      require(u32(op, 8) <= 0x7ff && op[12] == 0 && op[13] == 0,
              "only 11-bit Classical CAN data frames are supported");
      require(!next.pending_, "competing transmission requests are unsupported");
      next.frame_.assign(op.begin(), op.end());
      next.sender_ = terminal;
      next.pending_ = true;
    } else {
      throw std::runtime_error("unsupported CAN operation");
    }
    operations = operations.subspan(length);
  }
  *this = std::move(next);
}

void Bus::complete() {
  require(pending_, "no transmission is pending");
  outputs_[1 - sender_] = frame_;
  outputs_[sender_] = {0x20, 0, 0, 0, 12, 0, 0, 0,
                       frame_[8], frame_[9], frame_[10], frame_[11]};
  pending_ = false;
  frame_.clear();
}
}  // namespace can
