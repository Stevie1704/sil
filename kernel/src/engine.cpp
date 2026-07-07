#include "engine.hpp"

#include <algorithm>
#include <cstring>
#include <limits>
#include <map>
#include <set>

#include "native_participant.hpp"
#include "process_participant.hpp"
#include "recorder.hpp"
#include "replayer.hpp"

namespace sil {

namespace {

// Byte layout of each field type: little-endian, declared order, no padding —
// the same wire contract participants pack to (see sil/schema.py and
// tools/silschema.py). Maps a type name to its width and whether it is signed
// integer, unsigned integer, or float, so an override constant can be encoded
// into a message the same way its producer would have.
struct TypeLayout {
  size_t size;
  enum { kUnsigned, kSigned, kFloat } repr;
};

const std::map<std::string, TypeLayout> kTypeLayouts = {
    {"u8", {1, TypeLayout::kUnsigned}},  {"u16", {2, TypeLayout::kUnsigned}},
    {"u32", {4, TypeLayout::kUnsigned}}, {"u64", {8, TypeLayout::kUnsigned}},
    {"i8", {1, TypeLayout::kSigned}},    {"i16", {2, TypeLayout::kSigned}},
    {"i32", {4, TypeLayout::kSigned}},   {"i64", {8, TypeLayout::kSigned}},
    {"f32", {4, TypeLayout::kFloat}},    {"f64", {8, TypeLayout::kFloat}}};

// Writes `value`, interpreted as type `layout`, little-endian into `dst`.
// `value` is already range-checked at load, so the numeric conversions here
// cannot overflow their target type.
void encode_le(const TypeLayout &layout, double value, uint8_t *dst) {
  uint64_t bits = 0;
  switch (layout.repr) {
    case TypeLayout::kUnsigned:
      bits = static_cast<uint64_t>(value);
      break;
    case TypeLayout::kSigned:
      bits = static_cast<uint64_t>(static_cast<int64_t>(value));
      break;
    case TypeLayout::kFloat:
      if (layout.size == 4) {
        float f = static_cast<float>(value);
        std::memcpy(&bits, &f, sizeof(f));
      } else {
        std::memcpy(&bits, &value, sizeof(value));
      }
      break;
  }
  for (size_t i = 0; i < layout.size; i++)
    dst[i] = static_cast<uint8_t>(bits >> (8 * i));
}

}  // namespace

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

  // Open-loop replay must not race live production on the same channel: a
  // channel a live participant publishes cannot also be replayed. Process
  // participants declare their publishes in the manifest, so the collision is
  // caught here, at load, before any message flows.
  std::set<std::string> live_published;
  for (const ParticipantSpec &p : manifest_.participants)
    if (const auto *proc = std::get_if<ProcessSpec>(&p.impl))
      for (const std::string &ch : proc->publishes) live_published.insert(ch);

  // Manifest order is name-sorted: registration indices, and with them all
  // scheduling tie-breaks, are independent of authoring order.
  for (const ParticipantSpec &p : manifest_.participants) {
    if (const auto *native = std::get_if<NativeSpec>(&p.impl)) {
      natives_.push_back(std::make_unique<NativeParticipant>(
          *this, p.name, *native, manifest_.base_dir));
    } else if (const auto *replay = std::get_if<ReplaySpec>(&p.impl)) {
      for (const std::string &ch : replay->channels)
        if (live_published.count(ch))
          throw ManifestError("manifest error: participant '" + p.name +
                              "': replayed channel '" + ch +
                              "' is also published by a live participant");
      replayers_.push_back(
          std::make_unique<Replayer>(*this, p.name, *replay, manifest_.base_dir));
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

void Engine::apply_overrides(const ChannelState &c, uint64_t publish_ns,
                             std::vector<uint8_t> &bytes) const {
  for (const InterceptorSpec &i : c.spec->interceptors) {
    if (i.kind != "override") continue;
    if (publish_ns < i.start_ns || (i.end_ns && publish_ns >= *i.end_ns))
      continue;
    // Field offset is the sum of preceding field widths (no padding). The
    // field's presence in the schema was validated at load, so the lookups
    // below always resolve.
    size_t offset = 0;
    for (const FieldSpec &f : c.schema->fields) {
      const TypeLayout &layout = kTypeLayouts.at(f.type);
      if (f.name == i.field) {
        encode_le(layout, i.value, bytes.data() + offset);
        break;
      }
      offset += layout.size;
    }
  }
}

void Engine::publish(const std::string &owner, const std::string &channel,
                     const void *data, size_t len) {
  ChannelState &c = channel_or_fail(channel, "participant '" + owner + "'");
  if (len != c.schema->byte_size)
    throw RunError("participant '" + owner + "' published " +
                   std::to_string(len) + " bytes on '" + channel +
                   "' but schema '" + c.spec->schema + "' is " +
                   std::to_string(c.schema->byte_size) + " bytes");

  // Fault injection choke point: a `delay` interceptor whose half-open window
  // [start_ns, end_ns) contains the actual publish time shifts the message's
  // visibility — and the recorded ground truth — later by delay_ns. Multiple
  // matching delays compose in declared order. Other kinds are still inert.
  // The sum saturates: delay_ns is only bounded to 2^64-1 at load, so an
  // overflowing total clamps to the max and is dropped below rather than
  // wrapping around into the visible range.
  uint64_t visible_ns = now_ns_;
  for (const InterceptorSpec &i : c.spec->interceptors) {
    if (i.kind != "delay") continue;
    if (now_ns_ < i.start_ns || (i.end_ns && now_ns_ >= *i.end_ns)) continue;
    uint64_t sum = visible_ns + *i.delay_ns;
    visible_ns = sum < visible_ns ? UINT64_MAX : sum;
  }

  // A message whose shifted visibility lands at or beyond the run duration
  // never surfaces: drop it before it is recorded or enqueued, matching the
  // replayer's [0, duration) truncation. A sequence number is not consumed.
  if (visible_ns >= manifest_.duration_ns) {
    global_seq_++;
    return;
  }

  // An `override` interceptor rewrites the named field to a constant on the
  // same choke point, keyed on the actual publish time. The rewrite happens
  // before recording and enqueueing, so the MCAP is the post-interceptor
  // ground truth and every subscriber sees the overridden value.
  const uint8_t *p = static_cast<const uint8_t *>(data);
  PendingMessage msg{visible_ns, global_seq_++,
                     std::vector<uint8_t>(p, p + len)};
  apply_overrides(c, now_ns_, msg.bytes);
  if (recorder_)
    recorder_->record(c.index, visible_ns, c.next_seq, msg.bytes.data(),
                      msg.bytes.size());
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
    // A replay timestamp can fall between task periods; it must still open a
    // slot so the message is published at its recorded virtual time.
    for (const auto &r : replayers_)
      slot = std::min(slot, r->next_publish_ns());
    if (slot == std::numeric_limits<uint64_t>::max()) break;

    std::vector<Task *> due;
    for (Task &t : tasks_)
      if (!t.done && t.next_ns == slot) due.push_back(&t);
    std::sort(due.begin(), due.end(), [](const Task *a, const Task *b) {
      if (a->priority != b->priority) return a->priority < b->priority;
      return a->registration_index < b->registration_index;
    });

    now_ns_ = slot;
    // Replayed messages enter the router before any task activation in the
    // slot, so a consumer stepped in this slot sees them exactly as it saw
    // the original live production.
    for (const auto &r : replayers_) r->publish_due(slot);
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
