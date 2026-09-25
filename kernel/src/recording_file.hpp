#pragma once

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <stdexcept>
#include <string>

namespace sil {

// A Recording that cannot be read, or no longer is the file that was
// validated. The Replayer states it as a Manifest error at startup and as a
// Run failure while the Run advances.
struct RecordingError : std::runtime_error {
  using std::runtime_error::runtime_error;
};

// A Replay's Recording, opened once. Hashing, validation and replay all read
// through this one descriptor, so the validated file is the one consumed:
// renaming or deleting the path afterwards does not change what is replayed.
// A write into the file itself is detected by its size or modification time
// at the next read, which then throws RecordingError. The status-change time
// is not compared: unlinking the path moves it without touching the content.
// A write that keeps both fields (the same size, and a modification time the
// file system's clock does not advance or that is reset to its old value) is
// not detected.
class RecordingFile {
 public:
  explicit RecordingFile(const std::filesystem::path &path);
  ~RecordingFile();
  RecordingFile(const RecordingFile &) = delete;
  RecordingFile &operator=(const RecordingFile &) = delete;

  const std::filesystem::path &path() const { return path_; }
  uint64_t size() const { return stamp_.size; }

  // Reads up to `len` bytes at `offset`; fewer only at the end of the file.
  size_t read_at(uint64_t offset, void *out, size_t len) const;

  // SHA-256 of the whole file, read in fixed-size blocks.
  std::string sha256_hex() const;

 private:
  struct Stamp {
    uint64_t size = 0;
    int64_t mtime_s = 0, mtime_ns = 0;
    bool operator==(const Stamp &) const = default;
  };
  Stamp current_stamp() const;

  std::filesystem::path path_;
  int fd_ = -1;
  Stamp stamp_;
};

}  // namespace sil
