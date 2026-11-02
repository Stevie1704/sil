/* Proof-owned deterministic workload for #359. Build with -O2 -g.
 * Task 51, Activation 610 performs the known late work and eight allocations.
 * All 52 Tasks publish their identity twice at each equal Virtual time.
 * Changing WORK changes host work only, never the published values. */
#include <sil/participant.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#ifndef WORK
#define WORK 40000000
#endif

typedef struct {
  const sil_api_v1 *api;
  uint64_t task;
  uint64_t index;
} State;

static State states[52];
static volatile uint64_t sink;

__attribute__((noinline)) static void late_spin(void) {
  uint64_t result = 1;
  for (uint64_t i = 0; i < WORK; ++i)
    result = (result ^ i) * UINT64_C(6364136223846793005) + 1;
  sink = result;
}

__attribute__((noinline)) static void late_allocations(void) {
  for (int i = 0; i < 8; ++i) {
    volatile unsigned char *block = malloc(1024);
    if (!block) abort();
    block[0] = (unsigned char)i;
    sink = block[0];
    free((void *)block);
  }
}

static void put_u64(unsigned char *out, uint64_t value) {
  for (int i = 0; i < 8; ++i) out[i] = (unsigned char)(value >> (8 * i));
}

static void step(void *user, uint64_t now_ns) {
  (void)now_ns;
  State *state = user;
  uint64_t index = state->index++;
  if (state->task == 51 && index == 610) {
    late_spin();
    late_allocations();
  }
  unsigned char data[24];
  put_u64(data, state->task);
  put_u64(data + 8, index);
  for (uint64_t publication = 0; publication < 2; ++publication) {
    put_u64(data + 16, publication);
    if (state->api->publish(state->api->ctx, "events", data, sizeof data) != SIL_OK)
      state->api->fail(state->api->ctx, "proof publication failed");
  }
}

int sil_participant_init(const sil_api_v1 *api, const char *name,
                         const char *config_json) {
  (void)name;
  (void)config_json;
  if (api->abi_version != SIL_ABI_VERSION) return SIL_ERR;
  for (int i = 0; i < 52; ++i) {
    char task[16];
    snprintf(task, sizeof task, "task%02d", i);
    states[i] = (State){api, (uint64_t)i, 0};
    if (api->register_task(api->ctx, task, 1000000, 0, i,
                           step, &states[i]) != SIL_OK) return SIL_ERR;
  }
  return SIL_OK;
}
