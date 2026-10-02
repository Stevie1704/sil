/* The pinned library's own computation, timed without SiL (issue #232).
 *
 * native_acceptance.py writes every frame and transmit candidate of the
 * Native nominal Run, in Run order, and starts this program once per repeat:
 * the library keeps its state in C globals, so one process is one fresh
 * instance. The program binds the library, sets the recorded contract and
 * builds every CANPacket_t first. It then times only the library calls, made
 * as native_adapter.c makes them: per event set_timer, safety_tick when warm,
 * safety_fwd_hook and safety_rx_hook per frame, again set_timer and
 * safety_tick before the candidates, safety_tx_hook per candidate, and the
 * state getters. No Message, routing, Step, replay or Recording is inside the
 * timed interval. It writes one libsafety.NativeState per event, so the
 * acceptance can require that this computation is the one the Run published.
 *
 * Input: records of one kind byte (0 frame, 1 candidate) and one
 * can.TimedFrame, the frames of an event before its candidates.
 *
 *   library_cost <libsafety.so> <input> <output> <mode> <param>
 *       <alternative_experience> <timer_origin_ns> <timer_unit_ns>
 *       <first_event_ns> <last_event_ns>
 *
 * It prints {"events": n, "frames": n, "candidates": n, "elapsed_ns": n}.
 */
#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <inttypes.h>
#include <stdlib.h>
#include <time.h>

#include "libsafety_api.h"

enum { FRAME = 0, CANDIDATE = 1 };

#pragma pack(push, 1)
typedef struct record {
  uint8_t kind;
  can_TimedFrame frame;
} record;
#pragma pack(pop)

/* One event: its frames are [first, candidates), its candidates
 * [candidates, end). */
typedef struct event {
  uint64_t event_ns;
  size_t first;
  size_t candidates;
  size_t end;
} event;

typedef struct workload {
  size_t records;
  can_packet *packets;
  uint8_t *src;
  uint32_t *address;
  event *events;
  size_t event_count;
} workload;

static uint64_t now_ns(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static int refuse(const char *message, const char *detail) {
  fprintf(stderr, "library_cost: %s%s\n", message, detail ? detail : "");
  return 2;
}

static bool number(const char *text, uint64_t *out) {
  char *end;
  errno = 0;
  *out = strtoull(text, &end, 10);
  return *text != '\0' && *end == '\0' && errno == 0;
}

/* Reads the records into packets and event boundaries. */
static int load(const char *path, workload *w) {
  FILE *file = fopen(path, "rb");
  if (!file) return refuse("cannot open the input ", path);
  if (fseek(file, 0, SEEK_END) != 0) return refuse("cannot size ", path);
  long size = ftell(file);
  rewind(file);
  if (size <= 0 || size % (long)sizeof(record) != 0)
    return refuse("the input is not a whole number of records: ", path);
  w->records = (size_t)size / sizeof(record);
  record *records = malloc((size_t)size);
  w->packets = malloc(w->records * sizeof *w->packets);
  w->src = malloc(w->records);
  w->address = malloc(w->records * sizeof *w->address);
  w->events = malloc(w->records * sizeof *w->events);
  if (!records || !w->packets || !w->src || !w->address || !w->events)
    return refuse("cannot allocate the workload", NULL);
  if (fread(records, sizeof(record), w->records, file) != w->records)
    return refuse("cannot read ", path);
  fclose(file);

  w->event_count = 0;
  for (size_t i = 0; i < w->records; i++) {
    const record *r = &records[i];
    if (r->frame.length > MAX_CLASSIC_LENGTH || r->kind > CANDIDATE)
      return refuse("a record is not a classic CAN frame or candidate", NULL);
    event *last = w->event_count ? &w->events[w->event_count - 1] : NULL;
    if (r->kind == FRAME &&
        (!last || last->event_ns != r->frame.event_ns)) {
      if (last && r->frame.event_ns < last->event_ns)
        return refuse("the events are not in time order", NULL);
      last = &w->events[w->event_count++];
      *last = (event){r->frame.event_ns, i, i, i};
    }
    if (!last || last->event_ns != r->frame.event_ns ||
        (r->kind == FRAME && last->end != last->candidates))
      return refuse("a candidate has no event, or precedes a frame", NULL);
    if (r->kind == FRAME) last->candidates = i + 1;
    last->end = i + 1;
    packet(&r->frame, r->frame.src % 4, &w->packets[i]);
    w->src[i] = r->frame.src;
    w->address[i] = r->frame.address;
  }
  free(records);
  return 0;
}

static void compute(const library *lib, const event_policy *policy,
                    const workload *w, libsafety_NativeState *out) {
  for (size_t e = 0; e < w->event_count; e++) {
    const event *ev = &w->events[e];
    libsafety_NativeState *state = &out[e];
    state->event_ns = ev->event_ns;
    start_event(lib, policy, ev->event_ns);
    for (size_t i = ev->first; i < ev->candidates; i++) {
      lib->safety_fwd_hook(w->src[i], (int)w->address[i]);
      if (lib->safety_rx_hook(w->packets[i].bytes))
        state->accepted++;
      else
        state->rejected++;
    }
    if (ev->end > ev->candidates) start_event(lib, policy, ev->event_ns);
    for (size_t i = ev->candidates; i < ev->end; i++) {
      if (lib->safety_tx_hook(w->packets[i].bytes))
        state->tx_accepted++;
      else
        state->tx_rejected++;
    }
    read_state(lib, state);
  }
}

int main(int argc, char **argv) {
  if (argc != 11)
    return refuse("usage: library_cost <libsafety.so> <input> <output> <mode> "
                  "<param> <alternative_experience> <timer_origin_ns> "
                  "<timer_unit_ns> <first_event_ns> <last_event_ns>", NULL);
  uint64_t values[7];
  for (int i = 0; i < 7; i++)
    if (!number(argv[4 + i], &values[i]))
      return refuse("not an unsigned integer: ", argv[4 + i]);
  uint64_t mode = values[0], param = values[1], experience = values[2];
  event_policy policy = {values[3], values[4], values[5], values[6]};
  if (mode > UINT16_MAX || param > UINT16_MAX || experience > INT32_MAX ||
      policy.timer_unit_ns == 0)
    return refuse("the contract or the timer unit is out of range", NULL);

  workload w = {0};
  int status = load(argv[2], &w);
  if (status != 0) return status;
  library lib;
  char error[512];
  if (!bind(argv[1], &lib, error, sizeof error)) return refuse(error, NULL);
  if (lib.set_safety_hooks((uint16_t)mode, (uint16_t)param) != 0)
    return refuse("set_safety_hooks refused the contract", NULL);
  lib.set_alternative_experience((int)experience);
  libsafety_NativeState *out = calloc(w.event_count, sizeof *out);
  if (!out) return refuse("cannot allocate the outputs", NULL);

  uint64_t start = now_ns();
  compute(&lib, &policy, &w, out);
  uint64_t elapsed_ns = now_ns() - start;

  FILE *file = fopen(argv[3], "wb");
  if (!file || fwrite(out, sizeof *out, w.event_count, file) != w.event_count ||
      fclose(file) != 0)
    return refuse("cannot write ", argv[3]);
  size_t candidates = 0;
  for (size_t e = 0; e < w.event_count; e++)
    candidates += w.events[e].end - w.events[e].candidates;
  printf("{\"events\": %zu, \"frames\": %zu, \"candidates\": %zu, "
         "\"elapsed_ns\": %" PRIu64 "}\n",
         w.event_count, w.records - candidates, candidates, elapsed_ns);
  return 0;
}
