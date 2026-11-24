// Scheduler-trace native participant (#404). It registers the Tasks that its
// config lists, in that order. Each Activation publishes one toy.Counter
// marker on the participant's own Channel: `seq` is the Virtual time that the
// kernel passed to the Task, and `value` is the Task's index in the config
// list. The Recording then holds the complete ordered Activation trace.
//
// The interceptor drops every Publish at or after the Run duration, so an
// Activation there would leave no marker. The Task fails the Run instead.
//
// State lives behind the `user` pointer of each Task, never in a global, so
// one library backs any number of Participants in one Run.
#include <sil/participant.h>

#include <exception>
#include <string>

#include <nlohmann/json.hpp>

#include "toy_messages.h"

namespace {

struct Participant {
  const sil_api_v1 *api;
  std::string channel;
  uint64_t duration_ns;
};

struct TraceTask {
  const Participant *owner;
  int64_t index;
};

void activate(void *user, uint64_t now_ns) {
  const auto *task = static_cast<const TraceTask *>(user);
  const sil_api_v1 *api = task->owner->api;
  if (now_ns >= task->owner->duration_ns) {
    const std::string reason = "Activation at " + std::to_string(now_ns) +
                               " ns is at or after the run duration";
    api->fail(api->ctx, reason.c_str());
    return;
  }
  toy_Counter marker;
  marker.seq = now_ns;
  marker.value = task->index;
  if (api->publish(api->ctx, task->owner->channel.c_str(), &marker,
                   sizeof marker) != SIL_OK)
    api->fail(api->ctx, "publish failed");
}

int register_tasks(const sil_api_v1 *api, const nlohmann::json &cfg) {
  // Owned by the run; freed at process exit.
  auto *owner = new Participant{api, cfg.at("channel").get<std::string>(),
                                cfg.at("duration_ns").get<uint64_t>()};
  const nlohmann::json &tasks = cfg.at("tasks");
  for (size_t i = 0; i < tasks.size(); i++) {
    const nlohmann::json &t = tasks[i];
    auto *task = new TraceTask{owner, int64_t(i)};
    if (api->register_task(api->ctx, t.at("name").get<std::string>().c_str(),
                           t.at("period_ns").get<uint64_t>(),
                           t.at("offset_ns").get<uint64_t>(),
                           t.at("priority").get<int32_t>(), activate,
                           task) != SIL_OK)
      return SIL_ERR;
  }
  return SIL_OK;
}

}  // namespace

// No exception may cross the C ABI: a bad config fails init with SIL_ERR.
extern "C" int sil_participant_init(const sil_api_v1 *api, const char *,
                                    const char *config_json) {
  try {
    return register_tasks(api, nlohmann::json::parse(config_json));
  } catch (const std::exception &e) {
    api->fail(api->ctx, e.what());
    return SIL_ERR;
  }
}
