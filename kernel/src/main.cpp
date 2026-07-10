#include <signal.h>

#include <cstring>
#include <filesystem>
#include <iostream>
#include <memory>
#include <variant>

#include "clock_shim.hpp"
#include "engine.hpp"
#include "manifest.hpp"
#include "recording_sink.hpp"

namespace {

constexpr int kExitOk = 0;
constexpr int kExitRunFailure = 1;
constexpr int kExitConfigError = 2;

void usage() {
  std::cerr << "usage: sil-run <manifest.json> [-o <out.mcap>]\n";
}

// Fail fast at load if a participant opted into the shim but the shim library
// is missing next to the runner, so a broken install is a config error rather
// than a silently wall-clocked run. The shim is injected at spawn (issue #28).
bool manifest_requests_shim(const sil::Manifest &m) {
  for (const sil::ParticipantSpec &p : m.participants) {
    const auto *ps = std::get_if<sil::ProcessSpec>(&p.impl);
    if (ps && ps->shim) return true;
  }
  return false;
}

}  // namespace

int main(int argc, char **argv) {
  // A participant process dying mid-write must surface as a RunError,
  // not kill the kernel via SIGPIPE.
  signal(SIGPIPE, SIG_IGN);

  const char *manifest_path = nullptr;
  const char *out_path = "out.mcap";
  for (int i = 1; i < argc; i++) {
    if (std::strcmp(argv[i], "-o") == 0 && i + 1 < argc) {
      out_path = argv[++i];
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

  try {
    sil::Manifest manifest = sil::load_manifest(manifest_path);
    if (manifest_requests_shim(manifest)) {
      std::filesystem::path shim = sil::clock_shim_library_path();
      if (shim.empty() || !std::filesystem::exists(shim)) {
        std::cerr << "sil-run: clock shim requested but shim library not found "
                     "next to the runner: "
                  << shim.string() << "\n";
        return kExitConfigError;
      }
    }
    // Selecting the recording format is a manifest/config concern: an
    // unrecognized output extension is a config error (exit 2) and must reject
    // before any participant is created.
    std::unique_ptr<sil::RecordingSink> recorder =
        sil::make_recording_sink(out_path, manifest);
    try {
      sil::Engine engine(manifest, recorder.get());
      engine.setup();
      engine.run();
      recorder->close();
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
