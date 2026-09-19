#include <signal.h>

#include <cstring>
#include <filesystem>
#include <iostream>
#include <memory>
#include <variant>

#include "clock_shim.hpp"
#include "copy_counters.hpp"
#include "engine.hpp"
#include "manifest.hpp"
#include "recording_sink.hpp"

namespace {

constexpr int kExitOk = 0;
constexpr int kExitRunFailure = 1;
constexpr int kExitConfigError = 2;

void usage() {
  std::cerr << "usage: sil-run <manifest.json> [-o <out.mcap> | --no-recording]\n";
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
  for (int i = 1; i < argc; i++) {
    if (std::strcmp(argv[i], "-o") == 0 && i + 1 < argc) {
      out_path = argv[++i];
      out_given = true;
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
      sil::Engine engine(manifest, recorder.get());
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
