#pragma once

#include <chrono>
#include <cstdint>
#include <deque>
#include <filesystem>
#include <functional>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

#include "manifest.hpp"
#include "virtual_time.hpp"

namespace sil {

// Failure while executing an otherwise valid manifest. Runner exits 1.
struct RunError : std::runtime_error {
  using std::runtime_error::runtime_error;
};

class RecordingSink;
class NativeParticipant;
class OwnedDirectory;
class ProcessParticipant;
class Replayer;

struct PendingMessage {
  uint64_t publish_ns;
  uint64_t global_seq;  // total publish order across all channels
  std::vector<uint8_t> bytes;
};

// One subscriber's view of one channel: FIFO in publish order, messages
// become visible according to the channel's declared latency.
class SubscriberRoute {
 public:
  enum class PushResult { Enqueued, DroppedNewest, OverflowFailure };

  SubscriberRoute(std::string owner, std::string channel,
                  std::optional<uint64_t> latency_ns,
                  std::optional<size_t> capacity, OverflowPolicy overflow)
      : owner_(std::move(owner)),
        channel_(std::move(channel)),
        latency_ns_(latency_ns),
        capacity_(capacity),
        overflow_(overflow) {}

  const std::string &owner() const { return owner_; }
  const std::string &channel() const { return channel_; }
  size_t depth() const { return pending_.size(); }
  const std::optional<size_t> &capacity() const { return capacity_; }

  bool visible_at(uint64_t now_ns) const {
    if (pending_.empty()) return false;
    // Strict FIFO in publish order: only the front is ever inspected. A delay
    // interceptor can give an early message a later visibility than a message
    // published behind it on the same channel; that message then waits for the
    // delayed front rather than overtaking it, so per-channel order is
    // preserved (an intentional choice over reordering by shifted time).
    const PendingMessage &m = pending_.front();
    // Default semantics: visible at the consumer's next activation after
    // publish, so results are independent of execution order within a slot.
    // Explicit latency L: visible at publish + L (L=0 is declared
    // same-slot feedthrough). A visibility the clock cannot represent lies
    // beyond every slot the run can open, so the message is never delivered.
    // The run duration needs no separate test here: a slot only opens for a
    // task activation or a replay timestamp below the duration, so a visible
    // time that passes `<= now_ns` is inside the run by construction.
    if (!latency_ns_) return m.publish_ns < now_ns;
    const std::optional<uint64_t> visible_ns =
        virtual_time_after(m.publish_ns, *latency_ns_);
    return visible_ns && *visible_ns <= now_ns;
  }

  uint64_t front_seq() const { return pending_.front().global_seq; }

  PendingMessage pop() {
    PendingMessage m = std::move(pending_.front());
    pending_.pop_front();
    return m;
  }

  PushResult push(const PendingMessage &m) {
    if (capacity_ && pending_.size() >= *capacity_)
      return overflow_ == OverflowPolicy::DropNewest
                 ? PushResult::DroppedNewest
                 : PushResult::OverflowFailure;
    pending_.push_back(m);
    return PushResult::Enqueued;
  }

 private:
  std::string owner_;
  std::string channel_;
  std::optional<uint64_t> latency_ns_;
  std::optional<size_t> capacity_;
  OverflowPolicy overflow_;
  std::deque<PendingMessage> pending_;
};

class Engine {
 public:
  Engine(const Manifest &manifest, RecordingSink *recorder,
         std::optional<std::chrono::milliseconds> participant_timeout =
             std::nullopt);
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
  bool in_task() const { return in_task_; }
  const Manifest &manifest() const { return manifest_; }
  const std::filesystem::path &run_working_directory();

  void register_task(const std::string &owner, const std::string &task,
                     uint64_t period_ns, uint64_t offset_ns, int32_t priority,
                     std::function<void(uint64_t)> fn);
  SubscriberRoute *subscribe(const std::string &owner,
                             const SubscriberRouteSpec &route);
  void publish(const std::string &owner, const std::string &channel,
               const void *data, size_t len);
  bool take(SubscriberRoute &route, PendingMessage &out);
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
    std::unique_ptr<InterceptorPlan> interceptor_plan;
    std::vector<SubscriberRoute *> subscriber_routes;
  };

  ChannelState &channel_or_fail(const std::string &name,
                                const std::string &ctx);

  const Manifest &manifest_;
  RecordingSink *recorder_;
  std::optional<std::chrono::milliseconds> participant_timeout_;
  std::vector<ChannelState> channels_;  // manifest (name-sorted) order
  std::vector<std::unique_ptr<SubscriberRoute>> subscriber_routes_;
  std::vector<Task> tasks_;
  std::vector<std::unique_ptr<NativeParticipant>> natives_;
  // Declared before processes_ so reverse-order destruction reaps every child
  // before removing their common Run directory.
  std::unique_ptr<OwnedDirectory> run_working_directory_;
  std::vector<std::unique_ptr<ProcessParticipant>> processes_;
  std::vector<std::unique_ptr<Replayer>> replayers_;
  uint64_t now_ns_ = 0;
  uint64_t global_seq_ = 0;
  bool in_setup_ = false;
  bool in_task_ = false;
  std::string failure_;
};

}  // namespace sil
