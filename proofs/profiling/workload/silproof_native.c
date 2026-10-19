/* Native participant of the profiling proof (issue #314).
 *
 * One 10 ms Task. Activation k calls silproof_native_spin_even (k even) or
 * silproof_native_spin_odd (k odd), then silproof_native_alloc_site, which
 * allocates and frees SILPROOF_NATIVE_ALLOCS blocks of
 * SILPROOF_NATIVE_BLOCK bytes. Activation 0 also calls
 * silproof_native_retain_site, which keeps one block until the process ends.
 * It publishes the spin result on proof.native and a fixed acceleration on
 * fmu.In for the FMU participant.
 *
 * Build SILPROOF_MISMATCH to get a library with other code and so another
 * build ID: its debug file is the proof's mismatched debug file.
 */
#include "silproof.h"

#include <sil/participant.h>

#define SILPROOF_NATIVE_ITERATIONS 3000000ULL
#define SILPROOF_NATIVE_ALLOCS 16
#define SILPROOF_NATIVE_BLOCK 1024
#define SILPROOF_RETAINED_BLOCK 65536
#define SILPROOF_PERIOD_NS 10000000ULL

#if defined(SILPROOF_MISMATCH)
#define SILPROOF_EVEN_K 0x51ULL
#else
#define SILPROOF_EVEN_K 0x9E37ULL
#endif

typedef struct {
  const sil_api_v1 *api;
  uint64_t activation;
  void *retained;
} State;

static State g_state;

SILPROOF_HOT static uint64_t silproof_native_spin_even(uint64_t seed,
                                                       uint64_t iterations) {
  SILPROOF_SPIN_BODY(seed, iterations, SILPROOF_EVEN_K, 0xFFu);
}

SILPROOF_HOT static uint64_t silproof_native_spin_odd(uint64_t seed,
                                                      uint64_t iterations) {
  SILPROOF_SPIN_BODY(seed, iterations, 0x7F4AULL, 0x3Fu);
}

SILPROOF_HOT static void silproof_native_alloc_site(void) {
  for (int i = 0; i < SILPROOF_NATIVE_ALLOCS; i++) {
    void *block = malloc(SILPROOF_NATIVE_BLOCK);
    if (!block) return;
    silproof_touch(block, SILPROOF_NATIVE_BLOCK);
    free(block);
  }
}

SILPROOF_HOT static void *silproof_native_retain_site(void) {
  void *block = malloc(SILPROOF_RETAINED_BLOCK);
  if (block) silproof_touch(block, SILPROOF_RETAINED_BLOCK);
  return block;
}

static void put_u64(unsigned char *out, uint64_t value) {
  for (int i = 0; i < 8; i++) out[i] = (unsigned char)(value >> (8 * i));
}

static void native_task(void *user, uint64_t now_ns) {
  (void)now_ns;
  State *state = (State *)user;
  const sil_api_v1 *api = state->api;
  uint64_t k = state->activation++;
  int even = (k % 2) == 0;
  uint64_t begin = silproof_host_ns();
  uint64_t result = even
                        ? silproof_native_spin_even(k, SILPROOF_NATIVE_ITERATIONS)
                        : silproof_native_spin_odd(k, SILPROOF_NATIVE_ITERATIONS);
  uint64_t end = silproof_host_ns();
  silproof_interval("native", k,
                    even ? "silproof_native_spin_even"
                         : "silproof_native_spin_odd",
                    begin, end);
  silproof_native_alloc_site();
  if (k == 0) state->retained = silproof_native_retain_site();

  unsigned char native[16];
  put_u64(native, result);
  put_u64(native + 8, k);
  if (api->publish(api->ctx, "proof.native", native, sizeof native) != SIL_OK)
    api->fail(api->ctx, "publish proof.native");
  double acceleration = 0.5;
  if (api->publish(api->ctx, "fmu.In", &acceleration, sizeof acceleration) !=
      SIL_OK)
    api->fail(api->ctx, "publish fmu.In");
}

int sil_participant_init(const sil_api_v1 *api, const char *name,
                         const char *config_json) {
  (void)name;
  (void)config_json;
  if (api->abi_version != SIL_ABI_VERSION) return SIL_ERR;
  g_state.api = api;
  g_state.activation = 0;
  g_state.retained = NULL;
  return api->register_task(api->ctx, "hot", SILPROOF_PERIOD_NS, 0, 0,
                            native_task, &g_state);
}
