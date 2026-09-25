#include "replayer.hpp"

#include <limits>

#include "engine.hpp"

namespace sil {

Replayer::Replayer(Engine &engine, const std::string &name,
                   const ReplaySpec &spec,
                   const std::filesystem::path &base_dir)
    : engine_(engine),
      name_(name),
      channels_(spec.channels.begin(), spec.channels.end()),
      duration_ns_(engine.manifest().duration_ns) {
  const std::string ctx = "participant '" + name + "'";

  std::filesystem::path path = spec.recording;
  if (path.is_relative()) path = base_dir / path;

  try {
    file_ = std::make_unique<RecordingFile>(path);

    // The file must hash to the value the manifest committed to — otherwise
    // the manifest hash would not actually cover the run's stimulus. This is
    // format-independent.
    const std::string actual = file_->sha256_hex();
    if (actual != spec.recording_hash)
      throw ManifestError("manifest error: " + ctx + ": recording '" +
                          path.string() + "' hash " + actual +
                          " does not match manifest " + spec.recording_hash);

    // Decode via the format reader; all validation below stays here and is
    // independent of how the recording was stored.
    std::unique_ptr<RecordingReader> validation =
        make_recording_reader(*file_);
    validate_channels(*validation, spec, path, ctx);

    // Every record must decode before any participant is stepped, so the
    // second pass below cannot fail on the file's own content.
    while (validation->current()) validation->advance();
    validation.reset();

    reader_ = make_recording_reader(*file_);
    skip_unreplayed();
  } catch (const RecordingError &e) {
    throw ManifestError("manifest error: " + ctx + ": " + e.what());
  }
}

void Replayer::validate_channels(const RecordingReader &reader,
                                 const ReplaySpec &spec,
                                 const std::filesystem::path &path,
                                 const std::string &ctx) const {
  // A replayed channel must exist in the recording, must be selected from the
  // recording's own topics, and its recorded schema must match the schema the
  // new manifest declares for that channel (name and byte layout, compared via
  // the canonical schema JSON the recorder embedded).
  const Manifest &m = engine_.manifest();
  std::set<std::string> found;
  for (const RecordingReader::ChannelSchema &cs : reader.channel_schemas()) {
    if (!channels_.count(cs.channel)) continue;
    found.insert(cs.channel);

    // The channel exists in the manifest (load_manifest already rejects a
    // replay of an undeclared channel).
    const ChannelSpec *spec_ch = m.find_channel(cs.channel);
    const SchemaSpec &want_schema = m.schemas.at(spec_ch->schema);
    if (cs.schema_name != spec_ch->schema ||
        cs.canonical_json != want_schema.canonical_json)
      throw ManifestError(
          "manifest error: " + ctx + ": channel '" + cs.channel +
          "' schema in recording does not match manifest schema '" +
          spec_ch->schema + "'");
  }
  for (const std::string &want : spec.channels)
    if (!found.count(want))
      throw ManifestError("manifest error: " + ctx + ": channel '" + want +
                          "' not present in recording '" + path.string() +
                          "'");
}

void Replayer::skip_unreplayed() {
  for (const RecordingReader::Message *m = reader_->current(); m;
       m = reader_->current()) {
    if (channels_.count(m->channel) && m->publish_ns < duration_ns_) return;
    reader_->advance();
  }
}

uint64_t Replayer::next_publish_ns() const {
  const RecordingReader::Message *m = reader_->current();
  return m ? m->publish_ns : std::numeric_limits<uint64_t>::max();
}

void Replayer::publish_due(uint64_t now_ns) {
  try {
    for (const RecordingReader::Message *m = reader_->current();
         m && m->publish_ns == now_ns; m = reader_->current()) {
      engine_.publish(name_, m->channel, m->bytes, m->size);
      reader_->advance();
      skip_unreplayed();
    }
  } catch (const RecordingError &e) {
    throw RunError("participant '" + name_ + "' failed: " + e.what());
  }
}

}  // namespace sil
