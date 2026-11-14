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

class RecordingFileOutput;

// MCAP implementation of the recording seam: self-describing, schema-embedded,
// indexed container (DESIGN #13). Written uncompressed and in publish order, so
// FileOrder reads reproduce the original global publish order.
//
// Each Channel's record mode (issue #398) selects what a Message stores: the
// payload (full), its SHA-256 under the fixed kDigestSchemaName Schema
// (digest), or nothing (off). A digest or off Channel carries the Channel
// metadata kRecordModeKey; a full Channel carries none, so a Manifest without
// `record` writes the same bytes as before.
inline constexpr const char *kRecordModeKey = "sil_record";
inline constexpr const char *kRecordSchemaKey = "sil_schema";
inline constexpr const char *kDigestSchemaName = "sil.Sha256Digest";

class McapRecorder : public RecordingSink {
 public:
  McapRecorder(const std::filesystem::path &out, const Manifest &manifest,
               ProvenanceMode mode);
  ~McapRecorder() override;

  void record(uint32_t channel_index, uint64_t publish_ns, uint32_t seq,
              const void *data, size_t len) override;
  void close() override;
  const ProvenanceDigest &digest() const override { return digest_; }

 private:
  ProvenanceDigest finalized_digest() const;

  std::filesystem::path path_;
  ProvenanceMode mode_;
  // Declared before the writer, which still holds a reference when destroyed.
  std::unique_ptr<RecordingFileOutput> output_;
  std::unique_ptr<mcap::McapWriter> writer_;
  std::vector<uint16_t> channel_ids_;  // manifest channel index -> mcap id
  std::vector<RecordMode> modes_;      // manifest channel index -> mode
  ProvenanceDigest digest_ =
      ProvenanceDigest::failed("the Recording is not finalized");
  bool closed_ = false;
};

}  // namespace sil
