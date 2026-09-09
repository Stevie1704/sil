// Bench fixture: subscribes to one Channel and drains it every activation, for
// the routing baseline in issue #61.
//
// Several subscribers in one Run share one loaded library, so every instance
// keeps its own state on the heap. The payload checksum exists so the take
// cannot be optimized away; it is never published.
#include <sil/participant.h>

#include <cstdint>
#include <memory>
#include <string>

#include <nlohmann/json.hpp>

namespace {

struct Subscriber {
  const sil_api_v1 *api;
  std::string input;
  uint64_t checksum = 0;
};

void drain(void *user, uint64_t) {
  auto *s = static_cast<Subscriber *>(user);
  const void *data;
  size_t len;
  // take returns 1 per Message and 0 when the queue is drained. An error is
  // already the Run's recorded failure by the time it returns, so the loop
  // needs no separate error branch.
  while (s->api->take(s->api->ctx, s->input.c_str(), &data, &len) == 1) {
    // Touch the first and last byte: enough to prove the payload is readable
    // without walking megabytes per message.
    const auto *bytes = static_cast<const uint8_t *>(data);
    if (len) s->checksum += bytes[0] + bytes[len - 1];
  }
}

}  // namespace

extern "C" int sil_participant_init(const sil_api_v1 *api, const char *,
                                    const char *config_json) {
  nlohmann::json cfg = nlohmann::json::parse(config_json);
  auto subscriber = std::make_unique<Subscriber>();
  subscriber->api = api;
  subscriber->input = cfg.at("input").get<std::string>();
  if (api->subscribe(api->ctx, subscriber->input.c_str()) != SIL_OK) return SIL_ERR;

  const uint64_t period = cfg.at("period_ns").get<uint64_t>();
  const int32_t priority = cfg.value("priority", 1);
  Subscriber *raw = subscriber.release();  // owned by the run; freed at process exit
  return api->register_task(api->ctx, "drain", period, 0, priority, drain, raw);
}
