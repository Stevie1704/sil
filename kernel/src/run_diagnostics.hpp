#pragma once

// The opt-in Run diagnostics collector (#299, #300).
//
// Every function and class here compiles to nothing unless
// SIL_RUN_DIAGNOSTICS is defined, so the ordinary `sil-run` carries no clock
// read, no counter and no branch on the activation path. The separately built
// and installed `sil-run-diagnostics` defines it and writes one
// `sil_run_diagnostics` v1 report (docs/diagnostics-report.md) after the Run.
//
// The collector only observes. Nothing it measures reaches the Recording, the
// Manifest hash, scheduling or the Run's exit code; it reads the runner's real
// monotonic clock, which the Participant Clock shim never interposes.

#include <sys/resource.h>
#include <sys/types.h>

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <optional>
#include <string>

namespace sil {

class Replayer;

namespace diagnostics {

enum class ParticipantKind { Native, Process, Replay };
enum class TaskKind { NativeTask, ProcessStep };

// Non-overlapping lifecycle intervals, in the order a Run passes them. A Run
// that fails early never enters the later ones.
enum class Phase {
  Preflight,              // Manifest load, preparation, Recording open
  Setup,                  // Engine construction, Participant init/spawn
  ScheduledExecution,     // the Slot loop
  Shutdown,               // Process shutdown, Run directory, Engine teardown
  RecordingFinalization,  // Recording close
  kCount,
};

// What the runner knows about the Run when it writes the report.
struct RunFacts {
  std::string manifest_path;
  std::optional<std::string> manifest_hash;
  std::optional<uint64_t> duration_ns;
  std::string sil_version;
  std::string source_repository;
  std::string source_revision;
  int exit_code = 0;
  // Present for a failed Run: the first diagnostic and its facts.
  std::optional<std::string> failure_message;
  std::optional<std::string> failure_category;
  std::optional<std::string> failure_participant;
  std::optional<uint64_t> failure_virtual_time_ns;
  bool recording = true;
  std::string recording_path;
  std::string provenance_path;
  std::string provenance_mode;
  std::optional<std::string> failure_report_path;
  std::optional<uint64_t> participant_timeout_ms;
  uint64_t max_protocol_line_bytes = 0;
  uint64_t max_step_output_messages = 0;
  uint64_t max_step_inline_payload_bytes = 0;
};

#ifdef SIL_RUN_DIAGNOSTICS

// The id a Task's or a Replay participant's work is recorded under.
using Id = size_t;

// Run wall time starts here, before the Manifest is loaded.
void start_run() noexcept;
// Closes the current phase and opens `phase`; re-entering it is a no-op.
void enter_phase(Phase phase) noexcept;
// Run wall time ends here, after Participant cleanup and Recording close.
void end_run() noexcept;

void participant(const std::string &name, ParticipantKind kind) noexcept;
// Returns the id an Activation names the Task by.
Id task(const std::string &participant, const std::string &task,
        TaskKind kind, uint64_t period_ns, uint64_t offset_ns) noexcept;
Id replay(const std::string &participant) noexcept;
// The host time just before a Process participant's fork.
class SpawnTime {
 public:
  SpawnTime() noexcept;
  uint64_t ns() const { return ns_; }

 private:
  uint64_t ns_;
};
// A Process participant's child was forked as `pid` at `forked`.
void process_spawned(const std::string &participant, pid_t pid,
                     const SpawnTime &forked) noexcept;
// A Process participant's child was reaped: `usage` is what wait4 reported
// for it and its waited descendants, or null when the reap failed.
void process_reaped(const std::string &participant, pid_t pid,
                    const rusage *usage) noexcept;

// Streams Activation observations (#316) to `path`, a new file, with at most
// `limit_bytes` of records (docs/profile-report.md). Call before start_run.
// False with `error` when the file cannot be made; the Run has not started.
bool observe_activations(const std::filesystem::path &path,
                         uint64_t limit_bytes, std::string &error);

// Starts the Process participant `participant` as `wrapper COMMAND...`
// instead of `COMMAND...` (#317): a profiler's adapter that must exec the
// command, so the participant keeps its PID, pipes, working directory,
// arguments and environment. The Manifest and its hash do not change.
void wrap_launch(const std::string &participant,
                 const std::filesystem::path &wrapper);
// The wrapper of `participant`, or null.
const std::string *launch_wrapper(const std::string &participant) noexcept;

// Writes the report as a new file through a temporary sibling. Throws on any
// write error; the caller reports it without changing the Run's exit code.
void write_report(const std::filesystem::path &path, const RunFacts &facts);

// Times one Task activation inclusively, from before the call into the Task
// to its return. An activation that is not marked completed was failed, or
// interrupted when a termination signal had arrived.
class Activation {
 public:
  Activation(Id task, uint64_t slot_ns) noexcept;
  ~Activation();
  void completed() noexcept;
  // Completed unless the Engine recorded a failure during the activation.
  void completed_unless(const std::string &failure) noexcept {
    if (failure.empty()) completed();
  }

 private:
  Id task_;
  uint64_t slot_ns_;
  uint64_t start_ns_;
  bool done_ = false;
};

// Times one Replay publication Slot: a publish_due call that had at least one
// Message due. `publications` is the Engine's global publish order counter,
// read before and after to count the Messages it published.
class ReplayWork {
 public:
  ReplayWork(Id replay, const Replayer &replayer, uint64_t slot_ns,
             const uint64_t &publications) noexcept;
  ~ReplayWork();
  void completed() noexcept;
  // Completed unless the Engine recorded a failure during the publication.
  void completed_unless(const std::string &failure) noexcept {
    if (failure.empty()) completed();
  }

 private:
  Id replay_;
  uint64_t slot_ns_;
  const uint64_t &publications_;
  uint64_t publications_before_;
  uint64_t start_ns_;
  bool due_;
  bool done_ = false;
};

// Enters `phase` when the scope ends, normally or by an exception, so the
// teardown that follows is attributed to it.
class PhaseOnExit {
 public:
  explicit PhaseOnExit(Phase phase) noexcept : phase_(phase) {}
  ~PhaseOnExit() { enter_phase(phase_); }

 private:
  Phase phase_;
};

#else

// Empty, so a [[no_unique_address]] id member takes no storage in sil-run.
struct Id {};

inline void start_run() noexcept {}
inline void enter_phase(Phase) noexcept {}
inline void end_run() noexcept {}
inline void participant(const std::string &, ParticipantKind) noexcept {}
inline Id task(const std::string &, const std::string &, TaskKind, uint64_t,
               uint64_t) noexcept {
  return {};
}
inline Id replay(const std::string &) noexcept { return {}; }
class SpawnTime {};
inline void process_spawned(const std::string &, pid_t,
                            const SpawnTime &) noexcept {}
inline void process_reaped(const std::string &, pid_t,
                           const rusage *) noexcept {}
// No wrapper: the ordinary runner execs the Manifest command only.
inline const std::string *launch_wrapper(const std::string &) noexcept {
  return nullptr;
}

class Activation {
 public:
  Activation(Id, uint64_t) noexcept {}
  void completed() noexcept {}
  void completed_unless(const std::string &) noexcept {}
};

class ReplayWork {
 public:
  ReplayWork(Id, const Replayer &, uint64_t, const uint64_t &) noexcept {}
  void completed() noexcept {}
  void completed_unless(const std::string &) noexcept {}
};

class PhaseOnExit {
 public:
  explicit PhaseOnExit(Phase) noexcept {}
};

#endif

}  // namespace diagnostics
}  // namespace sil
