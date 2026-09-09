// Bench fixture: subscribes to one channel and drains it every activation, for
// the routing baseline in issue #61.
//
// Several sinks in one run share one loaded library, so every instance keeps
// its own state on the heap. The payload checksum exists so the take cannot be
// optimized away; it is never published.
#include <sil/participant.h>

#include <cstdint>
#include <memory>
#include <string>

#include <nlohmann/json.hpp>

namespace {

struct Sink {
  const sil_api_v1 *api;
  std::string input;
  uint64_t taken = 0;
  uint64_t checksum = 0;
};

void drain(void *user, uint64_t) {
  auto *s = static_cast<Sink *>(user);
  const void *data;
  size_t len;
  int r;
  while ((r = s->api->take(s->api->ctx, s->input.c_str(), &data, &len)) == 1) {
    s->taken++;
    // Touch the first and last byte: enough to prove the payload is readable
    // without walking megabytes per message.
    const auto *bytes = static_cast<const uint8_t *>(data);
    if (len) s->checksum += bytes[0] + bytes[len - 1];
  }
  (void)r;  // a failure is already raised through the kernel
}

}  // namespace

extern "C" int sil_participant_init(const sil_api_v1 *api, const char *,
                                    const char *config_json) {
  nlohmann::json cfg = nlohmann::json::parse(config_json);
  auto sink = std::make_unique<Sink>();
  sink->api = api;
  sink->input = cfg.at("input").get<std::string>();
  if (api->subscribe(api->ctx, sink->input.c_str()) != SIL_OK) return SIL_ERR;

  const uint64_t period = cfg.at("period_ns").get<uint64_t>();
  const int32_t priority = cfg.value("priority", 1);
  Sink *raw = sink.release();  // owned by the run; freed at process exit
  return api->register_task(api->ctx, "drain", period, 0, priority, drain, raw);
}
