// Preparation-local digest reuse (#326). Each canonical input path is hashed
// once per prepare_run, every Participant keeps its own attribution entry, and
// a later preparation hashes the bytes that are on disk then.

#include <unistd.h>

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

#include "manifest.hpp"
#include "provenance.hpp"

namespace {

namespace fs = std::filesystem;
using sil::Manifest;
using sil::ManifestError;
using sil::NativeSpec;
using sil::ParticipantSpec;
using sil::Provenance;
using sil::ProvenanceDigests;
using sil::ProcessSpec;

// SHA-256 references computed outside the kernel with `shasum -a 256`.
constexpr const char *kSha256Abc =
    "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";
constexpr const char *kSha256Hello =
    "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824";
constexpr const char *kSha256Tool =
    "311c6f9b13faf0634961c511f1391a322784eec49ac5c7aa57f3d911062d33e7";
constexpr const char *kSha256Changed =
    "d67e2e944994496c8d8ec76eed0cf9f09679448d584b532bebf941852a37f5ed";

void check(bool condition, const std::string &message) {
  if (!condition) throw std::runtime_error(message);
}

void write_file(const fs::path &path, const std::string &bytes) {
  fs::create_directories(path.parent_path());
  std::ofstream output(path, std::ios::binary | std::ios::trunc);
  output << bytes;
  check(static_cast<bool>(output), "cannot write " + path.string());
}

ParticipantSpec native(const std::string &name, const std::string &library) {
  return {name, NativeSpec{library, "", {}, {}}};
}

ParticipantSpec process(const std::string &name,
                        std::vector<std::string> command) {
  ProcessSpec spec;
  spec.command = std::move(command);
  return {name, spec};
}

Manifest manifest_in(const fs::path &directory,
                     std::vector<ParticipantSpec> participants) {
  Manifest manifest;
  manifest.duration_ns = 1000;
  manifest.base_dir = directory;
  manifest.participants = std::move(participants);
  return manifest;
}

const sil::ProvenanceNativeParticipant &native_record(const Provenance &p,
                                                      const std::string &name) {
  for (const auto &record : p.native_participants)
    if (record.name == name) return record;
  throw std::runtime_error("no native record for " + name);
}

const sil::ProvenanceProcessParticipant &process_record(const Provenance &p,
                                                        const std::string &name) {
  for (const auto &record : p.process_participants)
    if (record.name == name) return record;
  throw std::runtime_error("no process record for " + name);
}

// The runner digest is one operation of its own, so every expectation counts it.
void shared_inputs_are_hashed_once(const fs::path &directory) {
  const fs::path library = directory / "lib" / "libshared.so";
  write_file(library, "abc");
  fs::create_directory_symlink(library.filename(), directory / "lib" / "alias.so");
  write_file(directory / "tool", "tool-body");
  write_file(directory / "data.txt", "hello");

  // Two Process participants share one executable; the first also names a
  // file argument. Three Native participants share one library through a
  // symlink alias and the direct path.
  const Manifest manifest = manifest_in(
      directory,
      {native("a", "lib/libshared.so"), native("b", "lib/alias.so"),
       native("c", "lib/libshared.so"),
       process("p", {"./tool", "data.txt"}), process("q", {"./tool"})});

  Provenance provenance = sil::initialize_provenance(manifest, "test", "", "");
  ProvenanceDigests digests;
  const auto prepared = sil::prepare_run(manifest, provenance, digests);
  (void)prepared;

  // Runner, shared library, shared executable, and the command file.
  check(digests.hash_operations() == 4,
        "expected 4 digest operations, got " +
            std::to_string(digests.hash_operations()));

  const fs::path canonical_library = fs::canonical(library);
  for (const std::string name : {"a", "b", "c"}) {
    const auto &record = native_record(provenance, name);
    check(record.library.has_value(), "native " + name + " has no library");
    check(record.library->path == canonical_library.string(),
          "native " + name + " attributed to " + record.library->path);
    check(record.library->sha256 == kSha256Abc,
          "native " + name + " digest mismatch");
  }

  const fs::path canonical_tool = fs::canonical(directory / "tool");
  for (const std::string name : {"p", "q"}) {
    const auto &record = process_record(provenance, name);
    check(record.executable.has_value(), "process " + name + " has no executable");
    check(record.executable->path == canonical_tool.string(),
          "process " + name + " executable attributed to " +
              record.executable->path);
    check(record.executable->sha256 == kSha256Tool,
          "process " + name + " executable digest mismatch");
  }

  check(provenance.command_files.size() == 1, "expected one command file");
  check(provenance.command_files[0].file.has_value(), "command file missing");
  check(provenance.command_files[0].file->sha256 == kSha256Hello,
        "command file digest mismatch");
}

void later_preparation_observes_replaced_bytes(const fs::path &directory) {
  const fs::path library = directory / "lib" / "libswap.so";
  write_file(library, "abc");
  const Manifest manifest =
      manifest_in(directory, {native("only", "lib/libswap.so")});

  Provenance first = sil::initialize_provenance(manifest, "test", "", "");
  ProvenanceDigests first_digests;
  (void)sil::prepare_run(manifest, first, first_digests);
  check(native_record(first, "only").library->sha256 == kSha256Abc,
        "first preparation digest mismatch");

  write_file(library, "changed");

  Provenance second = sil::initialize_provenance(manifest, "test", "", "");
  ProvenanceDigests second_digests;
  (void)sil::prepare_run(manifest, second, second_digests);
  check(native_record(second, "only").library->sha256 == kSha256Changed,
        "later preparation reused a stale digest");
  check(second_digests.hash_operations() == 2,
        "later preparation should hash the runner and the library once each");
}

void unreadable_shared_input_is_reported_per_participant(const fs::path &directory) {
  const Manifest manifest = manifest_in(
      directory, {native("a", "lib/missing.so"), native("b", "lib/missing.so")});

  Provenance provenance = sil::initialize_provenance(manifest, "test", "", "");
  ProvenanceDigests digests;
  bool rejected = false;
  try {
    (void)sil::prepare_run(manifest, provenance, digests);
  } catch (const ManifestError &) {
    rejected = true;
  }
  check(rejected, "missing shared library must reject the preparation");
  check(provenance.errors.size() == 2,
        "each Participant keeps its own error, got " +
            std::to_string(provenance.errors.size()));
}

}  // namespace

int main() {
  std::string pattern =
      (fs::temp_directory_path() / "sil-provenance-digest-XXXXXX").string();
  if (!mkdtemp(pattern.data())) {
    std::cerr << "cannot create a temporary directory\n";
    return 1;
  }
  const fs::path root = fs::canonical(pattern);

  int failures = 0;
  const auto run = [&failures](const char *name, auto body) {
    try {
      body();
      std::cout << "ok " << name << '\n';
    } catch (const std::exception &error) {
      ++failures;
      std::cerr << "FAIL " << name << ": " << error.what() << '\n';
    }
  };

  run("shared inputs are hashed once", [&] {
    shared_inputs_are_hashed_once(root / "shared");
  });
  run("later preparation observes replaced bytes", [&] {
    later_preparation_observes_replaced_bytes(root / "replaced");
  });
  run("unreadable shared input is reported per participant", [&] {
    unreadable_shared_input_is_reported_per_participant(root / "missing");
  });

  fs::remove_all(root);
  return failures == 0 ? 0 : 1;
}
