#pragma once

#include <cstdint>
#include <filesystem>
#include <memory>
#include <vector>

#include "manifest.hpp"

namespace mcap {
class McapWriter;
}

namespace sil {

// Writes every published message to an MCAP file. Conceptually a latency-0
// subscriber to all channels that runs last in every slot; implemented as a
// direct sink fed in global publish order, which yields the same bytes.
class Recorder {
 public:
  Recorder(const std::filesystem::path &out, const Manifest &manifest);
  ~Recorder();

  void record(uint32_t channel_index, uint64_t publish_ns, uint32_t seq,
              const void *data, size_t len);
  void close();

 private:
  std::unique_ptr<mcap::McapWriter> writer_;
  std::vector<uint16_t> channel_ids_;  // manifest channel index -> mcap id
  bool closed_ = false;
};

}  // namespace sil
