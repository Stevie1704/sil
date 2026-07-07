#pragma once

#include <cstdint>
#include <filesystem>
#include <memory>
#include <vector>

#include "manifest.hpp"
#include "recording_sink.hpp"

namespace mcap {
class McapWriter;
}

namespace sil {

// MCAP implementation of the recording seam: self-describing, schema-embedded,
// indexed container (DESIGN #13). Written uncompressed and in publish order, so
// FileOrder reads reproduce the original global publish order.
class McapRecorder : public RecordingSink {
 public:
  McapRecorder(const std::filesystem::path &out, const Manifest &manifest);
  ~McapRecorder() override;

  void record(uint32_t channel_index, uint64_t publish_ns, uint32_t seq,
              const void *data, size_t len) override;
  void close() override;

 private:
  std::unique_ptr<mcap::McapWriter> writer_;
  std::vector<uint16_t> channel_ids_;  // manifest channel index -> mcap id
  bool closed_ = false;
};

}  // namespace sil
