#include "engine.hpp"

#include <algorithm>
#include <limits>

#include "native_participant.hpp"
#include "process_participant.hpp"
#include "recorder.hpp"

namespace sil {

Engine::Engine(const Manifest &manifest, Recorder *recorder)
    : manifest_(manifest), recorder_(recorder) {
  channels_.reserve(manifest.channels.size());
  for (uint32_t i = 0; i < manifest.channels.size(); i++) {
    const ChannelSpec &spec = manifest.channels[i];
    channels_.push_back({&spec, &manifest.schemas.at(spec.schema), i, 0, {}});
  }
}

Engine::~Engine() = default;

Engine::ChannelState &Engine::channel_or_fail(const std::string &name,
                                              const std::string &ctx) {
  for (ChannelState &c : channels_)
    if (c.spec->name == name) return c;
  throw ManifestError("manifest error: " + ctx + ": unknown channel '" + name +
                      "'");
}

void Engine::setup() {
  in_setup_ = true;
  // Manifest order is name-sorted: registration indices, and with them all
  // scheduling tie-breaks, are independent of authoring order.
  for (const ParticipantSpec &p : manifest_.participants) {
    if (const auto *native = std::get_if<NativeSpec>(&p.impl)) {
      natives_.push_back(std::make_unique<NativeParticipant>(
          *this, p.name, *native, manifest_.base_dir));
    } else {
      const auto &spec = std::get<ProcessSpec>(p.impl);
      auto proc = std::make_unique<ProcessParticipant>(*this, p.name, spec);
      ProcessParticipant *raw = proc.get();
      register_task(p.name, "step", spec.step_period_ns, 0, spec.priority,
                    [raw](uint64_t now) { raw->step(now); });
      processes_.push_back(std::move(proc));
    }
  }
  in_setup_ = false;
}

void Engine::register_task(const std::string &owner, const std::string &task,
                           uint64_t period_ns, uint64_t offset_ns,
                           int32_t priority, std::function<void(uint64_t)> fn) {
  if (period_ns == 0)
    throw ManifestError("manifest error: participant '" + owner + "' task '" +
                        task + "': period must be positive");
  Task t;
  t.name = owner + "/" + task;
  t.period_ns = period_ns;
  t.next_ns = offset_ns;
  t.priority = priority;
  t.registration_index = tasks_.size();
  t.fn = std::move(fn);
  t.done = t.next_ns >= manifest_.duration_ns;
  tasks_.push_back(std::move(t));
}

SubQueue *Engine::subscribe(const std::string &owner,
                            const std::string &channel) {
  ChannelState &c = channel_or_fail(channel, "participant '" + owner + "'");
  queues_.push_back(std::make_unique<SubQueue>(channel, c.spec->latency_ns));
  c.subscribers.push_back(queues_.back().get());
  return queues_.back().get();
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
  PendingMessage msg{now_ns_, global_seq_++, std::vector<uint8_t>(p, p + len)};
  if (recorder_) recorder_->record(c.index, now_ns_, c.next_seq, data, len);
  c.next_seq++;
  for (SubQueue *q : c.subscribers) q->push(msg);
}

bool Engine::take(SubQueue &queue, PendingMessage &out) {
  if (!queue.visible_at(now_ns_)) return false;
  out = queue.pop();
  return true;
}

void Engine::fail(const std::string &owner, const std::string &reason) {
  if (failure_.empty())
    failure_ = "participant '" + owner + "' failed: " + reason;
}

void Engine::run() {
  for (;;) {
    uint64_t slot = std::numeric_limits<uint64_t>::max();
    for (const Task &t : tasks_)
      if (!t.done) slot = std::min(slot, t.next_ns);
    if (slot == std::numeric_limits<uint64_t>::max()) break;

    std::vector<Task *> due;
    for (Task &t : tasks_)
      if (!t.done && t.next_ns == slot) due.push_back(&t);
    std::sort(due.begin(), due.end(), [](const Task *a, const Task *b) {
      if (a->priority != b->priority) return a->priority < b->priority;
      return a->registration_index < b->registration_index;
    });

    now_ns_ = slot;
    for (Task *t : due) {
      in_task_ = true;
      t->fn(slot);
      in_task_ = false;
      if (!failure_.empty()) throw RunError(failure_);
      t->next_ns += t->period_ns;
      if (t->next_ns >= manifest_.duration_ns) t->done = true;
    }
  }

  for (auto &proc : processes_) proc->shutdown();
}

}  // namespace sil
