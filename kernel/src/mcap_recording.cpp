#include "mcap_recording.hpp"

// Single translation unit that compiles the MCAP implementation (writer for
// recording, reader for the replayer); every other TU includes the headers
// for declarations only.
#define MCAP_IMPLEMENTATION
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <optional>

#include <mcap/reader.hpp>
#include <mcap/writer.hpp>

#include "copy_counters.hpp"
#include "engine.hpp"
#include "recording_reader.hpp"
#include "sha256.hpp"

namespace sil {

// The one place where Recording bytes reach the file. It digests what fwrite
// accepted, in file order, so the digest covers every record of the format: the
// MCAP writer sends all of them here, also its indexes, footer and final magic.
//
// After the first failed write, no later byte goes to the file, so the file
// stays a prefix of the Recording. The class holds a fixed-size digest state and
// no copy of the bytes.
class RecordingFileOutput final : public mcap::IWritable {
 public:
  RecordingFileOutput(const std::filesystem::path &path, ProvenanceMode mode)
      : path_(path.string()) {
    if (mode == ProvenanceMode::Full) start_digest();
    file_ = std::fopen(path_.c_str(), "wb");
  }
  ~RecordingFileOutput() override { end(); }

  bool opened() const { return file_ != nullptr; }
  bool failed() const { return !failure_.empty(); }
  const std::string &failure() const { return failure_; }

  void handleWrite(const std::byte *data, uint64_t size) override {
    size_ += size;  // The MCAP offsets need the size the format expects.
    if (failed() || !file_) return;
    if (std::fwrite(data, 1, size, file_) != size) {
      fail("cannot write");
      return;
    }
    update_digest(data, size);
  }

  void flush() override {
    if (!file_ || failed()) return;
    if (std::fflush(file_) != 0) fail("cannot write");
  }

  void end() override {
    if (!file_) return;
    const bool closed = std::fclose(file_) == 0;
    file_ = nullptr;
    if (!closed && !failed()) fail("cannot close");
  }

  uint64_t size() const override { return size_; }

  // The digest of the bytes written, or empty if there is no such digest. Call
  // it after end(): only then are the bytes final.
  std::string take_digest() {
    if (failed() || !digest_) return {};
    try {
      return digest_->hex_digest();
    } catch (const std::exception &) {
      return {};
    }
  }

 private:
  void start_digest() {
    try {
      digest_.emplace();
    } catch (const std::exception &) {
      digest_.reset();  // The file is still good; see finalized_digest().
    }
  }

  void update_digest(const std::byte *data, uint64_t size) {
    if (!digest_) return;
    try {
      digest_->update(data, size);
      counters::count(counters::Site::kRecordingDigest, size);
    } catch (const std::exception &) {
      digest_.reset();  // The file is still good; see finalized_digest().
    }
  }

  void fail(const char *action) {
    failure_ = std::string(action) + " '" + path_ + "': " + std::strerror(errno);
  }

  std::string path_;
  std::FILE *file_ = nullptr;
  uint64_t size_ = 0;
  std::optional<Sha256> digest_;
  std::string failure_;
};

McapRecorder::McapRecorder(const std::filesystem::path &out,
                           const Manifest &manifest, ProvenanceMode mode)
    : path_(out),
      mode_(mode),
      output_(std::make_unique<RecordingFileOutput>(out, mode)) {
  if (!output_->opened())
    throw RunError("cannot open recording '" + out.string() +
                   "': failed to open file \"" + out.string() +
                   "\" for writing");
  writer_ = std::make_unique<mcap::McapWriter>();
  mcap::McapWriterOptions opts("sil");
  opts.compression = mcap::Compression::None;
  writer_->open(*output_, opts);

  // The manifest hash ties the recording back to its exact execution input.
  mcap::Metadata meta;
  meta.name = "sil";
  meta.metadata = {{"manifest_hash", manifest.hash_hex}};
  mcap::Status status = writer_->write(meta);
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
  try {
    close();
  } catch (const std::exception &) {
    // A caller that needs the error calls close() itself.
  }
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
  if (output_->failed())
    throw RunError("recording write: " + output_->failure());
}

void McapRecorder::close() {
  if (closed_) return;
  closed_ = true;
  writer_->close();
  digest_ = finalized_digest();
  if (output_->failed())
    throw RunError("recording close: " + output_->failure());
}

// Normal path: the digest of the bytes written, with no read of the file. If the
// write path gives no digest (a failed write or close, or a backend failure), a
// single read of the file gives the digest of the bytes that are really there.
// This keeps the evidence of a partial file.
ProvenanceDigest McapRecorder::finalized_digest() const {
  if (mode_ == ProvenanceMode::External) return ProvenanceDigest::omitted();
  std::string streamed = output_->take_digest();
  if (!streamed.empty())
    return ProvenanceDigest::calculated(std::move(streamed));
  try {
    return ProvenanceDigest::calculated(sha256_file(path_));
  } catch (const std::exception &e) {
    return ProvenanceDigest::failed(e.what());
  }
}

std::unique_ptr<RecordingSink> make_recording_sink(
    const std::filesystem::path &out, const Manifest &manifest,
    ProvenanceMode mode) {
  if (out.extension() == ".mcap")
    return std::make_unique<McapRecorder>(out, manifest, mode);
  throw ManifestError("manifest error: unrecognized recording format '" +
                      out.extension().string() + "' for output '" +
                      out.string() + "'");
}

namespace {

// The recording file as MCAP's read interface. Every read goes through the
// one validated descriptor into a single buffer, which grows to the largest
// record read and no further: that buffer is a Replayer's resident payload.
class McapFileInput final : public mcap::IReadable {
 public:
  explicit McapFileInput(const RecordingFile &file) : file_(file) {}

