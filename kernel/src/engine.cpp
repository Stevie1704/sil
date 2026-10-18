#include "engine.hpp"

#include <algorithm>
#include <limits>
#include <optional>
#include <utility>

#include "copy_counters.hpp"
#include "interceptor.hpp"
#include "native_participant.hpp"
#include "owned_directory.hpp"
#include "process_participant.hpp"
#include "recording_sink.hpp"
#include "replayer.hpp"
#include "run_signal.hpp"

namespace sil {

Engine::Engine(PreparedRun &&prepared, RecordingSink *recorder,
               std::optional<std::chrono::milliseconds> participant_timeout,
               RunBoundaryLimits limits)
    : prepared_(std::move(prepared)), manifest_(prepared_.manifest()),
      recorder_(recorder),
      participant_timeout_(participant_timeout), limits_(limits) {
  for (const ChannelSpec &spec : manifest_.channels)
    if (!spec.interceptor_plan)
      throw ManifestError("manifest error: channel '" + spec.name +
                          "': missing compiled interceptor plan");

  channels_.reserve(manifest_.channels.size());
  for (uint32_t i = 0; i < manifest_.channels.size(); i++) {
    const ChannelSpec &spec = manifest_.channels[i];
    auto interceptor_plan = std::unique_ptr<InterceptorPlan>(
        new InterceptorPlan(*spec.interceptor_plan));
    channels_.push_back({&spec, &manifest_.schemas.at(spec.schema), i, 0,
                         std::move(interceptor_plan), {}});
  }
}

Engine::~Engine() = default;

const std::filesystem::path &Engine::run_working_directory() {
  if (!run_working_directory_) {
    std::string error;
    OwnedDirectory directory = OwnedDirectory::create_unique(
        std::filesystem::current_path(), ".sil-run-", error);
    if (!directory)
      throw ManifestError("cannot create Run working directory: " + error);
    run_working_directory_ =
        std::make_unique<OwnedDirectory>(std::move(directory));
  }
  return run_working_directory_->path();
}

Engine::ChannelState &Engine::channel_or_fail(const std::string &name,
                                              const std::string &ctx) {
  for (ChannelState &c : channels_)
    if (c.spec->name == name) return c;
  throw ManifestError("manifest error: " + ctx + ": unknown channel '" + name +
                      "'");
}

void Engine::setup() {
  if (run_interrupted()) throw RunError(run_interrupt_message());
  in_setup_ = true;

  validate_one_publisher_per_channel(manifest_);

  // Manifest order is name-sorted: registration indices, and with them all
  // scheduling tie-breaks, are independent of authoring order.
  for (const PreparedParticipantSpec &p : prepared_.participants()) {
    if (run_interrupted()) throw RunError(run_interrupt_message());
    if (const auto *native = std::get_if<PreparedNativeSpec>(&p.impl)) {
      diagnostics::participant(p.name, diagnostics::ParticipantKind::Native);
      natives_.push_back(
          std::make_unique<NativeParticipant>(*this, p.name, *native));
      // A contract violation during init is reported through fail(). A
      // participant that ignores the callback's return code must not carry
      // that failure into the run.
      if (!failure_.empty()) throw ManifestError("manifest error: " + failure_);
    } else if (const auto *replay = std::get_if<ReplaySpec>(&p.impl)) {
      diagnostics::participant(p.name, diagnostics::ParticipantKind::Replay);
      replayers_.push_back(
          std::make_unique<Replayer>(*this, p.name, *replay, manifest_.base_dir));
      replay_places_.push_back({replay->priority, next_registration_index_++,
                                diagnostics::replay(p.name)});
    } else {
      diagnostics::participant(p.name, diagnostics::ParticipantKind::Process);
      const auto &spec = std::get<PreparedProcessSpec>(p.impl);
      auto proc = std::make_unique<ProcessParticipant>(
          *this, p.name, spec, participant_timeout_, limits_);
      ProcessParticipant *raw = proc.get();
      add_task(p.name, "step", diagnostics::TaskKind::ProcessStep,
               spec.authored.step_period_ns, 0, spec.authored.priority,
               [raw](uint64_t now) { raw->step(now); });
      processes_.push_back(std::move(proc));
    }
  }
  in_setup_ = false;
}

void Engine::register_task(const std::string &owner, const std::string &task,
                           uint64_t period_ns, uint64_t offset_ns,
                           int32_t priority, std::function<void(uint64_t)> fn) {
  add_task(owner, task, diagnostics::TaskKind::NativeTask, period_ns,
           offset_ns, priority, std::move(fn));
}

void Engine::add_task(const std::string &owner, const std::string &task,
                      diagnostics::TaskKind kind, uint64_t period_ns,
                      uint64_t offset_ns, int32_t priority,
                      std::function<void(uint64_t)> fn) {
  if (period_ns == 0)
    throw ManifestError("manifest error: participant '" + owner + "' task '" +
                        task + "': period must be positive");
  Task t;
  t.name = owner + "/" + task;
  t.period_ns = period_ns;
  t.next_ns = offset_ns;
  t.priority = priority;
  t.registration_index = next_registration_index_++;
  t.fn = std::move(fn);
  t.done = t.next_ns >= manifest_.duration_ns;
  t.diagnostics_id =
      diagnostics::task(owner, task, kind, period_ns, offset_ns);
  tasks_.push_back(std::move(t));
}

SubscriberRoute *Engine::subscribe(const std::string &owner,
                                   const SubscriberRouteSpec &route) {
  ChannelState &c =
      channel_or_fail(route.channel, "participant '" + owner + "'");
  subscriber_routes_.push_back(std::make_unique<SubscriberRoute>(
      owner, route.channel, c.spec->latency_ns, route.capacity, route.overflow));
  counters::route_created(route.channel, owner);
  c.subscriber_routes.push_back(subscriber_routes_.back().get());
  return subscriber_routes_.back().get();
}

void Engine::publish(const std::string &owner, const std::string &channel,
                     const void *data, size_t len) {
  ChannelState &c = channel_or_fail(channel, "participant '" + owner + "'");
  if (len != c.schema->byte_size)
    throw RunError("participant '" + owner + "' published " +
                   std::to_string(len) + " bytes on '" + channel +
                   "' but schema '" + c.spec->schema + "' is " +
                   std::to_string(c.schema->byte_size) + " bytes");

  const uint8_t *p = static_cast<const uint8_t *>(data);
  counters::count(counters::Site::kCallerToKernel, len);
  PendingMessage msg{0, 0, std::vector<uint8_t>(p, p + len)};
  const InterceptorPlan::Verdict verdict =
      c.interceptor_plan->apply(now_ns_, msg.bytes);
  msg.global_seq = global_seq_++;
  if (verdict.suppressed) return;
  msg.publish_ns = verdict.visible_ns;
  if (recorder_) {
    counters::count(counters::Site::kRecorded, msg.bytes.size());
    recorder_->record(c.index, msg.publish_ns, c.next_seq, msg.bytes.data(),
                      msg.bytes.size());
  }
  c.next_seq++;
  for (SubscriberRoute *route : c.subscriber_routes) {
    const SubscriberRoute::PushResult result = route->push(msg);
    if (result == SubscriberRoute::PushResult::Enqueued) {
      counters::count(counters::Site::kSubscriberCopy, msg.bytes.size());
      counters::route_depth(route->channel(), route->owner(), route->depth());
      continue;
    }
    if (result == SubscriberRoute::PushResult::DroppedNewest) {
      counters::route_dropped_newest(route->channel(), route->owner());
      continue;
    }
    counters::route_overflow_failure(route->channel(), route->owner());
    throw RunError(
        "subscriber route capacity exceeded: Channel '" + channel +
        "', publisher '" + owner + "', subscriber '" + route->owner() +
        "', configured capacity " + std::to_string(*route->capacity()) +
        ", current depth " + std::to_string(route->depth()) +
        ", policy 'fail'");
  }
}

bool Engine::take(SubscriberRoute &route, PendingMessage &out) {
  if (!route.visible_at(now_ns_)) return false;
  out = route.pop();
  counters::route_depth(route.channel(), route.owner(), route.depth());
  return true;
}

void Engine::fail(const std::string &owner, const std::string &reason) {
  if (!failure_.empty()) return;
  failure_ = "participant '" + owner + "' failed: " + reason;
  failure_owner_ = owner;
  failure_ns_ = now_ns_;
}

void Engine::throw_failure() const {
  FailureFacts failure;
  failure.category = FailureCategory::Participant;
  failure.participant = failure_owner_;
  failure.virtual_time_ns = failure_ns_;
  throw ParticipantRunError(failure_, std::move(failure));
}

void Engine::run() {
  diagnostics::enter_phase(diagnostics::Phase::ScheduledExecution);
  for (;;) {
    if (run_interrupted()) throw RunError(run_interrupt_message());
    uint64_t slot = std::numeric_limits<uint64_t>::max();
    for (const Task &t : tasks_)
      if (!t.done) slot = std::min(slot, t.next_ns);
    // A replay timestamp can fall between task periods; it must still open a
    // slot so the message is published at its recorded virtual time.
    for (const auto &r : replayers_)
      slot = std::min(slot, r->next_publish_ns());
    if (slot == std::numeric_limits<uint64_t>::max()) break;

    // A replayer with a priority takes its place among the due tasks.
    struct Due {
      int32_t priority;
      size_t registration_index;
      Task *task;
      Replayer *replayer;
      size_t replay_index;
    };
    std::vector<Due> due;
    for (Task &t : tasks_)
      if (!t.done && t.next_ns == slot)
        due.push_back({t.priority, t.registration_index, &t, nullptr, 0});
    for (size_t i = 0; i < replayers_.size(); i++)
      if (replay_places_[i].priority)
        due.push_back({*replay_places_[i].priority,
                       replay_places_[i].registration_index, nullptr,
                       replayers_[i].get(), i});
    std::sort(due.begin(), due.end(), [](const Due &a, const Due &b) {
      if (a.priority != b.priority) return a.priority < b.priority;
      return a.registration_index < b.registration_index;
    });

    now_ns_ = slot;
    // A replayer without a priority publishes before any task activation in
    // the slot, so a consumer stepped in this slot sees its messages as it
    // saw a live publisher that ran first.
    for (size_t i = 0; i < replayers_.size(); i++) {
      if (replay_places_[i].priority) continue;
      diagnostics::ReplayWork work(replay_places_[i].diagnostics_id,
                                   *replayers_[i], slot, global_seq_);
      replayers_[i]->publish_due(slot);
      if (failure_.empty()) work.completed();
    }
    for (const Due &d : due) {
      if (d.replayer) {
        diagnostics::ReplayWork work(
            replay_places_[d.replay_index].diagnostics_id, *d.replayer, slot,
            global_seq_);
        d.replayer->publish_due(slot);
        if (!failure_.empty()) throw_failure();
        work.completed();
        continue;
      }
      Task *t = d.task;
      // Inclusive: routing and recording called from inside the Task, and a
      // Process participant's whole Step exchange, are part of this time.
      diagnostics::Activation activation(t->diagnostics_id, slot);
      in_task_ = true;
      t->fn(slot);
      in_task_ = false;
      if (failure_.empty()) activation.completed();
      if (run_interrupted()) throw RunError(run_interrupt_message());
      if (!failure_.empty()) throw_failure();
      // A next activation the clock cannot represent, or one at or beyond the
      // half-open run duration, ends this task and nothing else. `next_ns`
      // stays at the activation just executed, so slot selection never sees
      // an earlier instant than the one it just left.
      const std::optional<uint64_t> next_ns =
          virtual_time_after(t->next_ns, t->period_ns);
      if (!next_ns || *next_ns >= manifest_.duration_ns)
        t->done = true;
      else
        t->next_ns = *next_ns;
    }
  }

  diagnostics::enter_phase(diagnostics::Phase::Shutdown);
  for (auto &proc : processes_) proc->shutdown();
  if (run_interrupted()) throw RunError(run_interrupt_message());
  run_working_directory_.reset();
}

}  // namespace sil
