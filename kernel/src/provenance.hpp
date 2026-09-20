#pragma once

#include <filesystem>
#include <optional>
#include <string>
#include <vector>

#include "manifest.hpp"

namespace sil {

struct ProvenanceArtifact {
  std::string path;
  std::string sha256;
};

struct ProvenanceError {
  std::string artifact;
  std::string path;
  std::string reason;
};

struct ProvenanceNativeParticipant {
  std::string name;
  std::optional<ProvenanceArtifact> library;
};

struct ProvenanceProcessParticipant {
  std::string name;
  std::vector<std::string> command;
  std::optional<ProvenanceArtifact> executable;
};

struct ProvenanceFmu {
  std::string participant;
  std::optional<ProvenanceArtifact> archive;
};

// The deterministic, format-neutral data that becomes the JSON side-car. The
// structure intentionally contains no wall-clock, pid, hostname, Run working
// directory, or elapsed-time fields.
struct Provenance {
  static constexpr int kSchemaVersion = 1;

  std::string manifest_hash;
  std::string sil_version;
  std::string source_repository;
  std::string source_revision;
  std::string machine_class_id;
  std::string machine_os;
  std::string machine_architecture;
  std::string machine_libc;

  std::optional<ProvenanceArtifact> runner;
  std::optional<ProvenanceArtifact> clock_shim;
  std::vector<ProvenanceNativeParticipant> native_participants;
  std::vector<ProvenanceProcessParticipant> process_participants;
  std::vector<ProvenanceFmu> fmus;
  std::vector<ProvenanceError> errors;

  int run_exit_code = 2;
  std::optional<std::string> recording_sha256;
};

// Builds the fixed metadata portion of a Run provenance record. Artifact
// collection is separate so the caller can still serialize a partial record
// if a preflight artifact is unreadable.
Provenance initialize_provenance(const Manifest &manifest,
                                 const std::string &sil_version,
                                 const std::string &source_repository,
                                 const std::string &source_revision);

// Resolves and hashes every artifact before a participant is loaded or
// spawned. On success it also stores the exact paths that Native and Process
// adapters must use. On failure it records every artifact error found and
// throws ManifestError after the preflight has finished.
void collect_provenance(Manifest &manifest, Provenance &provenance);

// Computes the SHA-256 of an already-resolved file. Used for the Recording
// after its sink has closed.
std::string sha256_file(const std::filesystem::path &path);

// The default side-car is adjacent to the selected Recording path. With
// --no-recording, the runner still uses its default conceptual out.mcap path,
// so the same name remains predictable; --provenance can override it.
std::filesystem::path default_provenance_path(
    const std::filesystem::path &recording_path);

void write_provenance(const std::filesystem::path &path,
                      const Provenance &provenance);

}  // namespace sil
