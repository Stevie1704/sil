// Toy native participant with two Tasks of different Periods (#300). Both
// publish toy.Counter on one Channel; `value` is the next number from the C
// library generator, so a Recording would change if anything else in the
// runner process drew from it. The diagnostics collector must not.
#include <sil/participant.h>

#include <cstdlib>
#include <memory>
#include <string>

#include <nlohmann/json.hpp>

#include "toy_messages.h"

namespace {

struct MultiTask {
  const sil_api_v1 *api;
  std::string channel;
  uint64_t seq = 0;
};

void publish_random(MultiTask *p) {
  toy_Counter msg;
  msg.seq = p->seq++;
  msg.value = int64_t(std::rand());
  if (p->api->publish(p->api->ctx, p->channel.c_str(), &msg, sizeof msg) !=
      SIL_OK)
    p->api->fail(p->api->ctx, "publish failed");
}

void fast(void *user, uint64_t) {
  publish_random(static_cast<MultiTask *>(user));
}

void slow(void *user, uint64_t) {
  publish_random(static_cast<MultiTask *>(user));
}

}  // namespace

extern "C" int sil_participant_init(const sil_api_v1 *api, const char *,
                                    const char *config_json) {
  nlohmann::json cfg = nlohmann::json::parse(config_json);
  std::srand(cfg.value("seed", 1u));
  auto state = std::make_unique<MultiTask>();
  state->api = api;
  state->channel = cfg.value("channel", "ticks");
  MultiTask *raw = state.release();  // owned by the run; freed at process exit
  if (api->register_task(api->ctx, "fast",
                         cfg.value("fast_period_ns", 1'000'000ULL), 0, 0, fast,
                         raw) != SIL_OK)
    return SIL_ERR;
  return api->register_task(api->ctx, "slow",
                            cfg.value("slow_period_ns", 5'000'000ULL), 0, 1,
                            slow, raw);
}
