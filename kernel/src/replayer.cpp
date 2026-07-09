#include "replayer.hpp"

#include <fstream>
#include <limits>
#include <memory>
#include <set>
#include <sstream>

#include "engine.hpp"
#include "recording_reader.hpp"
#include "sha256.hpp"

namespace sil {

Replayer::Replayer(Engine &engine, const std::string &name,
                   const ReplaySpec &spec,
                   const std::filesystem::path &base_dir)
    : engine_(engine), name_(name) {
  const std::string ctx = "participant '" + name + "'";

  std::filesystem::path path = spec.recording;
  if (path.is_relative()) path = base_dir / path;

  // Read the whole file up front: it must exist, be readable, and hash to the
  // value the manifest committed to — otherwise the manifest hash would not
  // actually cover the run's stimulus. This is format-independent.
  std::ifstream in(path, std::ios::binary);
  if (!in)
    throw ManifestError("manifest error: " + ctx +
                        ": cannot read recording '" + path.string() + "'");
  std::stringstream buf;
  buf << in.rdbuf();
  const std::string bytes = buf.str();

  const std::string actual = sha256_hex(bytes);
  if (actual != spec.recording_hash)
    throw ManifestError("manifest error: " + ctx + ": recording '" +
                        path.string() + "' hash " + actual +
                        " does not match manifest " + spec.recording_hash);

  // Decode via the format reader; all validation below stays here and is
  // independent of how the recording was stored.
  std::unique_ptr<RecordingReader> reader = make_recording_reader(path, ctx);

  // A replayed channel must exist in the recording, must be selected from the
  // recording's own topics, and its recorded schema must match the schema the
  // new manifest declares for that channel (name and byte layout, compared via
  // the canonical schema JSON the recorder embedded).
  const Manifest &m = engine_.manifest();
  std::set<std::string> wanted(spec.channels.begin(), spec.channels.end());
  std::set<std::string> found;
  for (const RecordingReader::ChannelSchema &cs : reader->channel_schemas()) {
    if (!wanted.count(cs.channel)) continue;
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

  // Keep the recording's stored total publish order (the tie-break for
  // messages sharing a timestamp). Drop anything at or beyond the run duration
  // so a long recording can drive a shorter run.
  const uint64_t duration = m.duration_ns;
  for (RecordingReader::Message &msg : std::move(*reader).take_messages()) {
    if (!wanted.count(msg.channel)) continue;
    if (msg.publish_ns >= duration) continue;
    messages_.push_back(
        {msg.publish_ns, std::move(msg.channel), std::move(msg.bytes)});
  }
}

uint64_t Replayer::next_publish_ns() const {
  if (next_ >= messages_.size()) return std::numeric_limits<uint64_t>::max();
  return messages_[next_].publish_ns;
}

void Replayer::publish_due(uint64_t now_ns) {
  while (next_ < messages_.size() && messages_[next_].publish_ns == now_ns) {
    const Msg &m = messages_[next_];
    engine_.publish(name_, m.channel, m.bytes.data(), m.bytes.size());
    next_++;
  }
}

}  // namespace sil
