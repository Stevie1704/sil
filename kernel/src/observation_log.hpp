#pragma once

// The bounded writer of the Activation observation sidecar (#316). Only
// `sil-run-diagnostics` builds it.
//
// Records are complete lines. They collect in a fixed buffer and go to the
// file when the buffer is full and at close. The file holds at most
// `limit_bytes` of records; the first record that does not fit, or the first
// write error, stops the stream, and every later record is only counted as
// dropped. The writer never retries and never waits for a reader: the file
// is a new regular file. The end line that close() appends states what was
// kept, so a reader can tell a complete stream from a cut one. It is the one
// line that may go past the limit.

#include <sys/types.h>

#include <cstddef>
#include <cstdint>
#include <string>

namespace sil::diagnostics {

class ObservationLog {
 public:
  static constexpr size_t kBufferBytes = 64 * 1024;

  // Takes ownership of `fd`. Only the process that made the log writes it:
  // a forked child that runs its exit handlers writes nothing.
  ObservationLog(int fd, uint64_t limit_bytes,
                 size_t buffer_bytes = kBufferBytes);
  ObservationLog(const ObservationLog &) = delete;
  ObservationLog &operator=(const ObservationLog &) = delete;
  // Closes the descriptor without a flush or an end line.
  ~ObservationLog();

  // `line` ends with '\n'. The first line is the header; it is a record for
  // the limit, but not counted in `records`.
  void append(const std::string &line) noexcept;
  // A record that could not be made (no memory) is counted as dropped.
  void drop() noexcept { dropped_++; }
  // Flushes and appends the end line; later calls do nothing.
  void close() noexcept;

  uint64_t records() const { return records_; }
  uint64_t dropped() const { return dropped_; }
  uint64_t bytes() const { return bytes_; }
  bool truncated() const { return truncated_; }
  bool write_failed() const { return write_failed_; }

 private:
  void flush() noexcept;
  bool write_all(const char *data, size_t size) noexcept;

  int fd_;
  pid_t owner_;
  uint64_t limit_bytes_;
  size_t buffer_bytes_;
  std::string buffer_;
  uint64_t buffered_records_ = 0;
  bool header_pending_ = true;
  uint64_t records_ = 0;   // records in the file, without the header
  uint64_t dropped_ = 0;   // records not in the file
  uint64_t accepted_bytes_ = 0;  // bytes in the file and in the buffer
  uint64_t bytes_ = 0;     // bytes in the file
  bool truncated_ = false;
  bool write_failed_ = false;
  bool mid_line_ = false;  // a failed write left part of a line
  bool closed_ = false;
};

}  // namespace sil::diagnostics
