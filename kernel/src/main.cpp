#include <signal.h>
#include <unistd.h>

#include <charconv>
#include <chrono>
#include <cstring>
#include <filesystem>
#include <iostream>
#include <limits>
#include <memory>
#include <optional>
#include <string>
#include <string_view>
#include <system_error>
#include <utility>
#include <variant>

#include "clock_shim.hpp"
#include "copy_counters.hpp"
#include "engine.hpp"
#include "exit_codes.hpp"
#include "failure_report.hpp"
#include "manifest.hpp"
#include "owned_directory.hpp"
#include "provenance.hpp"
#include "recording_sink.hpp"
#include "run_diagnostics.hpp"
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

#ifdef SIL_RUN_DIAGNOSTICS
#define SIL_RUNNER_NAME "sil-run-diagnostics"
#define SIL_DIAGNOSTICS_USAGE \
  " [--diagnostics-out <report.json>] [--retain-run-directory <path>]" \
  " [--activation-observations <path>" \
  " [--activation-observations-limit-bytes <N>]]" \
  " [--launch-wrapper <participant> <executable>]"
#else
#define SIL_RUNNER_NAME "sil-run"
#define SIL_DIAGNOSTICS_USAGE ""
#endif

void usage() {
  std::cerr << "usage: " SIL_RUNNER_NAME " <manifest.json> "
               "[--participant-timeout-ms <N>] "
               "[--max-protocol-line-bytes <N>] "
               "[--max-step-output-messages <N>] "
               "[--max-step-inline-payload-bytes <N>] "
               "[-o <out.mcap> | --no-recording] "
               "[--provenance <out.provenance.json>] "
               "[--provenance-mode full|external]" SIL_DIAGNOSTICS_USAGE
               "\n"
               "       " SIL_RUNNER_NAME " --version\n"
               "       " SIL_RUNNER_NAME " --build-info\n";
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

bool parse_provenance_mode(std::string_view text, sil::ProvenanceMode &mode) {
  if (text == "full") {
    mode = sil::ProvenanceMode::Full;
    return true;
  }
  if (text == "external") {
    mode = sil::ProvenanceMode::External;
    return true;
  }
  return false;
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
// the JSON side-car. The Recording digest comes from the closed sink, so a
// normal Run does not read the file again. A Run that opened no sink has no
// output of its own to digest: a file at that path is not from this Run.
void write_side_car(sil::Provenance &provenance, int exit_code,
                    const sil::RecordingSink *recorder, bool recording,
                    const char *out_path, const char *requested_path) {
  provenance.run_exit_code = exit_code;
  if (recording && provenance.mode == sil::ProvenanceMode::External) {
    provenance.recording = sil::ProvenanceDigest::omitted();
  } else if (recording) {
    provenance.recording =
        recorder ? recorder->digest()
                 : sil::ProvenanceDigest::failed(
                       "the Run did not open this Recording");
    if (provenance.recording.state == sil::DigestState::Failed)
      provenance.errors.push_back(
          {"Recording", out_path, provenance.recording.reason});
  }
  const std::filesystem::path path =
      requested_path ? std::filesystem::path(requested_path)
                     : sil::default_provenance_path(out_path);
  try {
    sil::write_provenance(path, provenance);
  } catch (const std::exception &e) {
    // The Run's exit code and its first diagnostic remain authoritative. A
    // side-car write problem is reported without rewriting either.
    std::cerr << SIL_RUNNER_NAME ": " << e.what() << "\n";
  }
}

// The first diagnostic of a failed Run, kept with its facts for the report.
struct FirstFailure {
  std::string message;
  sil::FailureFacts facts;
};

sil::FailureFacts classify(const std::exception &e) {
  if (const auto *failure = dynamic_cast<const sil::ParticipantRunError *>(&e))
    return failure->failure;
  sil::FailureFacts facts;
  if (dynamic_cast<const sil::ManifestError *>(&e))
    facts.category = sil::FailureCategory::Manifest;
  else if (sil::run_interrupted())
    facts.category = sil::FailureCategory::Interrupted;
  return facts;
}

// Prints the diagnostic line and keeps the first one for the failure report.
void report_failure(std::optional<FirstFailure> &first,
                    const std::exception &e) {
  std::cerr << SIL_RUNNER_NAME ": " << e.what() << "\n";
  if (!first) first = FirstFailure{e.what(), classify(e)};
}

// A failed Run leaves a report beside its Recording; a successful one removes
// a report left by an earlier failed Run at the same path, so the file always
// describes the most recent Run.
void write_failure_side_car(const std::optional<FirstFailure> &first,
                            int exit_code, const char *out_path) {
  const std::filesystem::path path = sil::default_failure_report_path(out_path);
  if (exit_code == kExitOk) {
    std::error_code ignored;
    std::filesystem::remove(path, ignored);
    return;
  }
  try {
    sil::write_failure_report(path, exit_code,
                              first ? first->message : std::string(),
                              first ? first->facts : sil::FailureFacts{});
  } catch (const std::exception &e) {
    // As with the provenance side-car, the exit code stays authoritative.
    std::cerr << SIL_RUNNER_NAME ": " << e.what() << "\n";
  }
}

#ifdef SIL_RUN_DIAGNOSTICS
std::filesystem::path default_diagnostics_path(const char *recording_path) {
  return std::filesystem::path(std::string(recording_path) +
                               ".diagnostics.json");
}

bool same_path(const std::filesystem::path &a,
               const std::filesystem::path &b) {
  std::error_code ignored;
  return std::filesystem::absolute(a, ignored).lexically_normal() ==
         std::filesystem::absolute(b, ignored).lexically_normal();
}

// A report is a new file: it never replaces an earlier one, and it never
// lands on a path the Run writes for something else. Checked before the
// Manifest is loaded, so a rejected path starts no Participant.
bool diagnostics_path_is_new(const std::filesystem::path &path,
                             const char *recording_path, bool recording,
                             const std::filesystem::path &provenance_path) {
  std::error_code error;
  const auto status = std::filesystem::symlink_status(path, error);
  const char *problem = nullptr;
  if (std::filesystem::exists(status))
    problem = "already exists";
  else if ((recording && same_path(path, recording_path)) ||
           same_path(path, provenance_path) ||
           same_path(path, sil::default_failure_report_path(recording_path)))
    problem = "is also another artifact of this Run";
  if (!problem) return true;
  std::cerr << "sil-run-diagnostics: diagnostics report '" << path.string()
            << "' " << problem << "\n";
  return false;
}

sil::diagnostics::RunFacts run_facts(
    const char *manifest_path, const std::optional<sil::Manifest> &manifest,
    int exit_code, const std::optional<FirstFailure> &failure, bool recording,
    const char *out_path, const std::filesystem::path &provenance_path,
    sil::ProvenanceMode provenance_mode,
    const std::optional<std::chrono::milliseconds> &participant_timeout,
    const sil::RunBoundaryLimits &limits) {
  sil::diagnostics::RunFacts facts;
  facts.manifest_path = manifest_path;
  if (manifest) {
    facts.manifest_hash = manifest->hash_hex;
    facts.duration_ns = manifest->duration_ns;
  }
  facts.sil_version = SIL_VERSION;
  facts.source_repository = SIL_SOURCE_REPOSITORY;
  facts.source_revision = SIL_SOURCE_REVISION;
  facts.exit_code = exit_code;
  if (exit_code != kExitOk) {
    facts.failure_report_path =
        sil::default_failure_report_path(out_path).string();
    if (failure) {
      facts.failure_message = failure->message;
      facts.failure_category =
          sil::failure_category_name(failure->facts.category);
      if (!failure->facts.participant.empty())
        facts.failure_participant = failure->facts.participant;
      facts.failure_virtual_time_ns = failure->facts.virtual_time_ns;
    }
  }
  facts.recording = recording;
  facts.recording_path = out_path;
  facts.provenance_path = provenance_path.string();
  facts.provenance_mode = sil::provenance_mode_name(provenance_mode);
  if (participant_timeout)
    facts.participant_timeout_ms = uint64_t(participant_timeout->count());
  facts.max_protocol_line_bytes = limits.max_protocol_line_bytes;
  facts.max_step_output_messages = limits.max_step_output_messages;
  facts.max_step_inline_payload_bytes = limits.max_step_inline_payload_bytes;
  return facts;
}

// The launch wrapper names one Process participant; any other name is a
// usage error before a Participant starts.
void require_process(const sil::Manifest &manifest, const char *name) {
  for (const sil::ParticipantSpec &participant : manifest.participants)
    if (participant.name == name &&
        std::holds_alternative<sil::ProcessSpec>(participant.impl))
      return;
  throw sil::ManifestError(
      std::string("--launch-wrapper: no Process participant '") + name + "'");
}

void write_diagnostics(const std::filesystem::path &path,
                       const sil::diagnostics::RunFacts &facts) {
  try {
    sil::diagnostics::write_report(path, facts);
  } catch (const std::exception &e) {
    // As with the other side-cars, the Run's exit code and its first
    // diagnostic stay authoritative; this line only follows them.
    std::cerr << "sil-run-diagnostics: cannot write diagnostics report: "
              << e.what() << "\n";
  }
}
#endif

int run(int argc, char **argv) {
  const int report = report_version(argc, argv);
  if (report >= 0) return report;

  // A participant process dying mid-write must surface as a RunError,
  // not kill the kernel via SIGPIPE.
  if (!sil::install_run_signal_handlers()) {
    std::cerr << SIL_RUNNER_NAME
        ": failed to install termination handlers\n";
    return kExitRunFailure;
  }
  signal(SIGPIPE, SIG_IGN);

  const char *manifest_path = nullptr;
  const char *out_path = "out.mcap";
  const char *provenance_path_arg = nullptr;
  sil::ProvenanceMode provenance_mode = sil::ProvenanceMode::Full;
  bool provenance_mode_given = false;
#ifdef SIL_RUN_DIAGNOSTICS
  const char *diagnostics_path_arg = nullptr;
  const char *retention_path_arg = nullptr;
  const char *observations_path_arg = nullptr;
  // 64 MiB of records by default (docs/profile-report.md).
  size_t observations_limit_bytes = size_t(64) << 20;
  bool observations_limit_given = false;
  const char *wrapped_participant_arg = nullptr;
  const char *launch_wrapper_arg = nullptr;
#endif
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
    } else if (std::strcmp(argv[i], "--provenance-mode") == 0) {
      if (provenance_mode_given || i + 1 >= argc) {
        usage();
        return kExitConfigError;
      }
      if (!parse_provenance_mode(argv[++i], provenance_mode)) {
        std::cerr << SIL_RUNNER_NAME
            ": --provenance-mode must be full or external\n";
        return kExitConfigError;
      }
      provenance_mode_given = true;
    } else if (std::strcmp(argv[i], "--participant-timeout-ms") == 0) {
      if (participant_timeout || i + 1 >= argc) {
        usage();
        return kExitConfigError;
      }
      std::chrono::milliseconds timeout;
      if (!parse_participant_timeout(argv[++i], timeout)) {
        std::cerr << SIL_RUNNER_NAME
            ": --participant-timeout-ms must be a positive integer "
            "representable as milliseconds\n";
        return kExitConfigError;
      }
      participant_timeout = timeout;
    } else if (SizeLimitOption *option = size_limit_for(argv[i])) {
      if (option->given || i + 1 >= argc) {
        usage();
        return kExitConfigError;
      }
      if (!parse_positive_size(argv[++i], *option->destination)) {
        std::cerr << SIL_RUNNER_NAME ": " << option->flag
                  << " must be a positive integer representable as a "
                     "size_t\n";
        return kExitConfigError;
      }
      option->given = true;
#ifdef SIL_RUN_DIAGNOSTICS
    } else if (std::strcmp(argv[i], "--diagnostics-out") == 0 &&
               i + 1 < argc) {
      if (diagnostics_path_arg) {
        usage();
        return kExitConfigError;
      }
      diagnostics_path_arg = argv[++i];
    } else if (std::strcmp(argv[i], "--retain-run-directory") == 0 &&
               i + 1 < argc) {
      if (retention_path_arg) {
        usage();
        return kExitConfigError;
      }
      retention_path_arg = argv[++i];
    } else if (std::strcmp(argv[i], "--activation-observations") == 0 &&
               i + 1 < argc) {
      if (observations_path_arg) {
        usage();
        return kExitConfigError;
      }
      observations_path_arg = argv[++i];
    } else if (std::strcmp(argv[i], "--activation-observations-limit-bytes") ==
               0) {
      if (observations_limit_given || i + 1 >= argc) {
        usage();
        return kExitConfigError;
      }
      if (!parse_positive_size(argv[++i], observations_limit_bytes)) {
        std::cerr << SIL_RUNNER_NAME
            ": --activation-observations-limit-bytes must be a positive "
            "integer representable as a size_t\n";
        return kExitConfigError;
      }
      observations_limit_given = true;
    } else if (std::strcmp(argv[i], "--launch-wrapper") == 0 &&
               i + 2 < argc) {
      if (launch_wrapper_arg) {
        usage();
        return kExitConfigError;
      }
      wrapped_participant_arg = argv[++i];
      launch_wrapper_arg = argv[++i];
#endif
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

#ifdef SIL_RUN_DIAGNOSTICS
  const std::filesystem::path diagnostics_path =
      diagnostics_path_arg ? std::filesystem::path(diagnostics_path_arg)
                           : default_diagnostics_path(out_path);
  const std::filesystem::path provenance_path =
      provenance_path_arg ? std::filesystem::path(provenance_path_arg)
                          : sil::default_provenance_path(out_path);
  if (!diagnostics_path_is_new(diagnostics_path, out_path, recording,
                               provenance_path))
    return kExitConfigError;
  if (retention_path_arg) {
    // A profiler captures the modules a Run loads from its Run working
    // directory before that directory is gone (#315). The path must be new,
    // so an earlier tree is never merged or replaced.
    std::error_code error;
    if (std::filesystem::symlink_status(retention_path_arg, error).type() !=
        std::filesystem::file_type::not_found) {
      std::cerr << SIL_RUNNER_NAME ": --retain-run-directory '"
                << retention_path_arg << "' already exists\n";
      return kExitConfigError;
    }
    sil::OwnedDirectory::retain_next_unique_at(
        std::filesystem::absolute(retention_path_arg));
  }
  if (launch_wrapper_arg) {
    // A profiler's adapter for one Process participant (#317). The runner
    // checks the file here and the participant after preparation, both
    // before any Participant starts.
    std::error_code error;
    const auto wrapper = std::filesystem::absolute(launch_wrapper_arg, error);
    if (error || !std::filesystem::is_regular_file(wrapper, error) ||
        ::access(wrapper.c_str(), X_OK) != 0) {
      std::cerr << SIL_RUNNER_NAME ": --launch-wrapper '"
                << launch_wrapper_arg << "' is not an executable file\n";
      return kExitConfigError;
    }
    sil::diagnostics::wrap_launch(wrapped_participant_arg, wrapper);
  }
  if (observations_limit_given && !observations_path_arg) {
    usage();
    return kExitConfigError;
  }
  if (observations_path_arg) {
    // A profiler joins these host intervals with its samples (#316). The
    // file is new, so earlier evidence is never replaced.
    std::error_code error;
    if (std::filesystem::symlink_status(observations_path_arg, error).type() !=
        std::filesystem::file_type::not_found) {
      std::cerr << SIL_RUNNER_NAME ": --activation-observations '"
                << observations_path_arg << "' already exists\n";
      return kExitConfigError;
    }
    std::string problem;
    if (!sil::diagnostics::observe_activations(
            observations_path_arg, observations_limit_bytes, problem)) {
      std::cerr << SIL_RUNNER_NAME ": cannot create --activation-observations '"
                << observations_path_arg << "': " << problem << "\n";
      return kExitConfigError;
    }
  }
#endif

  sil::diagnostics::start_run();
  std::optional<sil::Provenance> provenance;
  std::optional<sil::PreparedRun> prepared;
  std::unique_ptr<sil::RecordingSink> recorder;
  std::optional<sil::Manifest> manifest;
  std::optional<FirstFailure> failure;
  int exit_code = kExitConfigError;

  try {
    manifest.emplace(sil::load_manifest(manifest_path));
    provenance.emplace(
        sil::initialize_provenance(manifest.value(), SIL_VERSION,
                                   SIL_SOURCE_REPOSITORY, SIL_SOURCE_REVISION,
                                   provenance_mode));

    // Validate and resolve the Run before Engine can load a Native library or
    // spawn a Process participant. Preparation keeps the resolved execution
    // data separate from the authored Manifest.
    prepared.emplace(sil::prepare_run(manifest.value(), *provenance));
#ifdef SIL_RUN_DIAGNOSTICS
    if (wrapped_participant_arg)
      require_process(*manifest, wrapped_participant_arg);
#endif

    // Selecting the recording format is a manifest/config concern: an
    // unrecognized output extension is a Manifest error (exit 2) and must reject
    // before any participant is created.
    if (recording)
      recorder = sil::make_recording_sink(out_path, prepared->manifest(),
                                          provenance_mode);
    try {
      sil::diagnostics::enter_phase(sil::diagnostics::Phase::Setup);
      if (sil::run_interrupted())
        throw sil::RunError(sil::run_interrupt_message());
      sil::Engine engine(std::move(*prepared), recorder.get(),
                         participant_timeout, limits);
      // Destroyed before the Engine, so its teardown counts as shutdown on
      // the failure path as well.
      sil::diagnostics::PhaseOnExit teardown(sil::diagnostics::Phase::Shutdown);
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
      report_failure(failure, e);
      exit_code = kExitRunFailure;
    }
  } catch (const sil::ManifestError &e) {
    report_failure(failure, e);
    exit_code = kExitConfigError;
  } catch (const std::exception &e) {
    report_failure(failure, e);
    exit_code = kExitRunFailure;
  }

  // Close first so a failed Run's partial Recording is still an auditable
  // artifact with a final digest. McapRecorder's destructor is a second safety
  // net.
  if (recorder) {
    sil::diagnostics::enter_phase(
        sil::diagnostics::Phase::RecordingFinalization);
    try {
      recorder->close();
    } catch (const std::exception &e) {
      report_failure(failure, e);
      if (exit_code == kExitOk) exit_code = kExitRunFailure;
    }
  }
  sil::diagnostics::end_run();

  if (provenance)
    write_side_car(*provenance, exit_code, recorder.get(), recording, out_path,
                   provenance_path_arg);
  write_failure_side_car(failure, exit_code, out_path);

#ifdef SIL_RUN_DIAGNOSTICS
  write_diagnostics(diagnostics_path,
                    run_facts(manifest_path, manifest, exit_code, failure,
                              recording, out_path, provenance_path,
                              provenance_mode, participant_timeout, limits));
#endif

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
