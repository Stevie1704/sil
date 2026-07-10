#pragma once

#include <sys/types.h>

#include <cstdint>
#include <string>
#include <vector>

#include "sil/clock_region.h"

#include "engine.hpp"

namespace sil {

// An opaque vECU run as a child process, stepped over a JSON-lines pipe
// protocol on stdin/stdout. Fully sequential request/response: deterministic
// by construction regardless of what the child does internally in between.
//
//   kernel -> child  {"op":"init","name":...,"channels":{...},"schemas":{...},
//                     "config_version":1}
//   child  -> kernel {"op":"ready"}
//   kernel -> child  {"op":"step","t":...,"dt":...,
//                     "in":[{"ch":...,"t":...,"data":<base64>}...]}
//   child  -> kernel {"op":"step_done","out":[{"ch":...,"data":<base64>}...]}
//                 or {"op":"fail","reason":...}
//   kernel -> child  {"op":"shutdown"}
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
  // the child's own clock reads return stepped virtual time. Left inert (fd -1,
  // region null) for unshimmed participants.
  int region_fd_ = -1;
  std::string region_path_;
  std::string shim_lib_;  // resolved in the parent so the child only setenv()s
  volatile sil_clock_region *region_ = nullptr;
  uint64_t epoch_ns_ = 0;

  void setup_clock_region();
  void inject_shim_env() const;  // runs in the forked child before exec
  void write_clock_region(uint64_t now_ns);
  void teardown_clock_region();
};

}  // namespace sil
