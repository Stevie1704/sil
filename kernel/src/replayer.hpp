#pragma once

#include <cstdint>
#include <filesystem>
#include <memory>
#include <set>
#include <string>

#include "manifest.hpp"
#include "recording_file.hpp"
#include "recording_reader.hpp"

namespace sil {

class Engine;

// Re-publishes recorded channels from a prior run's MCAP as an ordinary
// scheduled participant. The recording is validated against the manifest at
// load time (content hash, channel presence, schema layout, every record
// decodable); each selected message is injected at its recorded virtual
// timestamp, before task activations in that slot, preserving the recording's
// global publish order for equal timestamps. Deterministic by construction: it
// lives inside the same stepped virtual-time world as every other participant.
//
// The recording is read twice through one descriptor: once at load time to
// hash and validate it, and once while the Run advances, one record at a time.
// Resident payload is bounded by the largest record, not the file length.
class Replayer {
 public:
  Replayer(Engine &engine, const std::string &name, const ReplaySpec &spec,
           const std::filesystem::path &base_dir);

  // Recorded timestamp of the next un-published message, or UINT64_MAX when
  // the recording is exhausted. Lets the engine fold replay times into slot
  // selection so a message never falls between task slots.
  uint64_t next_publish_ns() const;

  // Publishes every message recorded at exactly now_ns, in recording order.
  // Runs before task activations in the slot. May raise via Engine::fail.
  void publish_due(uint64_t now_ns);

 private:
  // Rejects a selected channel that is missing from the recording or whose
  // recorded schema differs from the manifest's.
  void validate_channels(const RecordingReader &reader, const ReplaySpec &spec,
                         const std::filesystem::path &path,
                         const std::string &ctx) const;

  // Moves the pass to the next selected message below the run duration. The
  // pass continues past a message at or beyond the duration: stored order is
  // not sorted, so a later message may still fall inside the Run.
  void skip_unreplayed();

  Engine &engine_;
  std::string name_;
  std::set<std::string> channels_;  // selected for replay
  uint64_t duration_ns_;
  std::unique_ptr<RecordingFile> file_;  // outlives reader_, which reads it
  std::unique_ptr<RecordingReader> reader_;
};

}  // namespace sil
