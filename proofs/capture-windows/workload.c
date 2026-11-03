/* Optimized source-owned, deterministic late-spike workload. Host markers
 * are independent of the diagnostics seam and perf sampled observations. */
#define _POSIX_C_SOURCE 200809L
#include <sil/participant.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

typedef struct { uint64_t ns, task, index, edge; } Marker;
static Marker markers[4096];
static size_t used, lost;
static volatile uint64_t sink;
static uint64_t host_ns(void) {
  struct timespec t;
  if (clock_gettime(CLOCK_MONOTONIC, &t)) abort();
  return (uint64_t)t.tv_sec*1000000000 + t.tv_nsec;
}
static void mark(uint64_t task, uint64_t index, uint64_t edge) {
  if (used < 4096) markers[used++] = (Marker){host_ns(), task, index, edge};
  else ++lost;
}
__attribute__((noinline)) static void ordinary_spin(void) {
  uint64_t value = 1;
  for (uint64_t i = 0; i < 1500000; ++i)
    value = (value ^ i)*UINT64_C(6364136223846793005) + 1;
  sink = value;
}
__attribute__((noinline)) static void late_hot_function(void) {
  uint64_t value = 1;
  for (uint64_t i = 0; i < 120000000; ++i)
    value = (value ^ i)*UINT64_C(6364136223846793005) + 1;
  sink = value;
}
void window_work(uint64_t task, uint64_t index) {
  mark(task, index, 0);
  if (task == 0 && index == 610) late_hot_function();
  else ordinary_spin();
  mark(task, index, 1);
}
__attribute__((destructor)) static void finish(void) {
  const char *path = getenv("SIL_WINDOW_MARKERS");
  if (!path) return;
  FILE *out = fopen(path, "wx");
  if (!out) return;
  fprintf(out, "{\"lost\":%zu,\"events\":[", lost);
  for (size_t i = 0; i < used; ++i) {
    Marker *m = &markers[i];
    fprintf(out, "%s[%llu,%llu,%llu,%llu]", i ? "," : "",
            (unsigned long long)m->ns, (unsigned long long)m->task,
            (unsigned long long)m->index, (unsigned long long)m->edge);
  }
  fprintf(out, "]}\n");
  fclose(out);
}
typedef struct { const sil_api_v1 *api; uint64_t task, index; int fail; } State;
static State states[2];
static void step(void *user, uint64_t now) {
  (void)now;
  State *s = user;
  uint64_t index = s->index++;
  window_work(s->task, index);
  if (s->fail && s->task == 0 && index == 610) {
    s->api->fail(s->api->ctx, "named late failure"); return;
  }
  unsigned char data[16];
  for (int i = 0; i < 8; ++i) {
    data[i] = (unsigned char)(s->task >> (8*i));
    data[i+8] = (unsigned char)(index >> (8*i));
  }
  if (s->api->publish(s->api->ctx, "events", data, sizeof data) != SIL_OK)
    s->api->fail(s->api->ctx, "publication failed");
}
int sil_participant_init(const sil_api_v1 *api, const char *name,
                         const char *config) {
  (void)name;
  for (int i = 0; i < 2; ++i) {
    states[i] = (State){api, (uint64_t)i, 0, config && strstr(config, "true")};
    if (api->register_task(api->ctx, i ? "peer" : "late", 1000000, 0, i,
                           step, &states[i]) != SIL_OK) return SIL_ERR;
  }
  return SIL_OK;
}
