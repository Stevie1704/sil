// A C++ Native participant that calls a C library function with a source
// scope; its callback throws through that scope (#372, Q6). The participant
// catches the exception, so the Activation completes. The capture must show
// the scope as unbalanced and never make up its return.
#include <sil/participant.h>

#include <stdexcept>

#include "toy_messages.h"

extern "C" void scoped_call(void (*callback)(void));

namespace {

const sil_api_v1 *g_api;
uint64_t g_seq;

void throwing() { throw std::runtime_error("through the scope"); }

void tick(void *, uint64_t) {
  try {
    scoped_call(throwing);
  } catch (const std::runtime_error &) {
  }
  toy_Counter message{g_seq, int64_t(g_seq)};
  g_seq++;
  g_api->publish(g_api->ctx, "ticks", &message, sizeof message);
}

}  // namespace

extern "C" int sil_participant_init(const sil_api_v1 *api, const char *,
                                    const char *) {
  g_api = api;
  return api->register_task(api->ctx, "tick", 1000000, 0, 0, tick, nullptr);
}
