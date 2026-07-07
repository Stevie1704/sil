#pragma once

#include <cstdint>
#include <filesystem>
#include <memory>
#include <string>
#include <vector>

namespace sil {

// Format-neutral read side of the recording seam. A reader decodes a recording
// into two format-independent views the replayer validates and schedules:
// per-channel schema descriptors (for the manifest layout match) and the
// messages in stored total publish order. All verification — content hash,
// channel presence, schema name/layout match, duration truncation, slot
// scheduling — stays in the replayer; only decoding lives here.
class RecordingReader {
 public:
  // One recorded channel's schema, as the recorder embedded it: the schema
  // name and its canonical JSON bytes, compared verbatim against the manifest.
  struct ChannelSchema {
    std::string channel;
    std::string schema_name;
    std::string canonical_json;
  };

  struct Message {
    uint64_t publish_ns;
    std::string channel;
    std::vector<uint8_t> bytes;
  };

  virtual ~RecordingReader() = default;

  // Schema descriptor for every channel present in the recording.
  virtual std::vector<ChannelSchema> channel_schemas() const = 0;

  // All recorded messages in stored total publish order (the tie-break for
  // equal timestamps). The replayer filters to selected channels and drops
  // anything at or beyond the run duration. Consuming (rvalue-only): a
  // recording's messages are read exactly once, so the reader hands over its
  // buffer rather than copying every payload (large per DESIGN #9).
  virtual std::vector<Message> take_messages() && = 0;
};

// Selects a recording format from the recording's extension and opens a reader
// over its already-verified bytes. Throws ManifestError for an unrecognized
// extension or an unreadable recording.
std::unique_ptr<RecordingReader> make_recording_reader(
    const std::filesystem::path &path, const std::string &ctx);

}  // namespace sil
