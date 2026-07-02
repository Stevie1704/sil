// Toy native participant: publishes toy.Counter on a channel at a fixed
// period. Walking-skeleton fixture, config-driven so tests can vary timing.
#include <sil/participant.h>

#include <string>

#include <nlohmann/json.hpp>

#include "toy_messages.h"

namespace {

struct Producer {
  const sil_api_v1 *api;
  std::string channel;
  uint64_t seq = 0;
};

Producer g_producer;

void tick(void *user, uint64_t) {
  auto *p = static_cast<Producer *>(user);
  toy_Counter msg;
  msg.seq = p->seq;
  msg.value = int64_t(p->seq) * 3;
  p->seq++;
  if (p->api->publish(p->api->ctx, p->channel.c_str(), &msg, sizeof msg) !=
      SIL_OK)
    p->api->fail(p->api->ctx, "publish failed");
}

}  // namespace

extern "C" int sil_participant_init(const sil_api_v1 *api, const char *,
                                    const char *config_json) {
  nlohmann::json cfg = nlohmann::json::parse(config_json);
  g_producer.api = api;
  g_producer.channel = cfg.value("channel", "ticks");
  uint64_t period = cfg.value("period_ns", 10'000'000ULL);
  uint64_t offset = cfg.value("offset_ns", 0ULL);
  int32_t priority = cfg.value("priority", 0);
  return api->register_task(api->ctx, "tick", period, offset, priority, tick,
                            &g_producer);
}
