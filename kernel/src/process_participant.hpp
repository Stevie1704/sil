#pragma once

#include <sys/types.h>

#include <chrono>
#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "sil/clock_region.h"

#include "channel_arenas.hpp"
#include "engine.hpp"
#include "mapped_region.hpp"

namespace sil {

class OwnedDirectory;

// One Process participant's child: its lifetime, its working directory, its
// half of the step protocol, and the virtual clock it reads time from.
//
// The normative JSON-lines step protocol is specified in
// docs/step-protocol.md. This endpoint is fully sequential, so a process
// participant cannot make execution order nondeterministic between requests.
//
// The Arena layout is a separate type (ChannelArenas): it is the binary
// contract the child maps, and it holds without a child process. The step-line
// representation stays a seam private to this implementation, per issue #41 —
// this class is a deep module behind four declarations, and its step protocol
// is not a second public interface.
class ProcessParticipant {
 public:
  ProcessParticipant(Engine &engine, const std::string &name,
                     const ProcessSpec &spec,
                     std::optional<std::chrono::milliseconds>
                         participant_timeout);
  ~ProcessParticipant();

  ProcessParticipant(const ProcessParticipant &) = delete;
  ProcessParticipant &operator=(const ProcessParticipant &) = delete;

  void step(uint64_t now_ns);
  void shutdown();

 private:
  using Clock = std::chrono::steady_clock;

  enum class ResponsePhase { Initialization, Step };

  // Reaps the child and releases its regions, and answers its wait status.
  // `shutdown` turns a bad status into a RunError; the destructor cannot.
  int terminate_child();
  void send_line(const std::string &line);
  std::string request_response(const std::string &line,
                               ResponsePhase phase,
                               std::optional<uint64_t> virtual_time);
  std::string read_line(
      const std::optional<Clock::time_point> &deadline,
      ResponsePhase phase, std::optional<uint64_t> virtual_time);

  Engine &engine_;
  std::string name_;
  uint64_t period_ns_;
  // Subscriber routes in the participant's declared Channel order.
  std::vector<std::pair<std::string, SubscriberRoute *>> inputs_;
  std::vector<std::string> publishes_;
  int child_stdin_ = -1;
  int child_stdout_ = -1;
  pid_t pid_ = -1;
  std::string read_buffer_;
  bool alive_ = false;
  std::optional<std::chrono::milliseconds> participant_timeout_;

  // Kernel-owned directory for this participant. It is created under the Run
  // working directory before fork and removed after the child is reaped,
  // including destruction after a failed Run.
  std::unique_ptr<OwnedDirectory> working_directory_;

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

  // Channel Arenas (issue #35). Created before fork so the child reaches the
  // same regions at load; empty for participants with no arena-backed Channel.
  // This class translates the Manifest into Arenas and names them to the child
  // in the init line; only the codec moves payloads through them afterwards.
  ChannelArenas arenas_;

  struct StepInput {
    std::string channel;
    uint64_t publish_ns;
    std::vector<uint8_t> bytes;
  };

  struct StepOutput {
    std::string channel;
    std::vector<uint8_t> bytes;
  };

  // Private step-scoped codec seam (issue #41), with inline and Arena as
  // adapters behind it. `encode_inputs` receives the complete input set
  // already merged in global publish order and never reorders it. It fills the
  // declared Arena slots per Channel in that Step and uses the inline
  // representation for every excess Message. `decode_outputs` honours the
  // field present on each output (`shm_seq` or `data`) rather than inferring
  // Transport from the Channel declaration. Arena setup failures remain
  // ManifestError; stale-seq and capacity violations remain RunError.
  //
  // It reaches the child through nothing but `arenas_` and the negotiated
  // protocol level, so the seam carries no back-reference to this class.
  class StepCodec;

  // Built after the init handshake, so it holds the negotiated protocol level
  // by value rather than watching this class's.
  std::unique_ptr<StepCodec> codec_;
  static constexpr int kSingleSlotProtocol = 1;
  static constexpr int kIndexedSlotsProtocol = 2;
  // Negotiated Step protocol; an absent ready echo selects the legacy level.
  int protocol_ = kSingleSlotProtocol;

  // Creates an Arena for every arena-backed Channel in `spec`, sized from the
  // schema byte_size, and rejects a Channel declared in both directions.
  // Throws ManifestError (exit 2) on any create failure so an environment
  // problem is distinguishable from a run/test failure.
  void setup_arenas(const ProcessSpec &spec);
};

}  // namespace sil
