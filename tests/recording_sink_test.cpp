// The close contract of a Recording sink (#328): repeated close, a close that
// fails, and the digest that each case gives. A low file size limit makes the
// final flush fail on Linux and on macOS, without a full disk.
#include <sys/resource.h>
#include <unistd.h>

#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <string>

#include "engine.hpp"
#include "recording_sink.hpp"

namespace fs = std::filesystem;

namespace {

int failures = 0;

void check(bool condition, const char *what) {
  if (condition) return;
  std::fprintf(stderr, "FAILED: %s\n", what);
  ++failures;
}

// Limits the size of a new file while it is in scope. The digest test below
// reads the file after the limit is back; a read does not need it anyway.
class FileSizeLimit {
 public:
  explicit FileSizeLimit(rlim_t bytes) {
    getrlimit(RLIMIT_FSIZE, &saved_);
    rlimit limited = saved_;
    limited.rlim_cur = bytes;
    setrlimit(RLIMIT_FSIZE, &limited);
  }
  ~FileSizeLimit() { setrlimit(RLIMIT_FSIZE, &saved_); }

 private:
  rlimit saved_{};
};

sil::Manifest empty_manifest() {
  sil::Manifest manifest;
  manifest.hash_hex = std::string(64, '0');
  return manifest;
}

std::unique_ptr<sil::RecordingSink> sink(const fs::path &path,
                                         sil::ProvenanceMode mode) {
  return sil::make_recording_sink(path, empty_manifest(), mode);
}

bool close_throws(sil::RecordingSink &recorder) {
  try {
    recorder.close();
    return false;
  } catch (const sil::RunError &) {
    return true;
  }
}

void test_a_repeated_close_keeps_the_digest_of_the_file(const fs::path &dir) {
  const fs::path path = dir / "repeated.mcap";
  auto recorder = sink(path, sil::ProvenanceMode::Full);
  check(recorder->digest().state == sil::DigestState::Failed,
        "a digest before close is not a claim");

  recorder->close();
  const sil::ProvenanceDigest first = recorder->digest();
  recorder->close();

  check(first.state == sil::DigestState::Calculated, "close gives a digest");
  check(first.sha256 == sil::sha256_file(path), "digest of the final file");
  check(recorder->digest().sha256 == first.sha256, "second close changes none");
}

void test_a_failed_close_gives_the_digest_of_the_partial_file(
    const fs::path &dir) {
  const fs::path path = dir / "partial.mcap";
  bool threw = false;
  std::unique_ptr<sil::RecordingSink> recorder;
  {
    FileSizeLimit limit(16);
    recorder = sink(path, sil::ProvenanceMode::Full);
    threw = close_throws(*recorder);
    check(!close_throws(*recorder), "only the first close can throw");
  }
  check(threw, "a failed final flush is an error");
  check(fs::file_size(path) <= 16, "the file is partial");
  check(recorder->digest().state == sil::DigestState::Calculated,
        "the partial file has a digest");
  check(recorder->digest().sha256 == sil::sha256_file(path),
        "the digest is of the bytes on disk, not of the bytes written");
}

void test_a_failed_close_with_no_file_claims_no_digest(const fs::path &dir) {
  const fs::path path = dir / "gone.mcap";
  std::unique_ptr<sil::RecordingSink> recorder;
  {
    FileSizeLimit limit(16);
    recorder = sink(path, sil::ProvenanceMode::Full);
    fs::remove(path);
    close_throws(*recorder);
  }
  check(recorder->digest().state == sil::DigestState::Failed,
        "no file, no digest");
  check(recorder->digest().sha256.empty(), "a failed digest holds no value");
  check(!recorder->digest().reason.empty(), "a failed digest has a reason");
}

void test_external_mode_reports_the_failure_and_no_digest(const fs::path &dir) {
  const fs::path path = dir / "external.mcap";
  FileSizeLimit limit(16);
  auto recorder = sink(path, sil::ProvenanceMode::External);

  check(close_throws(*recorder), "a failed close is an error in both modes");
  check(recorder->digest().state == sil::DigestState::Omitted,
        "external mode omits the digest");
}

void test_the_destructor_reports_no_error(const fs::path &dir) {
  FileSizeLimit limit(16);
  auto recorder = sink(dir / "destroyed.mcap", sil::ProvenanceMode::Full);
  recorder.reset();  // Would end the process if the error left the destructor.
}

}  // namespace

int main() {
  std::signal(SIGXFSZ, SIG_IGN);
  const fs::path dir =
      fs::temp_directory_path() /
      ("sil-recording-sink-" + std::to_string(::getpid()));
  fs::create_directories(dir);

  test_a_repeated_close_keeps_the_digest_of_the_file(dir);
  test_a_failed_close_gives_the_digest_of_the_partial_file(dir);
  test_a_failed_close_with_no_file_claims_no_digest(dir);
  test_external_mode_reports_the_failure_and_no_digest(dir);
  test_the_destructor_reports_no_error(dir);

  fs::remove_all(dir);
  return failures == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}
