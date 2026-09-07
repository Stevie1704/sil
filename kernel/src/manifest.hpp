#pragma once

#include <cstdint>
#include <filesystem>
#include <map>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <variant>
#include <vector>

namespace sil {

class InterceptorPlan;

// A manifest that can never be a valid kernel input. Runner exits 2.
struct ManifestError : std::runtime_error {
  using std::runtime_error::runtime_error;
};

struct FieldSpec {
  std::string name;
  std::string type;
  // Fixed-size array element count; 0 means a scalar field. An array occupies
  // count * sizeof(type) contiguous bytes (packed, no padding).
  size_t count = 0;
};

struct SchemaSpec {
  std::vector<FieldSpec> fields;
  std::string canonical_json;  // embedded verbatim into the MCAP schema record
  size_t byte_size = 0;
};

// A manifest-declared fault applied to a channel's message stream at the
// publish choke point. All four kinds — drop, drop_nth, delay, override — are
// live and applied there. The window is half-open [start_ns, end_ns); an empty
// end means "to end of run". Only the fields relevant to `kind` are populated.
struct InterceptorSpec {
  using OverrideValue = std::variant<uint64_t, int64_t, double>;

  std::string kind;  // drop | drop_nth | delay | override
  uint64_t start_ns = 0;
  std::optional<uint64_t> end_ns;
  std::optional<uint64_t> delay_ns;  // delay
  std::optional<uint64_t> n;         // drop_nth
  std::string field;                 // override
  // Override constants retain JSON's unsigned, signed, or floating category
  // until plan compilation pre-encodes the schema field's bytes.
  OverrideValue value = uint64_t{0};
};

// How a channel's payload crosses the kernel↔process boundary. Inline
// base64-encodes it into the JSON step line (default); Shm hands it through a
// per-channel arena, skipping base64/JSON for large payloads.
// Native participants are unaffected either way (pointer-based data plane).
enum class Transport { Inline, Shm };

struct ChannelSpec {
  std::string name;
  std::string schema;
  // Explicit latency in ns; empty means default "next activation" semantics.
  std::optional<uint64_t> latency_ns;
  Transport transport = Transport::Inline;
  std::vector<InterceptorSpec> interceptors;  // applied in declared order
  // Compiled from `interceptors` by load_manifest. Engines clone this
  // immutable template so runtime state stays isolated per run.
  std::shared_ptr<const InterceptorPlan> interceptor_plan;
};

struct NativeSpec {
  std::string library;      // resolved relative to the manifest directory
  std::string config_json;
  // The declared Channel contract. The manifest is authoritative: the C ABI
  // has no registration call, so runtime subscribe/publish only prove
  // conformance to these lists (issue #49).
  std::vector<std::string> subscribes;
  std::vector<std::string> publishes;
};

struct ProcessSpec {
  std::vector<std::string> command;
  uint64_t step_period_ns = 0;
  std::vector<std::string> subscribes;
  std::vector<std::string> publishes;
  int32_t priority = 0;
  bool shim = false;  // opt-in virtual clock shim (issue #27)
};

struct ReplaySpec {
  std::string recording;       // resolved relative to the manifest directory
  std::string recording_hash;  // sha256 the recording bytes must match
  std::vector<std::string> channels;
};

struct ParticipantSpec {
  std::string name;
  std::variant<NativeSpec, ProcessSpec, ReplaySpec> impl;
};

struct Manifest {
  uint64_t duration_ns = 0;
  uint64_t epoch_ns = 0;  // realtime epoch for shimmed participants (issue #27)
  std::map<std::string, SchemaSpec> schemas;
  std::vector<ChannelSpec> channels;          // name-sorted
  std::vector<ParticipantSpec> participants;  // name-sorted
  std::string hash_hex;                       // sha256 of the manifest file bytes
  std::filesystem::path base_dir;

  const ChannelSpec *find_channel(const std::string &name) const;
};

Manifest load_manifest(const std::filesystem::path &path);

}  // namespace sil
