#include "mcap_recording.hpp"

// Single translation unit that compiles the MCAP implementation (writer for
// recording, reader for the replayer); every other TU includes the headers
// for declarations only.
#define MCAP_IMPLEMENTATION
#include <fstream>

#include <mcap/reader.hpp>
#include <mcap/writer.hpp>

#include "engine.hpp"
#include "recording_reader.hpp"

namespace sil {

McapRecorder::McapRecorder(const std::filesystem::path &out,
                           const Manifest &manifest) {
  writer_ = std::make_unique<mcap::McapWriter>();
  mcap::McapWriterOptions opts("sil");
  opts.compression = mcap::Compression::None;
  mcap::Status status = writer_->open(out.string(), opts);
  if (!status.ok())
    throw RunError("cannot open recording '" + out.string() +
                   "': " + status.message);

  // The manifest hash ties the recording back to its exact execution input.
  mcap::Metadata meta;
  meta.name = "sil";
  meta.metadata = {{"manifest_hash", manifest.hash_hex}};
  status = writer_->write(meta);
  if (!status.ok()) throw RunError("recording metadata: " + status.message);

  // Channels registered in manifest (name-sorted) order: MCAP schema and
  // channel IDs are a pure function of the manifest.
  std::map<std::string, uint16_t> schema_ids;
  for (const ChannelSpec &c : manifest.channels) {
    auto it = schema_ids.find(c.schema);
    if (it == schema_ids.end()) {
      mcap::Schema schema(c.schema, "sil_pod",
                          manifest.schemas.at(c.schema).canonical_json);
      writer_->addSchema(schema);
      it = schema_ids.emplace(c.schema, schema.id).first;
    }
    mcap::Channel channel(c.name, "sil_pod", it->second);
    writer_->addChannel(channel);
    channel_ids_.push_back(channel.id);
  }
}

McapRecorder::~McapRecorder() {
  close();
}

void McapRecorder::record(uint32_t channel_index, uint64_t publish_ns,
                          uint32_t seq, const void *data, size_t len) {
  mcap::Message msg;
  msg.channelId = channel_ids_.at(channel_index);
  msg.sequence = seq;
  msg.logTime = publish_ns;
  msg.publishTime = publish_ns;
  msg.dataSize = len;
  msg.data = static_cast<const std::byte *>(data);
  mcap::Status status = writer_->write(msg);
  if (!status.ok()) throw RunError("recording write: " + status.message);
}

void McapRecorder::close() {
  if (closed_) return;
  closed_ = true;
  writer_->close();
}

std::unique_ptr<RecordingSink> make_recording_sink(
    const std::filesystem::path &out, const Manifest &manifest) {
  if (out.extension() == ".mcap")
    return std::make_unique<McapRecorder>(out, manifest);
  throw ManifestError("manifest error: unrecognized recording format '" +
                      out.extension().string() + "' for output '" +
                      out.string() + "'");
}

namespace {

// Decodes an MCAP recording into the format-neutral views the replayer
// validates. The bytes are read once, up front, so the reader owns a stable
// snapshot the caller can iterate without touching the file again.
class McapRecordingReader : public RecordingReader {
 public:
  McapRecordingReader(const std::filesystem::path &path,
                      const std::string &ctx) {
    mcap::McapReader reader;
    std::ifstream stream(path, std::ios::binary);
    if (!reader.open(stream).ok())
      throw ManifestError("manifest error: " + ctx + ": '" + path.string() +
                          "' is not a valid MCAP file");
    // Parse the summary (scanning the file if it has no summary section) so
    // channel and schema records are available for validation.
    if (!reader.readSummary(mcap::ReadSummaryMethod::AllowFallbackScan).ok())
      throw ManifestError("manifest error: " + ctx + ": '" + path.string() +
                          "' is not a readable MCAP recording");

    for (auto &[cid, channel] : reader.channels()) {
      mcap::SchemaPtr rs = reader.schema(channel->schemaId);
      if (!rs)
        throw ManifestError("manifest error: " + ctx + ": channel '" +
                            channel->topic + "' has no schema in recording");
      schemas_.push_back(
          {channel->topic, rs->name,
           std::string(reinterpret_cast<const char *>(rs->data.data()),
                       rs->data.size())});
    }

    // FileOrder (the default) hands back messages in the order they were
    // written, i.e. the original run's global publish order; that fixes the
    // tie-break for messages sharing a timestamp.
    for (const mcap::MessageView &mv : reader.readMessages()) {
      const auto *p = reinterpret_cast<const uint8_t *>(mv.message.data);
      messages_.push_back({mv.message.logTime, mv.channel->topic,
                           std::vector<uint8_t>(p, p + mv.message.dataSize)});
    }
    reader.close();
  }

  std::vector<ChannelSchema> channel_schemas() const override {
    return schemas_;
  }
  std::vector<Message> take_messages() && override {
    return std::move(messages_);
  }

 private:
  std::vector<ChannelSchema> schemas_;
  std::vector<Message> messages_;
};

}  // namespace

std::unique_ptr<RecordingReader> make_recording_reader(
    const std::filesystem::path &path, const std::string &ctx) {
  if (path.extension() == ".mcap")
    return std::make_unique<McapRecordingReader>(path, ctx);
  throw ManifestError("manifest error: " + ctx +
                      ": unrecognized recording format '" +
                      path.extension().string() + "' for recording '" +
                      path.string() + "'");
}

}  // namespace sil
