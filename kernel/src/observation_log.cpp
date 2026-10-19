#include "observation_log.hpp"

#include <unistd.h>

#include <cerrno>

namespace sil::diagnostics {

ObservationLog::ObservationLog(int fd, uint64_t limit_bytes,
                               size_t buffer_bytes)
    : fd_(fd),
      owner_(getpid()),
      limit_bytes_(limit_bytes),
      buffer_bytes_(buffer_bytes) {
  buffer_.reserve(buffer_bytes_);
}

ObservationLog::~ObservationLog() {
  if (fd_ >= 0 && getpid() == owner_) ::close(fd_);
}

void ObservationLog::append(const std::string &line) noexcept {
  const bool header = header_pending_;
  header_pending_ = false;
  if (closed_ || truncated_ || write_failed_ ||
      accepted_bytes_ + line.size() > limit_bytes_) {
    if (!closed_ && !write_failed_) truncated_ = true;
    if (!header) dropped_++;
    return;
  }
  if (buffer_.size() + line.size() > buffer_bytes_) flush();
  if (write_failed_) {
    if (!header) dropped_++;
    return;
  }
  if (line.size() > buffer_bytes_) {
    // Larger than the buffer: written directly, nothing is buffered now.
    if (!write_all(line.data(), line.size())) {
      if (!header) dropped_++;
      return;
    }
    accepted_bytes_ += line.size();
    if (!header) records_++;
    return;
  }
  buffer_ += line;  // reserved: no allocation
  accepted_bytes_ += line.size();
  if (!header) buffered_records_++;
}

void ObservationLog::flush() noexcept {
  if (buffer_.empty()) return;
  if (write_all(buffer_.data(), buffer_.size()))
    records_ += buffered_records_;
  else
    dropped_ += buffered_records_;
  buffered_records_ = 0;
  buffer_.clear();
}

bool ObservationLog::write_all(const char *data, size_t size) noexcept {
  if (write_failed_ || fd_ < 0 || getpid() != owner_) {
    write_failed_ = true;
    return false;
  }
  size_t written = 0;
  while (written < size) {
    const ssize_t n = ::write(fd_, data + written, size - written);
    if (n < 0 && errno == EINTR) continue;
    if (n <= 0) {
      write_failed_ = true;
      mid_line_ = written > 0;
      bytes_ += written;
      return false;
    }
    written += size_t(n);
  }
  bytes_ += written;
  return true;
}

void ObservationLog::close() noexcept {
  if (closed_ || getpid() != owner_) return;
  flush();
  closed_ = true;
  if (fd_ < 0) return;
  std::string end;
  try {
    if (mid_line_) end = "\n";
    end += "{\"kind\":\"end\",\"records\":" + std::to_string(records_) +
           ",\"dropped\":" + std::to_string(dropped_) +
           ",\"truncated\":" + (truncated_ ? "true" : "false") +
           ",\"write_failed\":" + (write_failed_ ? "true" : "false") +
           ",\"bytes\":" + std::to_string(bytes_) + "}\n";
  } catch (...) {
    end.clear();
  }
  // One attempt, also after a failed write: the stream says how it ended
  // when the file can still take it.
  size_t written = 0;
  while (written < end.size()) {
    const ssize_t n = ::write(fd_, end.data() + written, end.size() - written);
    if (n < 0 && errno == EINTR) continue;
    if (n <= 0) break;
    written += size_t(n);
  }
  ::close(fd_);
  fd_ = -1;
}

}  // namespace sil::diagnostics
