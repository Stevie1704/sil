#include "run_diagnostics.hpp"

#ifdef SIL_RUN_DIAGNOSTICS

#include <fcntl.h>
#include <sys/stat.h>
#include <sys/utsname.h>
#include <unistd.h>
#ifdef __APPLE__
#include <sys/sysctl.h>
#endif

#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <cstring>
#include <fstream>
#include <stdexcept>
#include <thread>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

#include "diagnostics_stats.hpp"
#include "replayer.hpp"
#include "run_signal.hpp"

namespace sil::diagnostics {

namespace {

using nlohmann::json;
namespace fs = std::filesystem;

constexpr int kReportVersion = 1;
constexpr int kCollectorVersion = 1;
// The reservoir seeds are fixed, so two reports of the same Run retain the
// same sample positions; each distribution adds its ordinal.
constexpr uint64_t kBaseSeed = 0x53494C4449414731ULL;  // "SILDIAG1"

const char *const kPhaseNames[] = {
    "preflight_ns", "setup_ns", "scheduled_execution_ns", "shutdown_ns",
    "recording_finalization_ns",
};
static_assert(sizeof kPhaseNames / sizeof *kPhaseNames ==
              size_t(Phase::kCount));

uint64_t now_ns() {
  return uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(
                      std::chrono::steady_clock::now().time_since_epoch())
                      .count());
}

enum class Outcome { Completed, Failed, Interrupted };

struct FailedActivation {
  uint64_t activation_index;
  uint64_t virtual_time_ns;
  uint64_t wall_ns;
  Outcome outcome;
};

// Counts and timing shared by a Task and a Replay participant's publication
// Slots: both are attempted, then completed, failed or interrupted.
struct WorkRecord {
  explicit WorkRecord(uint64_t seed) : timing(seed) {}

  uint64_t attempted = 0;
  uint64_t completed = 0;
  uint64_t failed = 0;
  uint64_t interrupted = 0;
  DurationDistribution timing;
  std::optional<FailedActivation> failed_activation;

  void record(uint64_t slot_ns, uint64_t wall_ns, Outcome outcome) {
    const uint64_t index = attempted++;
    if (outcome == Outcome::Completed) {
      completed++;
      timing.add(index, slot_ns, wall_ns);
      return;
    }
    (outcome == Outcome::Failed ? failed : interrupted)++;
    if (!failed_activation)
      failed_activation = FailedActivation{index, slot_ns, wall_ns, outcome};
  }
};

struct TaskRecord {
  std::string participant;
  std::string task;
  TaskKind kind;
  uint64_t period_ns;
  uint64_t offset_ns;
  WorkRecord work;
};

struct ReplayRecord {
  std::string participant;
  uint64_t publications = 0;
  WorkRecord work;
};

struct ParticipantRecord {
  std::string name;
  ParticipantKind kind;
};

struct Collector {
  std::optional<uint64_t> run_start_ns;
  std::optional<uint64_t> run_end_ns;
  std::optional<Phase> current_phase;
  uint64_t phase_start_ns = 0;
  std::array<std::optional<uint64_t>, size_t(Phase::kCount)> phase_ns;
  std::optional<uint64_t> last_virtual_time_ns;
  std::vector<ParticipantRecord> participants;
  std::vector<TaskRecord> tasks;
  std::vector<ReplayRecord> replays;
  uint64_t next_seed = kBaseSeed;
  bool lost_measurement = false;

  void close_phase(uint64_t at_ns) {
    if (!current_phase) return;
    std::optional<uint64_t> &total = phase_ns[size_t(*current_phase)];
    total = total.value_or(0) + (at_ns - phase_start_ns);
    current_phase.reset();
  }

