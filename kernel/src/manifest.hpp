#pragma once

#include <cstdint>
#include <filesystem>
#include <map>
#include <optional>
#include <stdexcept>
#include <string>
#include <variant>
#include <vector>

namespace sil {

// A manifest that can never be a valid kernel input. Runner exits 2.
struct ManifestError : std::runtime_error {
  using std::runtime_error::runtime_error;
};

struct FieldSpec {
  std::string name;
  std::string type;
};

struct SchemaSpec {
  std::vector<FieldSpec> fields;
  std::string canonical_json;  // embedded verbatim into the MCAP schema record
  size_t byte_size = 0;
};

struct ChannelSpec {
  std::string name;
  std::string schema;
  // Explicit latency in ns; empty means default "next activation" semantics.
  std::optional<uint64_t> latency_ns;
};

struct NativeSpec {
  std::string library;      // resolved relative to the manifest directory
  std::string config_json;
};

struct ProcessSpec {
  std::vector<std::string> command;
  uint64_t step_period_ns = 0;
  std::vector<std::string> subscribes;
  std::vector<std::string> publishes;
  int32_t priority = 0;
};

struct ParticipantSpec {
  std::string name;
  std::variant<NativeSpec, ProcessSpec> impl;
};

struct Manifest {
  uint64_t duration_ns = 0;
  std::map<std::string, SchemaSpec> schemas;
  std::vector<ChannelSpec> channels;          // name-sorted
  std::vector<ParticipantSpec> participants;  // name-sorted
  std::string hash_hex;                       // sha256 of the manifest file bytes
  std::filesystem::path base_dir;

  const ChannelSpec *find_channel(const std::string &name) const;
};

Manifest load_manifest(const std::filesystem::path &path);

}  // namespace sil
