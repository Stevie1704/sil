#pragma once

#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include "recording_file.hpp"

namespace sil {

// Format-neutral read side of the recording seam. A reader decodes a recording
// into two format-independent views the replayer validates and schedules:
// per-channel schema descriptors (for the manifest layout match) and one pass
// over the messages in stored total publish order. All verification — content
// hash, channel presence, schema name/layout match, duration truncation, slot
// scheduling — stays in the replayer; only decoding lives here.
//
// A reader holds one record of the file at a time, never the whole file, so
// its payload memory is bounded by the largest record the file declares.
class RecordingReader {
 public:
  // One recorded channel's schema, as the recorder embedded it: the schema
  // name and its canonical JSON bytes, compared verbatim against the manifest.
  struct ChannelSchema {
    std::string channel;
    std::string schema_name;
    std::string canonical_json;
  };

  // A view into the reader's current record: valid until advance().
  struct Message {
    uint64_t publish_ns;
    const std::string &channel;
    const uint8_t *bytes;
    size_t size;
  };

  virtual ~RecordingReader() = default;

  // Schema descriptor for every channel present in the recording.
  virtual const std::vector<ChannelSchema> &channel_schemas() const = 0;

  // The message the pass stands on, or nullptr once every message is read.
  // Messages come in stored total publish order (the tie-break for equal
  // timestamps). Throws RecordingError for a record that does not decode.
  virtual const Message *current() const = 0;
  virtual void advance() = 0;
};

// Selects a recording format from the recording's extension and starts one
// pass over the messages of the already-opened file. Throws RecordingError
// for an unrecognized extension or an unreadable recording.
std::unique_ptr<RecordingReader> make_recording_reader(
    const RecordingFile &file);

}  // namespace sil