  void observe_slot(uint64_t slot_ns) {
    if (!last_virtual_time_ns || slot_ns > *last_virtual_time_ns)
      last_virtual_time_ns = slot_ns;
  }
};

Collector &collector() {
  static Collector instance;
  return instance;
}

// Instrumentation must never replace or abort the Run it observes: a
// measurement that cannot be stored is reported as lost instead.
template <typename Update>
void guarded(Update update) noexcept {
  try {
    update(collector());
  } catch (...) {
    try {
      collector().lost_measurement = true;
    } catch (...) {
    }
  }
}

const char *task_kind_name(TaskKind kind) {
  return kind == TaskKind::NativeTask ? "native_task" : "process_step";
}

const char *timing_scope(TaskKind kind) {
  return kind == TaskKind::NativeTask ? "inclusive_native_activation"
                                      : "inclusive_process_step";
}

const char *participant_kind_name(ParticipantKind kind) {
  switch (kind) {
    case ParticipantKind::Native:
      return "native";
    case ParticipantKind::Process:
      return "process";
    case ParticipantKind::Replay:
      break;
  }
  return "replay";
}

const char *outcome_name(Outcome outcome) {
  return outcome == Outcome::Interrupted ? "interrupted" : "failed";
}

// Sets `key` to `value`, or to null with `reason` in the object's
// `unavailable` map. Every null measurement in the report carries a reason.
template <typename T>
void set_or_unavailable(json &object, const char *key,
                        const std::optional<T> &value, const char *reason) {
  if (value) {
    object[key] = *value;
  } else {
    object[key] = nullptr;
    object["unavailable"][key] = reason;
  }
}

std::optional<double> finite_ratio(double numerator, double denominator) {
  if (denominator <= 0) return std::nullopt;
  const double ratio = numerator / denominator;
  if (!std::isfinite(ratio)) return std::nullopt;
  return ratio;
}

json timing_json(const DurationDistribution &d, const char *scope) {
  json j = {{"scope", scope}, {"completed", d.count()},
            {"sum_ns", d.sum_ns()}, {"unavailable", json::object()}};
  const char *empty = "no_completed_activations";
  const bool any = d.count() > 0;
  set_or_unavailable(j, "min_ns", any ? std::optional(d.min_ns()) : std::nullopt,
                     empty);
  set_or_unavailable(j, "max_ns", any ? std::optional(d.max_ns()) : std::nullopt,
                     empty);
  set_or_unavailable(j, "mean_ns", d.mean_ns(), empty);
  set_or_unavailable(j, "stddev_ns", d.stddev_ns(), empty);
  const std::pair<const char *, Percentile> percentiles[] = {
      {"p50_ns", Percentile::P50},
      {"p95_ns", Percentile::P95},
      {"p99_ns", Percentile::P99},
  };
  for (const auto &[key, p] : percentiles) {
    const PercentileValue value = d.percentile(p);
    set_or_unavailable(j, key, value.value_ns,
                       value.unavailable_reason ? value.unavailable_reason
                                                : empty);
  }
  std::vector<uint64_t> samples = d.samples();
  std::sort(samples.begin(), samples.end());
  j["quantiles"] = {
      {"method", "nearest_rank"},
      {"approximate", d.approximate()},
      {"sample_count", d.count()},
      {"retained_count", samples.size()},
      {"reservoir",
       {{"algorithm", "algorithm_r"},
        {"generator", "splitmix64"},
        {"algorithm_version", 1},
        {"capacity", DurationDistribution::kReservoirCapacity},
        {"seed", d.seed()}}},
  };
  j["samples_ns"] = samples;
  json slowest = json::array();
  for (const SlowActivation &a : d.slowest())
    slowest.push_back({{"activation_index", a.activation_index},
                       {"virtual_time_ns", a.virtual_time_ns},
                       {"wall_ns", a.duration_ns}});
  j["slowest"] = std::move(slowest);
  return j;
}

json counts_json(const WorkRecord &work) {
  json j = {{"attempted", work.attempted},
            {"completed", work.completed},
            {"failed", work.failed},
            {"interrupted", work.interrupted},
            {"failed_activation", nullptr}};
  if (work.failed_activation)
    j["failed_activation"] = {
        {"activation_index", work.failed_activation->activation_index},
        {"virtual_time_ns", work.failed_activation->virtual_time_ns},
        {"outcome", outcome_name(work.failed_activation->outcome)}};
  return j;
}

json failed_wall_json(const WorkRecord &work) {
  if (!work.failed_activation) return nullptr;
  return {{"activation_index", work.failed_activation->activation_index},
          {"wall_ns", work.failed_activation->wall_ns}};
}

// The resource records #301 fills in. This collector version reserves their
// shape and reports every value as not collected.
json reserved_resource(const char *scope) {
  json j = {{"scope", scope}, {"unavailable", json::object()}};
  const char *fields[] = {"collector",      "interval",
                          "includes_waited_descendants",
                          "user_cpu_ns",    "system_cpu_ns",
                          "peak_rss_bytes", "peak_rss_kind"};
  for (const char *field : fields)
    set_or_unavailable(j, field, std::optional<int>(), "not_collected");
  return j;
}

std::optional<std::string> cpu_description() {
#ifdef __APPLE__
  char brand[256];
  size_t size = sizeof brand;
  if (sysctlbyname("machdep.cpu.brand_string", brand, &size, nullptr, 0) == 0)
    return std::string(brand);
#else
  std::ifstream cpuinfo("/proc/cpuinfo");
  std::string line;
  while (std::getline(cpuinfo, line)) {
    if (line.rfind("model name", 0) != 0) continue;
    const size_t colon = line.find(':');
    if (colon == std::string::npos) continue;
    const size_t start = line.find_first_not_of(" \t", colon + 1);
    if (start != std::string::npos) return line.substr(start);
  }
#endif
  return std::nullopt;
}

json host_json() {
  json j = {{"unavailable", json::object()}};
  utsname names;
  const bool known = uname(&names) == 0;
  const char *reason = "not_reported_by_os";
  set_or_unavailable(j, "os",
                     known ? std::optional<std::string>(names.sysname)
                           : std::nullopt,
                     reason);
  set_or_unavailable(j, "os_release",
                     known ? std::optional<std::string>(names.release)
                           : std::nullopt,
                     reason);
  set_or_unavailable(j, "architecture",
                     known ? std::optional<std::string>(names.machine)
                           : std::nullopt,
                     reason);
  const unsigned cpus = std::thread::hardware_concurrency();
  set_or_unavailable(j, "logical_cpu_count",
                     cpus ? std::optional<unsigned>(cpus) : std::nullopt,
                     reason);
  set_or_unavailable(j, "cpu_description", cpu_description(), reason);
  return j;
}

std::string run_status(const RunFacts &facts) {
  if (facts.exit_code == 0) return "succeeded";
  if (facts.exit_code == 2) return "configuration_error";
  if (facts.failure_category && *facts.failure_category == "interrupted")
    return "interrupted";
  return "run_failure";
}

json execution_json(const Collector &c, const RunFacts &facts) {
  json participants = json::array();
  for (const ParticipantRecord &p : c.participants) {
    json tasks = json::array();
    for (const TaskRecord &t : c.tasks) {
      if (t.participant != p.name) continue;
      json row = {{"task", t.task},
                  {"kind", task_kind_name(t.kind)},
                  {"period_ns", t.period_ns},
                  {"offset_ns", t.offset_ns}};
      row.update(counts_json(t.work));
      tasks.push_back(std::move(row));
    }
    json row = {{"participant", p.name},
                {"kind", participant_kind_name(p.kind)},
                {"tasks", std::move(tasks)},
                {"replay", nullptr}};
    for (const ReplayRecord &r : c.replays) {
      if (r.participant != p.name) continue;
      json replay = {{"publications", r.publications}};
      replay.update(counts_json(r.work));
      row["replay"] = std::move(replay);
    }
    participants.push_back(std::move(row));
  }
  json j = {{"participants", std::move(participants)},
            {"unavailable", json::object()}};
  set_or_unavailable(j, "duration_ns", facts.duration_ns,
                     "manifest_not_loaded");
  set_or_unavailable(j, "last_virtual_time_ns", c.last_virtual_time_ns,
                     "no_slot_opened");
  return j;
}

json observational_json(const Collector &c, const RunFacts &facts) {
  json wall = {{"boundary",
                "native runner start before Manifest load through "
                "Participant cleanup and Recording close"},
               {"unavailable", json::object()}};
  std::optional<uint64_t> wall_ns;
  if (c.run_start_ns && c.run_end_ns) wall_ns = *c.run_end_ns - *c.run_start_ns;
  set_or_unavailable(wall, "wall_ns", wall_ns, "not_measured");

  json phases = {{"unavailable", json::object()}};
  uint64_t attributed = 0;
  for (size_t i = 0; i < size_t(Phase::kCount); i++) {
    const char *reason = "not_reached";
    if (Phase(i) == Phase::RecordingFinalization && !facts.recording)
      reason = "no_recording";
    set_or_unavailable(phases, kPhaseNames[i], c.phase_ns[i], reason);
    attributed += c.phase_ns[i].value_or(0);
  }
  std::optional<uint64_t> unattributed;
  if (wall_ns && *wall_ns >= attributed) unattributed = *wall_ns - attributed;
  set_or_unavailable(phases, "unattributed_ns", unattributed, "not_measured");

  json tasks = json::array();
  uint64_t native_completed = 0;
  uint64_t steps_completed = 0;
  for (const TaskRecord &t : c.tasks) {
    (t.kind == TaskKind::NativeTask ? native_completed : steps_completed) +=
        t.work.completed;
    tasks.push_back({{"participant", t.participant},
                     {"task", t.task},
                     {"kind", task_kind_name(t.kind)},
                     {"timing", timing_json(t.work.timing, timing_scope(t.kind))},
                     {"failed_activation", failed_wall_json(t.work)}});
  }
  json replays = json::array();
  for (const ReplayRecord &r : c.replays)
    replays.push_back(
        {{"participant", r.participant},
         {"timing",
          timing_json(r.work.timing, "inclusive_replay_publication_slot")},
         {"failed_activation", failed_wall_json(r.work)}});

  const std::optional<uint64_t> scheduled =
      c.phase_ns[size_t(Phase::ScheduledExecution)];
  const double scheduled_s = scheduled ? double(*scheduled) / 1e9 : 0.0;
  json throughput = {{"denominator", "scheduled_execution_ns"},
                     {"completed_native_activations", native_completed},
                     {"completed_process_steps", steps_completed},
                     {"unavailable", json::object()}};
  set_or_unavailable(throughput, "scheduled_execution_ns", scheduled,
                     "not_reached");
  set_or_unavailable(throughput, "native_activations_per_s",
                     finite_ratio(double(native_completed), scheduled_s),
                     "no_scheduled_execution");
  set_or_unavailable(throughput, "process_steps_per_s",
                     finite_ratio(double(steps_completed), scheduled_s),
                     "no_scheduled_execution");

  json speed = {{"unavailable", json::object()}};
  std::optional<double> scheduled_factor;
  std::optional<double> end_to_end_factor;
  const char *speed_reason = "run_not_successful";
  if (facts.exit_code == 0 && facts.duration_ns) {
    speed_reason = "no_scheduled_execution";
    // Virtual nanoseconds per wall nanosecond: above 1 is faster than real
    // time.
    scheduled_factor = finite_ratio(double(*facts.duration_ns),
                                    double(scheduled.value_or(0)));
    if (wall_ns)
      end_to_end_factor =
          finite_ratio(double(*facts.duration_ns), double(*wall_ns));
  }
  set_or_unavailable(speed, "scheduled_factor", scheduled_factor,
                     speed_reason);
  set_or_unavailable(speed, "end_to_end_factor", end_to_end_factor,
                     speed_reason);

  json processes = json::array();
  for (const ParticipantRecord &p : c.participants) {
    if (p.kind != ParticipantKind::Process) continue;
    json record = reserved_resource("process_participant");
    record["participant"] = p.name;
    processes.push_back(std::move(record));
  }
  json resources = {{"runner", reserved_resource("runner_process")},
                    {"process_participants", std::move(processes)},
                    {"native_participants", "shared_with_runner"}};

  return {{"run_wall_time", std::move(wall)},
          {"phases", std::move(phases)},
          {"tasks", std::move(tasks)},
          {"replay", std::move(replays)},
          {"throughput", std::move(throughput)},
          {"virtual_time_speed", std::move(speed)},
          {"resources", std::move(resources)}};
}

json report_json(const Collector &c, const RunFacts &facts) {
  json failure = nullptr;
  if (facts.exit_code != 0) {
    failure = {{"message", facts.failure_message.value_or("")},
               {"category", facts.failure_category.value_or("run")},
               {"participant", nullptr},
               {"virtual_time_ns", nullptr}};
    if (facts.failure_participant)
      failure["participant"] = *facts.failure_participant;
    if (facts.failure_virtual_time_ns)
      failure["virtual_time_ns"] = *facts.failure_virtual_time_ns;
  }
  json reasons = json::array();
  if (facts.exit_code != 0) reasons.push_back(run_status(facts));
  json collection_errors = json::array();
  if (c.lost_measurement)
    collection_errors.push_back(
        {{"scope", "collector"},
         {"message", "a measurement could not be stored"}});

  json identity = {{"manifest_path", facts.manifest_path},
                   {"sil_version", facts.sil_version},
                   {"source_repository", facts.source_repository},
                   {"source_revision", facts.source_revision},
                   {"unavailable", json::object()}};
  set_or_unavailable(identity, "manifest_hash", facts.manifest_hash,
                     "manifest_not_loaded");

  json configuration = {
      {"measurement_mode", "diagnostics"},
      {"recording", facts.recording},
      {"participant_timeout_ms", nullptr},
      {"max_protocol_line_bytes", facts.max_protocol_line_bytes},
      {"max_step_output_messages", facts.max_step_output_messages},
      {"max_step_inline_payload_bytes", facts.max_step_inline_payload_bytes},
  };
  if (facts.participant_timeout_ms)
    configuration["participant_timeout_ms"] = *facts.participant_timeout_ms;

  json artifacts = {{"recording", nullptr},
                    {"provenance", facts.provenance_path},
                    {"failure_report", nullptr}};
  if (facts.recording) artifacts["recording"] = facts.recording_path;
  if (facts.failure_report_path)
    artifacts["failure_report"] = *facts.failure_report_path;

  return {
      {"sil_run_diagnostics", kReportVersion},
      {"collector",
       {{"name", "sil-run-diagnostics"},
        {"collector_version", kCollectorVersion},
        {"clock", "host_monotonic"},
        {"slowest_retained", DurationDistribution::kSlowestRetained},
        {"p95_minimum_samples", DurationDistribution::kP95MinimumSamples},
        {"p99_minimum_samples", DurationDistribution::kP99MinimumSamples}}},
      {"identity", std::move(identity)},
      {"host", host_json()},
      {"configuration", std::move(configuration)},
      {"run",
       {{"exit_code", facts.exit_code},
        {"status", run_status(facts)},
        {"failure", std::move(failure)}}},
      {"completeness",
       {{"run_complete", facts.exit_code == 0}, {"reasons", std::move(reasons)}}},
      {"collection_errors", std::move(collection_errors)},
      {"execution", execution_json(c, facts)},
      {"observational", observational_json(c, facts)},
      {"artifacts", std::move(artifacts)},
  };
}

std::string errno_text(int error) { return std::strerror(error); }

// Publishes `content` at `path` only if nothing is there yet: the bytes go to
// a temporary sibling first and are then linked into place, so a reader never
// sees a partial report and a prior report is never replaced.
void publish_new_file(const fs::path &path, const std::string &content) {
  std::string name = path.string() + ".tmp-XXXXXX";
  const int fd = mkstemp(name.data());
  if (fd < 0)
    throw std::runtime_error("cannot create a temporary file beside '" +
                             path.string() + "': " + errno_text(errno));
  const mode_t mask = umask(0);
  umask(mask);
  fchmod(fd, 0666 & ~mask);
  size_t written = 0;
  while (written < content.size()) {
    const ssize_t n =
        ::write(fd, content.data() + written, content.size() - written);
    if (n < 0 && errno == EINTR) continue;
    if (n <= 0) {
      const int error = errno;
      ::close(fd);
      ::unlink(name.c_str());
      throw std::runtime_error("cannot write '" + name + "': " +
                               errno_text(error));
    }
    written += size_t(n);
  }
  if (::close(fd) != 0) {
    const int error = errno;
    ::unlink(name.c_str());
    throw std::runtime_error("cannot close '" + name + "': " +
                             errno_text(error));
  }
  if (::link(name.c_str(), path.c_str()) != 0) {
    const int error = errno;
    ::unlink(name.c_str());
    throw std::runtime_error("cannot publish '" + path.string() +
                             "': " + errno_text(error));
  }
  ::unlink(name.c_str());
}

}  // namespace

