#pragma once

#include <cstdint>
#include <deque>
#include <functional>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

#include "manifest.hpp"

namespace sil {

// Failure while executing an otherwise valid manifest. Runner exits 1.
struct RunError : std::runtime_error {
  using std::runtime_error::runtime_error;
};

class Recorder;
class NativeParticipant;
class ProcessParticipant;

struct PendingMessage {
  uint64_t publish_ns;
  uint64_t global_seq;  // total publish order across all channels
  std::vector<uint8_t> bytes;
};

// One subscriber's view of one channel: FIFO in publish order, messages
// become visible according to the channel's declared latency.
class SubQueue {
 public:
  SubQueue(std::string channel, std::optional<uint64_t> latency_ns)
      : channel_(std::move(channel)), latency_ns_(latency_ns) {}

  const std::string &channel() const { return channel_; }

  bool visible_at(uint64_t now_ns) const {
    if (pending_.empty()) return false;
    const PendingMessage &m = pending_.front();
    // Default semantics: visible at the consumer's next activation after
    // publish, so results are independent of execution order within a slot.
    // Explicit latency L: visible at publish + L (L=0 is declared
    // same-slot feedthrough).
    return latency_ns_ ? m.publish_ns + *latency_ns_ <= now_ns
                       : m.publish_ns < now_ns;
  }

  uint64_t front_seq() const { return pending_.front().global_seq; }

  PendingMessage pop() {
    PendingMessage m = std::move(pending_.front());
    pending_.pop_front();
    return m;
  }

  void push(PendingMessage m) { pending_.push_back(std::move(m)); }

 private:
  std::string channel_;
  std::optional<uint64_t> latency_ns_;
  std::deque<PendingMessage> pending_;
};

class Engine {
 public:
  Engine(const Manifest &manifest, Recorder *recorder);
  ~Engine();

  // Loads native libraries, spawns process participants, collects task
  // registrations. Throws ManifestError for unloadable configs.
  void setup();

  // Steps virtual time until the manifest duration is reached or a
  // participant fails. Throws RunError on failure.
  void run();

  // --- services used by participant adapters ---
  uint64_t now_ns() const { return now_ns_; }
  bool in_setup() const { return in_setup_; }
  const Manifest &manifest() const { return manifest_; }

  void register_task(const std::string &owner, const std::string &task,
                     uint64_t period_ns, uint64_t offset_ns, int32_t priority,
                     std::function<void(uint64_t)> fn);
  SubQueue *subscribe(const std::string &owner, const std::string &channel);
  void publish(const std::string &owner, const std::string &channel,
               const void *data, size_t len);
  bool take(SubQueue &queue, PendingMessage &out);
  // Marks the run failed; the loop aborts after the current callback returns.
  void fail(const std::string &owner, const std::string &reason);

 private:
  struct Task {
    std::string name;
    uint64_t period_ns;
    uint64_t next_ns;
    int32_t priority;
    size_t registration_index;
    std::function<void(uint64_t)> fn;
    bool done = false;
  };

  struct ChannelState {
    const ChannelSpec *spec;
    const SchemaSpec *schema;
    uint32_t index;
    uint32_t next_seq = 0;
    std::vector<SubQueue *> subscribers;
  };

  ChannelState &channel_or_fail(const std::string &name,
                                const std::string &ctx);

  const Manifest &manifest_;
  Recorder *recorder_;
  std::vector<ChannelState> channels_;  // manifest (name-sorted) order
  std::vector<std::unique_ptr<SubQueue>> queues_;
  std::vector<Task> tasks_;
  std::vector<std::unique_ptr<NativeParticipant>> natives_;
  std::vector<std::unique_ptr<ProcessParticipant>> processes_;
  uint64_t now_ns_ = 0;
  uint64_t global_seq_ = 0;
  bool in_setup_ = false;
  bool in_task_ = false;
  std::string failure_;
};

}  // namespace sil
