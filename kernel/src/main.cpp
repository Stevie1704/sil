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
#include <utility>
#include <variant>

#include "clock_shim.hpp"
#include "copy_counters.hpp"
#include "engine.hpp"
#include "exit_codes.hpp"
#include "manifest.hpp"
#include "provenance.hpp"
#include "recording_sink.hpp"
#include "run_signal.hpp"

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

using sil::kExitConfigError;
using sil::kExitOk;
using sil::kExitRunFailure;

void usage() {
  std::cerr << "usage: sil-run <manifest.json> "
               "[--participant-timeout-ms <N>] "
               "[--max-protocol-line-bytes <N>] "
               "[--max-step-output-messages <N>] "
               "[--max-step-inline-payload-bytes <N>] "
               "[-o <out.mcap> | --no-recording] "
               "[--provenance <out.provenance.json>]\n"
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

// Every run-boundary number on the command line is one positive integer with
// no sign and no trailing text, differing only in the ceiling its destination
// can hold. from_chars over an unsigned type rejects "-1" and "1.5" and
// reports an out-of-range literal, so `ceiling` only has to bound the type.
bool parse_positive(std::string_view text, uint64_t ceiling, uint64_t &value) {
  if (text.empty()) return false;
  const auto result = std::from_chars(text.data(), text.data() + text.size(),
                                     value);
  return result.ec == std::errc{} && result.ptr == text.data() + text.size() &&
         value != 0 && value <= ceiling;
}

bool parse_participant_timeout(std::string_view text,
                               std::chrono::milliseconds &timeout) {
  uint64_t value = 0;
  if (!parse_positive(
          text, uint64_t(std::chrono::milliseconds::max().count()), value))
    return false;
  timeout = std::chrono::milliseconds(
      static_cast<std::chrono::milliseconds::rep>(value));
  return true;
}

bool parse_positive_size(std::string_view text, size_t &value) {
  uint64_t parsed = 0;
  if (!parse_positive(text, std::numeric_limits<size_t>::max(), parsed))
    return false;
  value = static_cast<size_t>(parsed);
  return true;
}

// The three Step-protocol size guards differ only in the flag that names them
// and the field they land in, so the loop below matches them through here
// rather than repeating the same parse-and-reject block once per guard.
struct SizeLimitOption {
  const char *flag;
  size_t *destination;
  bool given;
};

// Completes the provenance record once the Run's outcome is known and writes
// it beside the Recording. The Recording is digested here rather than during
// the preflight because its bytes do not exist until the sink has closed.
void write_side_car(sil::Provenance &provenance, int exit_code, bool recording,
                    const char *out_path, const char *requested_path) {
  provenance.run_exit_code = exit_code;
  if (recording) {
    try {
      provenance.recording_sha256 = sil::sha256_file(out_path);
    } catch (const std::exception &e) {
      provenance.errors.push_back({"Recording", out_path, e.what()});
    }
  }
  const std::filesystem::path path =
      requested_path ? std::filesystem::path(requested_path)
                     : sil::default_provenance_path(out_path);
  try {
    sil::write_provenance(path, provenance);
  } catch (const std::exception &e) {
    // The Run's exit code and its first diagnostic remain authoritative. A
    // side-car write problem is reported without rewriting either.
    std::cerr << "sil-run: " << e.what() << "\n";
  }
}

int run(int argc, char **argv) {
  const int report = report_version(argc, argv);
  if (report >= 0) return report;

  // A participant process dying mid-write must surface as a RunError,
  // not kill the kernel via SIGPIPE.
  if (!sil::install_run_signal_handlers()) {
    std::cerr << "sil-run: failed to install termination handlers\n";
    return kExitRunFailure;
  }
  signal(SIGPIPE, SIG_IGN);

  const char *manifest_path = nullptr;
  const char *out_path = "out.mcap";
  const char *provenance_path_arg = nullptr;
  // Running without a Recording is what separates the cost of routing to
  // subscribers from the cost of Recording I/O (issue #61). The engine already
  // treats a null sink as "do not record"; this is the switch that reaches it.
  bool recording = true;
  bool out_given = false;
  std::optional<std::chrono::milliseconds> participant_timeout;
  sil::RunBoundaryLimits limits;
  SizeLimitOption size_limits[] = {
      {"--max-protocol-line-bytes", &limits.max_protocol_line_bytes, false},
      {"--max-step-output-messages", &limits.max_step_output_messages, false},
      {"--max-step-inline-payload-bytes",
       &limits.max_step_inline_payload_bytes, false},
  };
  const auto size_limit_for = [&size_limits](const char *argument) {
    for (SizeLimitOption &option : size_limits)
      if (std::strcmp(argument, option.flag) == 0) return &option;
    return static_cast<SizeLimitOption *>(nullptr);
  };
  for (int i = 1; i < argc; i++) {
    if (std::strcmp(argv[i], "-o") == 0 && i + 1 < argc) {
      out_path = argv[++i];
      out_given = true;
    } else if (std::strcmp(argv[i], "--provenance") == 0 && i + 1 < argc) {
      if (provenance_path_arg) {
        usage();
        return kExitConfigError;
      }
      provenance_path_arg = argv[++i];
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
    } else if (SizeLimitOption *option = size_limit_for(argv[i])) {
      if (option->given || i + 1 >= argc) {
        usage();
        return kExitConfigError;
      }
      if (!parse_positive_size(argv[++i], *option->destination)) {
        std::cerr << "sil-run: " << option->flag
                  << " must be a positive integer representable as a "
                     "size_t\n";
        return kExitConfigError;
      }
      option->given = true;
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

  std::optional<sil::Provenance> provenance;
  std::optional<sil::PreparedRun> prepared;
  std::unique_ptr<sil::RecordingSink> recorder;
  std::optional<sil::Manifest> manifest;
  int exit_code = kExitConfigError;

  try {
    manifest.emplace(sil::load_manifest(manifest_path));
    provenance.emplace(
        sil::initialize_provenance(manifest.value(), SIL_VERSION,
                                   SIL_SOURCE_REPOSITORY, SIL_SOURCE_REVISION));

    // Validate and resolve the Run before Engine can load a Native library or
    // spawn a Process participant. Preparation keeps the resolved execution
    // data separate from the authored Manifest.
    prepared.emplace(sil::prepare_run(manifest.value(), *provenance));

    // Selecting the recording format is a manifest/config concern: an
    // unrecognized output extension is a Manifest error (exit 2) and must reject
    // before any participant is created.
    if (recording)
      recorder = sil::make_recording_sink(out_path, prepared->manifest());
    try {
      if (sil::run_interrupted())
        throw sil::RunError(sil::run_interrupt_message());
      sil::Engine engine(std::move(*prepared), recorder.get(),
                         participant_timeout, limits);
      engine.setup();
      engine.run();
      // A signal that arrived during the Run is a Run failure. Raising it here
      // rather than after the Recording is closed keeps that close, and the
      // provenance record that follows it, on the interrupted path too.
      if (sil::run_interrupted())
        throw sil::RunError(sil::run_interrupt_message());
      exit_code = kExitOk;
    } catch (const sil::ManifestError &) {
      throw;
    } catch (const std::exception &e) {
      std::cerr << "sil-run: " << e.what() << "\n";
      exit_code = kExitRunFailure;
    }
  } catch (const sil::ManifestError &e) {
    std::cerr << "sil-run: " << e.what() << "\n";
    exit_code = kExitConfigError;
  } catch (const std::exception &e) {
    std::cerr << "sil-run: " << e.what() << "\n";
    exit_code = kExitRunFailure;
  }

  // Close before hashing so a failed Run's partial Recording is still an
  // auditable artifact. McapRecorder's destructor is a second safety net.
  if (recorder) {
    try {
      recorder->close();
    } catch (const std::exception &e) {
      std::cerr << "sil-run: " << e.what() << "\n";
      if (exit_code == kExitOk) exit_code = kExitRunFailure;
    }
  }

  if (provenance)
    write_side_car(*provenance, exit_code, recording, out_path,
                   provenance_path_arg);

  if (exit_code == kExitOk && manifest)
    std::cout << "manifest_hash " << manifest->hash_hex << "\n";
  return exit_code;
}

}  // namespace

int main(int argc, char **argv) {
  const int code = run(argc, argv);
  // The Run is over and its exit code is fixed before the instrumented build
  // records it; the production build compiles this call away entirely.
  sil::counters::record_exit_code(code);
  return code;
}