void start_run() noexcept {
  guarded([](Collector &c) {
    c.run_start_ns = now_ns();
    c.current_phase = Phase::Preflight;
    c.phase_start_ns = *c.run_start_ns;
  });
}

void enter_phase(Phase phase) noexcept {
  guarded([phase](Collector &c) {
    if (c.current_phase == phase) return;
    const uint64_t at = now_ns();
    c.close_phase(at);
    c.current_phase = phase;
    c.phase_start_ns = at;
  });
}

void end_run() noexcept {
  guarded([](Collector &c) {
    const uint64_t at = now_ns();
    c.close_phase(at);
    c.run_end_ns = at;
  });
}

void participant(const std::string &name, ParticipantKind kind) noexcept {
  guarded([&](Collector &c) { c.participants.push_back({name, kind}); });
}

size_t task(const std::string &participant, const std::string &task,
            TaskKind kind, uint64_t period_ns, uint64_t offset_ns) noexcept {
  size_t id = 0;
  guarded([&](Collector &c) {
    c.tasks.push_back({participant, task, kind, period_ns, offset_ns,
                       WorkRecord(c.next_seed++)});
    id = c.tasks.size() - 1;
  });
  return id;
}

size_t replay(const std::string &participant) noexcept {
  size_t id = 0;
  guarded([&](Collector &c) {
    c.replays.push_back({participant, 0, WorkRecord(c.next_seed++)});
    id = c.replays.size() - 1;
  });
  return id;
}

