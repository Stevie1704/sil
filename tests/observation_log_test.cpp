// The bounded Activation observation writer (#316): its storage limit, its
// write-failure policy and its end line, pinned on a pipe whose bytes the
// test reads back. Expected byte counts are worked out by hand.
#include "observation_log.hpp"

#include <fcntl.h>
#include <signal.h>
#include <unistd.h>

#include <iostream>
#include <stdexcept>
#include <string>

namespace {

using sil::diagnostics::ObservationLog;

void check(bool condition, const std::string &message) {
  if (!condition) throw std::runtime_error(message);
}

struct Pipe {
  int read_end = -1;
  int write_end = -1;
  Pipe() {
    int fds[2];
    if (pipe(fds) != 0) throw std::runtime_error("pipe");
    read_end = fds[0];
    write_end = fds[1];
    fcntl(read_end, F_SETFL, O_NONBLOCK);
  }
  ~Pipe() {
    if (read_end >= 0) close(read_end);
  }
  std::string drain() const {
    std::string text;
    char block[4096];
    for (;;) {
      const ssize_t n = read(read_end, block, sizeof block);
      if (n <= 0) return text;
      text.append(block, size_t(n));
    }
  }
};

const std::string kEnd = "{\"kind\":\"end\",";

/** Records stay in the buffer until it is full or the log is closed. */
void test_records_are_buffered_until_close() {
  Pipe p;
  ObservationLog log(p.write_end, 1000, 16);
  log.append("header\n");      // 7 bytes
  log.append("r1\n");          // 3 bytes: 10 buffered
  check(p.drain().empty(), "nothing is written before the buffer is full");
  log.append("record-two\n");  // 11 bytes: does not fit in 16 with 10
  check(p.drain() == "header\nr1\n", "a full buffer is written");
  log.close();
  const std::string rest = p.drain();
  check(rest ==
            "record-two\n" + kEnd +
                "\"records\":2,\"dropped\":0,\"truncated\":false,"
                "\"write_failed\":false,\"bytes\":21}\n",
        "close writes the rest and an end line: " + rest);
}

/** The first record past the limit stops the stream; later ones count. */
void test_the_limit_truncates_and_counts_dropped_records() {
  Pipe p;
  ObservationLog log(p.write_end, 12);
  log.append("header\n");  // 7
  log.append("r1\n");      // 10
  log.append("r2r2\n");    // would be 15: dropped, stream stops
  log.append("r\n");       // would fit (12), but the stream has stopped
  log.close();
  const std::string text = p.drain();
  check(text == "header\nr1\n" + kEnd +
                    "\"records\":1,\"dropped\":2,\"truncated\":true,"
                    "\"write_failed\":false,\"bytes\":10}\n",
        "truncated stream: " + text);
  check(log.truncated() && !log.write_failed(), "the flags");
}

/** A failed write drops what it held and every later record. */
void test_a_write_failure_drops_records_and_never_retries() {
  Pipe p;
  close(p.read_end);  // every write now fails with EPIPE
  p.read_end = -1;
  ObservationLog log(p.write_end, 1000, 16);
  log.append("header\n");
  log.append("r1\n");
  log.append("record-two\n");  // flushes: the write fails
  log.append("r3\n");
  log.close();
  check(log.write_failed() && !log.truncated(), "the flags");
  check(log.records() == 0 && log.dropped() == 3,
        "r1, record-two and r3 are dropped");
  check(log.bytes() == 0, "no byte reached the file");
}

/** A record that cannot be made is counted. */
void test_a_record_that_cannot_be_made_is_counted() {
  Pipe p;
  ObservationLog log(p.write_end, 1000);
  log.append("header\n");
  log.drop();
  log.close();
  check(p.drain().find("\"dropped\":1,") != std::string::npos,
        "the end line counts it");
}

}  // namespace

int main() {
  signal(SIGPIPE, SIG_IGN);
  const struct {
    const char *name;
    void (*run)();
  } tests[] = {
      {"buffered until close", test_records_are_buffered_until_close},
      {"limit", test_the_limit_truncates_and_counts_dropped_records},
      {"write failure", test_a_write_failure_drops_records_and_never_retries},
      {"cannot be made", test_a_record_that_cannot_be_made_is_counted},
  };
  int failures = 0;
  for (const auto &test : tests) {
    try {
      test.run();
    } catch (const std::exception &e) {
      std::cerr << "FAIL " << test.name << ": " << e.what() << "\n";
      failures++;
    }
  }
  return failures ? 1 : 0;
}
