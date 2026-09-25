#include "recording_file.hpp"

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cerrno>
#include <cstring>
#include <vector>

#include "sha256.hpp"

namespace sil {

namespace {

constexpr size_t kHashBlockBytes = 64 * 1024;

RecordingError unreadable(const std::filesystem::path &path,
                          const std::string &reason) {
  return RecordingError("cannot read recording '" + path.string() + "': " +
                        reason);
}

}  // namespace

RecordingFile::RecordingFile(const std::filesystem::path &path) : path_(path) {
  do {
    fd_ = ::open(path.c_str(), O_RDONLY | O_CLOEXEC);
  } while (fd_ < 0 && errno == EINTR);
  if (fd_ < 0) throw unreadable(path, std::strerror(errno));
  struct stat st;
  if (::fstat(fd_, &st) != 0 || !S_ISREG(st.st_mode)) {
    ::close(fd_);
    throw unreadable(path, "not a regular file");
  }
  stamp_ = current_stamp();
}

RecordingFile::~RecordingFile() { ::close(fd_); }

RecordingFile::Stamp RecordingFile::current_stamp() const {
  struct stat st;
  if (::fstat(fd_, &st) != 0) throw unreadable(path_, std::strerror(errno));
#ifdef __APPLE__
  const timespec &mtime = st.st_mtimespec;
#else
  const timespec &mtime = st.st_mtim;
#endif
  return {uint64_t(st.st_size), mtime.tv_sec, mtime.tv_nsec};
}

size_t RecordingFile::read_at(uint64_t offset, void *out, size_t len) const {
  auto *p = static_cast<uint8_t *>(out);
  size_t done = 0;
  while (done < len) {
    const ssize_t n = ::pread(fd_, p + done, len - done, off_t(offset + done));
    if (n < 0 && errno == EINTR) continue;
    if (n < 0) throw unreadable(path_, std::strerror(errno));
    if (n == 0) break;
    done += size_t(n);
  }
  // Checked after the read, so bytes read from a changed file never pass.
  if (current_stamp() != stamp_)
    throw RecordingError("recording '" + path_.string() +
                         "' changed after it was validated");
  return done;
}

std::string RecordingFile::sha256_hex() const {
  Sha256 hash;
  std::vector<uint8_t> block(kHashBlockBytes);
  for (uint64_t offset = 0; offset < size();) {
    const size_t n = read_at(offset, block.data(), block.size());
    if (n == 0) break;
    hash.update(block.data(), n);
    offset += n;
  }
  return hash.hex_digest();
}

}  // namespace sil
