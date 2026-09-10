#pragma once

#include <sys/types.h>

#include <cstdint>
#include <map>
#include <memory>
#include <string>
#include <vector>

#include "sil/clock_region.h"

#include "engine.hpp"
#include "mapped_region.hpp"

namespace sil {

// The normative JSON-lines step protocol is specified in
// docs/step-protocol.md. This endpoint is fully sequential, so a process
// participant cannot make execution order nondeterministic between requests.
class ProcessParticipant {
 public:
  ProcessParticipant(Engine &engine, const std::string &name,
                     const ProcessSpec &spec);
  ~ProcessParticipant();

  ProcessParticipant(const ProcessParticipant &) = delete;
  ProcessParticipant &operator=(const ProcessParticipant &) = delete;

  void step(uint64_t now_ns);
  void shutdown();

 private:
  void send_line(const std::string &line);
  std::string read_line();

  Engine &engine_;
  std::string name_;
  uint64_t period_ns_;
  std::vector<std::pair<std::string, SubQueue *>> inputs_;  // declared order
  std::vector<std::string> publishes_;
  int child_stdin_ = -1;
  int child_stdout_ = -1;
  pid_t pid_ = -1;
  std::string read_buffer_;
  bool alive_ = false;

  // Virtual clock shim (issue #28). When the participant opts in, the kernel
  // maps a small fixed-layout time region shared with the child, injects the
  // shim preload plus the region path into the child's environment at spawn,
  // and writes the current virtual time into the region before every step so
  // the child's own clock reads return stepped virtual time. Left empty for
  // unshimmed participants; the region unlinks its file when released.
  MappedRegion clock_region_;
  std::string shim_lib_;  // resolved in the parent so the child only setenv()s
  uint64_t epoch_ns_ = 0;
  SleepPolicy sleep_policy_ = SleepPolicy::Immediate;  // issue #52

  void setup_clock_region();
  void inject_shim_env() const;  // runs in the forked child before exec
  void write_clock_region(uint64_t now_ns);

  // Channel arenas (issue #35). One arena per arena-backed channel this
  // participant subscribes to or publishes, mapped MAP_SHARED before fork so
  // the child maps the same file at load. The layout and per-step transport
  // rules are specified in docs/step-protocol.md. Empty for participants with
  // no arena-backed channel.
  struct Arena {
    MappedRegion region;  // sizeof(header) + capacity, unlinked when released
    size_t capacity = 0;  // schema byte_size
    uint64_t seq = 0;     // last seq stamped, for the fresh-payload marker
  };
  std::map<std::string, Arena> arenas_;  // by channel name

  struct StepInput {
    std::string channel;
    uint64_t publish_ns;
    std::vector<uint8_t> bytes;
  };

  struct StepOutput {
    std::string channel;
    std::vector<uint8_t> bytes;
  };

  // Private step-scoped codec seam. `encode_inputs` receives the complete
  // input set already merged in global publish order and never reorders it.
  // It uses the arena for the first message per arena-backed channel in that
  // step and the inline representation for every subsequent message. `decode_outputs`
  // honours the field present on each output (`shm_seq` or `data`) rather than
  // inferring transport from the channel declaration. Arena setup failures
  // remain ManifestError; stale-seq and capacity violations remain RunError.
  class StepCodec;

  std::unique_ptr<StepCodec> codec_;

  // Maps an arena for every arena-backed channel in `spec`, sized from the
  // schema byte_size.
  // Throws ManifestError (exit 2) on any create/map failure so an environment
  // problem is distinguishable from a run/test failure.
  void setup_arenas(const ProcessSpec &spec);
  // Writes `bytes` into the channel's arena and returns its post-write seq.
  uint64_t write_arena(const std::string &channel,
                       const std::vector<uint8_t> &bytes);
  // Reads the channel's arena payload back into `out`, checking `seq` freshness.
  void read_arena(const std::string &channel, uint64_t seq,
                  std::vector<uint8_t> &out);
};

}  // namespace sil
