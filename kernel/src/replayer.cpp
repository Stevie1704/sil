#include "replayer.hpp"

#include <fstream>
#include <limits>
#include <set>
#include <sstream>

#include <mcap/reader.hpp>

#include "engine.hpp"
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
  // actually cover the run's stimulus.
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

  mcap::McapReader reader;
  {
    std::ifstream stream(path, std::ios::binary);
    if (!reader.open(stream).ok())
      throw ManifestError("manifest error: " + ctx +
                          ": '" + path.string() + "' is not a valid MCAP file");
    // Parse the summary (scanning the file if it has no summary section) so
    // channel and schema records are available for validation.
    if (!reader.readSummary(mcap::ReadSummaryMethod::AllowFallbackScan).ok())
      throw ManifestError("manifest error: " + ctx + ": '" + path.string() +
                          "' is not a readable MCAP recording");

    // A replayed channel must exist in the recording, must be selected from
    // the recording's own topics, and its recorded schema must match the
    // schema the new manifest declares for that channel (name and byte
    // layout, compared via the canonical schema JSON the recorder embedded).
    const Manifest &m = engine_.manifest();
    std::set<std::string> wanted(spec.channels.begin(), spec.channels.end());
    std::set<std::string> found;
    for (auto &[cid, channel] : reader.channels()) {
      if (!wanted.count(channel->topic)) continue;
      found.insert(channel->topic);

      // The channel exists in the manifest (load_manifest already rejects a
      // replay of an undeclared channel); a recording with no schema for its
      // own channel is malformed and rejected here.
      const ChannelSpec *cs = m.find_channel(channel->topic);
      const SchemaSpec &want_schema = m.schemas.at(cs->schema);
      mcap::SchemaPtr rs = reader.schema(channel->schemaId);
      if (!rs)
        throw ManifestError("manifest error: " + ctx + ": channel '" +
                            channel->topic + "' has no schema in recording");
      std::string rec_schema(reinterpret_cast<const char *>(rs->data.data()),
                             rs->data.size());
      if (rs->name != cs->schema || rec_schema != want_schema.canonical_json)
        throw ManifestError(
            "manifest error: " + ctx + ": channel '" + channel->topic +
            "' schema in recording does not match manifest schema '" +
            cs->schema + "'");
    }
    for (const std::string &want : spec.channels)
      if (!found.count(want))
        throw ManifestError("manifest error: " + ctx + ": channel '" + want +
                            "' not present in recording '" + path.string() +
                            "'");

    // FileOrder (the default) hands back messages in the order they were
    // written, i.e. the original run's global publish order; that fixes the
    // tie-break for messages sharing a timestamp. Drop anything at or beyond
    // the run duration so a long recording can drive a shorter run.
    const uint64_t duration = m.duration_ns;
    for (const mcap::MessageView &mv : reader.readMessages()) {
      if (!wanted.count(mv.channel->topic)) continue;
      if (mv.message.logTime >= duration) continue;
      const auto *p = reinterpret_cast<const uint8_t *>(mv.message.data);
      messages_.push_back({mv.message.logTime, mv.channel->topic,
                           std::vector<uint8_t>(p, p + mv.message.dataSize)});
    }
  }
  reader.close();
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