void write_report(const fs::path &path, const RunFacts &facts) {
  const std::string text =
      report_json(collector(), facts)
          .dump(2, ' ', false, json::error_handler_t::replace) +
      "\n";
  publish_new_file(path, text);
}

Activation::Activation(size_t task, uint64_t slot_ns) noexcept
    : task_(task), slot_ns_(slot_ns), start_ns_(now_ns()) {}

void Activation::completed() noexcept {
  const uint64_t wall = now_ns() - start_ns_;
  done_ = true;
  guarded([&](Collector &c) {
    c.observe_slot(slot_ns_);
    c.tasks.at(task_).work.record(slot_ns_, wall, Outcome::Completed);
  });
}

Activation::~Activation() {
  if (done_) return;
  const uint64_t wall = now_ns() - start_ns_;
  const Outcome outcome =
      run_interrupted() ? Outcome::Interrupted : Outcome::Failed;
  guarded([&](Collector &c) {
    c.observe_slot(slot_ns_);
    c.tasks.at(task_).work.record(slot_ns_, wall, outcome);
  });
}

ReplayWork::ReplayWork(size_t replay, const Replayer &replayer,
                       uint64_t slot_ns, const uint64_t &publications) noexcept
    : replay_(replay),
      slot_ns_(slot_ns),
      publications_(publications),
      publications_before_(publications),
      start_ns_(now_ns()),
      due_(replayer.next_publish_ns() == slot_ns) {}

void ReplayWork::completed() noexcept {
  if (!due_) return;
  const uint64_t wall = now_ns() - start_ns_;
  done_ = true;
  guarded([&](Collector &c) {
    c.observe_slot(slot_ns_);
    ReplayRecord &r = c.replays.at(replay_);
    r.publications += publications_ - publications_before_;
    r.work.record(slot_ns_, wall, Outcome::Completed);
  });
}

ReplayWork::~ReplayWork() {
  if (!due_ || done_) return;
  const uint64_t wall = now_ns() - start_ns_;
  const Outcome outcome =
      run_interrupted() ? Outcome::Interrupted : Outcome::Failed;
  guarded([&](Collector &c) {
    c.observe_slot(slot_ns_);
    ReplayRecord &r = c.replays.at(replay_);
    r.publications += publications_ - publications_before_;
    r.work.record(slot_ns_, wall, outcome);
  });
}

}  // namespace sil::diagnostics

#endif
