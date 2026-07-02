#include "recorder.hpp"

#define MCAP_IMPLEMENTATION
#include <mcap/writer.hpp>

#include "engine.hpp"

namespace sil {

Recorder::Recorder(const std::filesystem::path &out, const Manifest &manifest) {
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

Recorder::~Recorder() {
  close();
}

void Recorder::record(uint32_t channel_index, uint64_t publish_ns,
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

void Recorder::close() {
  if (closed_) return;
  closed_ = true;
  writer_->close();
}

}  // namespace sil