  uint64_t size() const override { return file_.size(); }

  uint64_t read(std::byte **output, uint64_t offset, uint64_t size) override {
    // A record that claims to run past the end of the file is a read failure
    // for the MCAP reader to report, not an allocation to make.
    if (offset > file_.size() || size > file_.size() - offset) return 0;
    if (size > buffer_.size()) {
      buffer_.resize(size);
      counters::replay_read_buffer(size);
    }
    *output = buffer_.data();
    return file_.read_at(offset, buffer_.data(), size);
  }

 private:
  const RecordingFile &file_;
  std::vector<std::byte> buffer_;
};

// One pass over an MCAP recording's messages in FileOrder, which hands them
// back in the order they were written, i.e. the original run's global publish
// order; that fixes the tie-break for messages sharing a timestamp.
//
// Metadata is held apart from payload: the parsed summary keeps one chunk
// index per chunk (with one offset per channel in it) and every channel and
// schema record for the whole pass.
class McapRecordingReader : public RecordingReader {
 public:
  explicit McapRecordingReader(const RecordingFile &file)
      : input_(file), quoted_path_("'" + file.path().string() + "'") {
    if (!reader_.open(input_).ok())
      throw RecordingError(quoted_path_ + " is not a valid MCAP file");
    // A recording whose footer is missing was cut short; its last records
    // cannot be trusted to be complete.
    mcap::Footer footer;
    if (!mcap::McapReader::ReadFooter(
             input_, file.size() - mcap::internal::FooterLength, &footer)
             .ok())
      throw RecordingError(quoted_path_ + " is not a complete MCAP recording");
    // Parse the summary (scanning the file if it has no summary section) so
    // channel and schema records are available for validation.
    if (!reader_.readSummary(mcap::ReadSummaryMethod::AllowFallbackScan).ok())
      throw RecordingError(quoted_path_ + " is not a readable MCAP recording");

    for (auto &[cid, channel] : reader_.channels()) {
      mcap::SchemaPtr rs = reader_.schema(channel->schemaId);
      if (!rs)
        throw RecordingError("channel '" + channel->topic +
                             "' has no schema in recording");
      schemas_.push_back(
          {channel->topic, rs->name,
           std::string(reinterpret_cast<const char *>(rs->data.data()),
                       rs->data.size())});
    }

    view_.emplace(reader_.readMessages(
        [this](const mcap::Status &status) {
          if (problem_.empty()) problem_ = status.message;
        },
        mcap::ReadMessageOptions{}));
    it_.emplace(view_->begin());
    settle();
  }

  const std::vector<ChannelSchema> &channel_schemas() const override {
    return schemas_;
  }

  const Message *current() const override {
    return current_ ? &*current_ : nullptr;
  }

  void advance() override {
    ++*it_;
    settle();
  }

 private:
  // Surfaces a decode problem, then exposes the message the pass stands on.
  void settle() {
    if (!problem_.empty())
      throw RecordingError(quoted_path_ + " is not a readable MCAP recording: " +
                           problem_);
    current_.reset();
    if (*it_ == view_->end()) return;
    const mcap::MessageView &mv = **it_;
    current_.emplace(Message{mv.message.logTime, mv.channel->topic,
                             reinterpret_cast<const uint8_t *>(mv.message.data),
                             size_t(mv.message.dataSize)});
  }

  McapFileInput input_;
  const std::string quoted_path_;
  mcap::McapReader reader_;
  std::vector<ChannelSchema> schemas_;
  std::string problem_;
  std::optional<mcap::LinearMessageView> view_;
  std::optional<mcap::LinearMessageView::Iterator> it_;
  std::optional<Message> current_;
};

}  // namespace

std::unique_ptr<RecordingReader> make_recording_reader(
    const RecordingFile &file) {
  const std::filesystem::path &path = file.path();
  if (path.extension() == ".mcap")
    return std::make_unique<McapRecordingReader>(file);
  throw RecordingError("unrecognized recording format '" +
                       path.extension().string() + "' for recording '" +
                       path.string() + "'");
}

}  // namespace sil
