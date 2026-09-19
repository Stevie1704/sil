#include <signal.h>

#include <charconv>
#include <chrono>
#include <cstring>
#include <filesystem>
#include <iostream>
#include <limits>
#include <memory>
#include <optional>
#include <string_view>
#include <variant>

#include "clock_shim.hpp"
#include "copy_counters.hpp"
#include "engine.hpp"
#include "manifest.hpp"
#include "recording_sink.hpp"

namespace {

#ifndef SIL_VERSION
#define SIL_VERSION "unknown"
#endif
#ifndef SIL_SOURCE_REPOSITORY
#define SIL_SOURCE_REPOSITORY "unknown"
#endif
#ifndef SIL_SOURCE_REVISION
#define SIL_SOURCE_REVISION "unknown"
#endif
#ifndef SIL_LICENSE_IDENTIFIER
#define SIL_LICENSE_IDENTIFIER "Apache-2.0"
#endif

constexpr int kExitOk = 0;
constexpr int kExitRunFailure = 1;
constexpr int kExitConfigError = 2;

void usage() {
  std::cerr << "usage: sil-run <manifest.json> [--participant-timeout-ms <N>] "
               "[-o <out.mcap> | --no-recording]\n"
               "       sil-run --version\n"
               "       sil-run --build-info\n";
}

int report_version(int argc, char **argv) {
  if (argc == 2 && std::strcmp(argv[1], "--version") == 0) {
    std::cout << SIL_VERSION << "\n";
    return kExitOk;
  }
  if (argc == 2 && std::strcmp(argv[1], "--build-info") == 0) {
    std::cout << "{\"license\":\"" << SIL_LICENSE_IDENTIFIER
              << "\",\"source_repository\":\"" << SIL_SOURCE_REPOSITORY
              << "\",\"source_revision\":\"" << SIL_SOURCE_REVISION
              << "\",\"version\":\"" << SIL_VERSION << "\"}\n";
    return kExitOk;
  }
  return -1;
}

bool parse_participant_timeout(std::string_view text,
                               std::chrono::milliseconds &timeout) {
  if (text.empty()) return false;
  uint64_t value = 0;
  const auto result = std::from_chars(text.data(), text.data() + text.size(),
                                     value);
  if (result.ec != std::errc{} || result.ptr != text.data() + text.size() ||
      value == 0 ||
      value > static_cast<uint64_t>(
                   std::chrono::milliseconds::max().count()))
    return false;
  timeout = std::chrono::milliseconds(
      static_cast<std::chrono::milliseconds::rep>(value));
  return true;
}

// Fail fast at load if a participant opted into the shim but the shim library
// is missing next to the runner, so a broken install is a Manifest error rather
// than a silently wall-clocked run. The shim is injected at spawn (issue #28).
bool manifest_requests_shim(const sil::Manifest &m) {
  for (const sil::ParticipantSpec &p : m.participants) {
    const auto *ps = std::get_if<sil::ProcessSpec>(&p.impl);
    if (ps && ps->shim) return true;
  }
  return false;
}

int run(int argc, char **argv) {
  const int report = report_version(argc, argv);
  if (report >= 0) return report;

  // A participant process dying mid-write must surface as a RunError,
  // not kill the kernel via SIGPIPE.
  signal(SIGPIPE, SIG_IGN);

  const char *manifest_path = nullptr;
  const char *out_path = "out.mcap";
  // Running without a Recording is what separates the cost of routing to
  // subscribers from the cost of Recording I/O (issue #61). The engine already
  // treats a null sink as "do not record"; this is the switch that reaches it.
  bool recording = true;
  bool out_given = false;
  std::optional<std::chrono::milliseconds> participant_timeout;
  for (int i = 1; i < argc; i++) {
    if (std::strcmp(argv[i], "-o") == 0 && i + 1 < argc) {
      out_path = argv[++i];
      out_given = true;
    } else if (std::strcmp(argv[i], "--participant-timeout-ms") == 0) {
      if (participant_timeout || i + 1 >= argc) {
        usage();
        return kExitConfigError;
      }
      std::chrono::milliseconds timeout;
      if (!parse_participant_timeout(argv[++i], timeout)) {
        std::cerr << "sil-run: --participant-timeout-ms must be a positive "
                     "integer representable as milliseconds\n";
        return kExitConfigError;
      }
      participant_timeout = timeout;
    } else if (std::strcmp(argv[i], "--no-recording") == 0) {
      recording = false;
    } else if (argv[i][0] == '-') {
      usage();
      return kExitConfigError;
    } else if (!manifest_path) {
      manifest_path = argv[i];
    } else {
      usage();
      return kExitConfigError;
    }
  }
  if (!manifest_path) {
    usage();
    return kExitConfigError;
  }
  // An output path that would never be written is a Manifest mistake, not
  // a silently ignored argument.
  if (!recording && out_given) {
    usage();
    return kExitConfigError;
  }

  try {
    sil::Manifest manifest = sil::load_manifest(manifest_path);
    if (manifest_requests_shim(manifest)) {
      std::filesystem::path shim = sil::clock_shim_library_path();
      if (shim.empty() || !std::filesystem::exists(shim)) {
        std::cerr << "sil-run: clock shim requested but shim library not found "
                     "in the runner installation layout: "
                  << shim.string() << "\n";
        return kExitConfigError;
      }
    }
    // Selecting the recording format is a manifest/config concern: an
    // unrecognized output extension is a Manifest error (exit 2) and must reject
    // before any participant is created.
    std::unique_ptr<sil::RecordingSink> recorder;
    if (recording) recorder = sil::make_recording_sink(out_path, manifest);
    try {
      sil::Engine engine(manifest, recorder.get(), participant_timeout);
      engine.setup();
      engine.run();
      if (recorder) recorder->close();
    } catch (const sil::ManifestError &) {
      throw;
    } catch (const std::exception &e) {
      std::cerr << "sil-run: " << e.what() << "\n";
      return kExitRunFailure;
    }
    std::cout << "manifest_hash " << manifest.hash_hex << "\n";
  } catch (const sil::ManifestError &e) {
    std::cerr << "sil-run: " << e.what() << "\n";
    return kExitConfigError;
  }
  return kExitOk;
}

}  // namespace

int main(int argc, char **argv) {
  const int code = run(argc, argv);
  // The Run is over and its exit code is fixed before the instrumented build
  // records it; the production build compiles this call away entirely.
  sil::counters::record_exit_code(code);
  return code;
}
