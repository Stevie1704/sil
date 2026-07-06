#pragma once

#include <cstdint>
#include <filesystem>
#include <string>
#include <vector>

#include "manifest.hpp"

namespace sil {

class Engine;

// Re-publishes recorded channels from a prior run's MCAP as an ordinary
// scheduled participant. The recording is validated against the manifest at
// load time (content hash, channel presence, schema layout); each selected
// message is injected at its recorded virtual timestamp, before task
// activations in that slot, preserving the recording's global publish order
// for equal timestamps. Deterministic by construction: it lives inside the
// same stepped virtual-time world as every other participant.
class Replayer {
 public:
  Replayer(Engine &engine, const std::string &name, const ReplaySpec &spec,
           const std::filesystem::path &base_dir);

  // Recorded timestamp of the next un-published message, or UINT64_MAX when
  // the recording is exhausted. Lets the engine fold replay times into slot
  // selection so a message never falls between task slots.
  uint64_t next_publish_ns() const;

  // Publishes every message recorded at exactly now_ns, in recording order.
  // Runs before task activations in the slot. May raise via Engine::fail.
  void publish_due(uint64_t now_ns);

 private:
  struct Msg {
    uint64_t publish_ns;
    std::string channel;
    std::vector<uint8_t> bytes;
  };

  Engine &engine_;
  std::string name_;
  std::vector<Msg> messages_;  // recording order (== global publish order)
  size_t next_ = 0;
};

}  // namespace sil
