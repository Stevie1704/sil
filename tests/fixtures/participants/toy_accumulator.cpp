// Toy native participant: consumes toy.Counter messages and publishes a
// running toy.Accum (count, sum) every activation.
//
// State lives behind the `user` pointer this participant registers, never in a
// global, so one shared library backs any number of Participants in one Run.
// That is the rule at sil_participant_init in sil/participant.h.
#include <sil/participant.h>

#include <memory>
#include <string>

#include <nlohmann/json.hpp>

#include "toy_messages.h"

namespace {

struct Accumulator {
  const sil_api_v1 *api;
  std::string input;
  std::string output;
  uint64_t count = 0;
  int64_t sum = 0;
};

void accumulate(void *user, uint64_t) {
  auto *a = static_cast<Accumulator *>(user);
  const void *data;
  size_t len;
  int r;
  while ((r = a->api->take(a->api->ctx, a->input.c_str(), &data, &len)) == 1) {
    const auto *msg = static_cast<const toy_Counter *>(data);
    a->count++;
    a->sum += msg->value;
  }
  if (r != 0) return;  // fail() already raised by the kernel

  toy_Accum out;
  out.count = a->count;
  out.sum = a->sum;
  a->api->publish(a->api->ctx, a->output.c_str(), &out, sizeof out);
}

}  // namespace

extern "C" int sil_participant_init(const sil_api_v1 *api, const char *,
                                    const char *config_json) {
  nlohmann::json cfg = nlohmann::json::parse(config_json);
  auto accumulator = std::make_unique<Accumulator>();
  accumulator->api = api;
  accumulator->input = cfg.value("input", "ticks");
  accumulator->output = cfg.value("output", "sums");
  if (api->subscribe(api->ctx, accumulator->input.c_str()) != SIL_OK)
    return SIL_ERR;
  uint64_t period = cfg.value("period_ns", 10'000'000ULL);
  uint64_t offset = cfg.value("offset_ns", 0ULL);
  int32_t priority = cfg.value("priority", 0);
  // Owned by the run; freed at process exit.
  Accumulator *raw = accumulator.release();
  return api->register_task(api->ctx, "accumulate", period, offset, priority,
                            accumulate, raw);
}
