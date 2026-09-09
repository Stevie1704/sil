// Bench fixture: publishes a fixed-size payload on one Channel, `burst` times
// per activation, for the routing baseline in issue #61.
//
// The payload buffer is filled once at init and only its leading sequence
// counter changes per publication: the measurement is of the routing path, not
// of a participant filling megabytes. The bytes stay a pure function of the
// publication index, so a run is still deterministic.
#include <sil/participant.h>

#include <cstdint>
#include <cstring>
#include <memory>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace {

struct Publisher {
  const sil_api_v1 *api;
  std::string channel;
  std::vector<uint8_t> payload;
  uint32_t burst;
  uint64_t seq = 0;
};

void publish(void *user, uint64_t) {
  auto *s = static_cast<Publisher *>(user);
  for (uint32_t i = 0; i < s->burst; i++) {
    std::memcpy(s->payload.data(), &s->seq, sizeof s->seq);
    s->seq++;
    if (s->api->publish(s->api->ctx, s->channel.c_str(), s->payload.data(),
                        s->payload.size()) != SIL_OK) {
      s->api->fail(s->api->ctx, "publish failed");
      return;
    }
  }
}

}  // namespace

extern "C" int sil_participant_init(const sil_api_v1 *api, const char *,
                                    const char *config_json) {
  nlohmann::json cfg = nlohmann::json::parse(config_json);
  // One instance per init call: the same library backs several bench
  // participants in one Run, so no state may live in a global.
  auto publisher = std::make_unique<Publisher>();
  publisher->api = api;
  publisher->channel = cfg.at("channel").get<std::string>();
  publisher->burst = cfg.value("burst", 1U);
  const size_t bytes = cfg.at("bytes").get<size_t>();
  publisher->payload.assign(bytes, 0);
  for (size_t i = sizeof(uint64_t); i < bytes; i++)
    publisher->payload[i] = uint8_t(i % 251);

  const uint64_t period = cfg.at("period_ns").get<uint64_t>();
  Publisher *raw = publisher.release();  // owned by the run; freed at process exit
  return api->register_task(api->ctx, "publish", period, 0, 0, publish, raw);
}
