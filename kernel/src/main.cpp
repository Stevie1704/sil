#include <signal.h>

#include <cstring>
#include <iostream>
#include <memory>

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
