/* opendbc's safety library as a SiL Native participant (issue #232).
 *
 * The library has its own C API and exports no sil_participant_init. This
 * adapter loads the pinned libsafety.so with dlopen, from the path in its
 * config, and maps the upstream replay policy onto one Task, as adapter.py
 * does for the Process form:
 *
 * - Bursts. One recorded can event is one Burst on the frame Channel. Its
 *   frames are visible, with Latency 0, in the first Task activation at or
 *   after its instant. An activation that holds frames of two instants fails
 *   the Run: one observation per activation could no longer name its event.
 * - Time. The Native ABI's take() returns no Message time, so each frame
 *   carries its Virtual instant in event_ns (rebased by sil-window). The
 *   library's timer is ((timer_origin_ns + event_ns) / timer_unit_ns)
 *   % 0xFFFFFFFF; it reads no other clock.
 * - Receive. set_timer, safety_tick when more than 1 s from the first and
 *   the last event, then per frame safety_fwd_hook(src, address) and
 *   safety_rx_hook with bus src % 4.
 * - Transmit. The candidates of the same instant on the transmit Channel are
 *   upstream's sendcan event after the can event: set_timer, safety_tick when
 *   warm, then safety_tx_hook per candidate. A candidate with no can event at
 *   its instant fails the Run.
 * - Observation. One libsafety.NativeState per Burst: the instant, the
 *   receive and transmit verdict counts, safety_config_valid() and the state
 *   getters.
 *
 * Instances. The library keeps its state in C globals, and a second dlopen of
 * the same file in one process returns the same state. A Run therefore holds
 * at most one instance: a second Participant of this adapter is a Manifest
 * error.
 * Native ABI v1 has no termination callback and the library has no shutdown
 * call, so the library stays loaded until the runner process exits. The
 * library starts no thread and blocks on nothing; it runs only inside the
 * Task callback.
 *
 * LIBSAFETY_FAILURE_AT_EVENT, with LIBSAFETY_FAILURE_CRASH or
 * LIBSAFETY_FAILURE_HANG, builds the crash and hang controls: the adapter
 * raises SIGSEGV, or never returns, when that event (counted from 0) starts,
 * as a crash or a hang inside the library would. The library runs in the
 * runner's process, so a crash ends the runner and a hang stops the Run
 * until something outside it, the whole-case guard, ends it. */
#include <sil/participant.h>

#include <dlfcn.h>
#include <errno.h>
#include <inttypes.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "libsafety_messages.h"

#define MAX_TEXT 256
#define MAX_ENTRIES 16
/* A diagnostic holds a channel name and its numbers; a reason adds the
 * activation time. */
#define MESSAGE_BYTES 1024
#define REASON_BYTES (MESSAGE_BYTES + 64)
#define TIMER_MODULUS 0xFFFFFFFFull
#define TICK_MARGIN_NS 1000000000ull
/* opendbc's packed CANPacket_t: a 40-bit header, a checksum byte, 64 data
 * bytes. Classic CAN only: a length of 0 to 8 bytes is its own DLC. */
#define PACKET_BYTES 70
#define HEADER_BYTES 5
#define MAX_CLASSIC_LENGTH 8
#define EXTENDED_FROM 0x800u

/* --- configuration: a flat object of strings and unsigned integers ------ */

typedef struct entry {
  char key[MAX_TEXT];
  char text[MAX_TEXT];
  bool is_string;
  bool used;
} entry;

typedef struct config_doc {
  entry entries[MAX_ENTRIES];
  size_t count;
  char error[MAX_TEXT + 64];
} config_doc;

static const char *skip_space(const char *p) {
  while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r') p++;
  return p;
}

/* Reads a JSON string without escapes, or a run of digits, into out. */
static const char *read_value(const char *p, char *out, bool *is_string) {
  size_t n = 0;
  *is_string = *p == '"';
  if (*is_string) {
    for (p++; *p != '"'; p++) {
      if (*p == '\0' || *p == '\\' || n + 1 >= MAX_TEXT) return NULL;
      out[n++] = *p;
    }
    p++;
  } else {
    while (*p >= '0' && *p <= '9' && n + 1 < MAX_TEXT) out[n++] = *p++;
    if (n == 0) return NULL;
  }
  out[n] = '\0';
  return p;
}

static bool parse_config(const char *json, config_doc *doc) {
  memset(doc, 0, sizeof *doc);
  const char *p = skip_space(json);
  if (*p++ != '{') goto malformed;
  p = skip_space(p);
  if (*p == '}') {
    if (*skip_space(p + 1) == '\0') return true;
    goto malformed;
  }
  for (;;) {
    if (doc->count == MAX_ENTRIES) goto malformed;
    entry *e = &doc->entries[doc->count++];
    bool key_is_string;
    if (!(p = read_value(p, e->key, &key_is_string)) || !key_is_string)
      goto malformed;
    p = skip_space(p);
    if (*p++ != ':') goto malformed;
    if (!(p = read_value(skip_space(p), e->text, &e->is_string)))
      goto malformed;
    p = skip_space(p);
    if (*p == ',') {
      p = skip_space(p + 1);
      continue;
    }
    if (*p == '}' && *skip_space(p + 1) == '\0') return true;
    goto malformed;
  }
malformed:
  snprintf(doc->error, sizeof doc->error,
           "config must be a flat JSON object of unescaped strings and "
           "unsigned integers");
  return false;
}

static entry *find(config_doc *doc, const char *key, bool is_string) {
  for (size_t i = 0; i < doc->count; i++) {
    entry *e = &doc->entries[i];
    if (strcmp(e->key, key) != 0) continue;
    if (e->is_string != is_string) break;
    e->used = true;
    return e;
  }
  snprintf(doc->error, sizeof doc->error, "config needs %s '%s'",
           is_string ? "the string" : "the unsigned integer", key);
  return NULL;
}

static bool string_value(config_doc *doc, const char *key, char *out) {
  entry *e = find(doc, key, true);
  if (e) memcpy(out, e->text, MAX_TEXT);
  return e != NULL;
}

static bool unsigned_value(config_doc *doc, const char *key, uint64_t *out) {
  entry *e = find(doc, key, false);
  if (!e) return false;
  errno = 0;
  *out = strtoull(e->text, NULL, 10);
  if (errno == ERANGE) {
    snprintf(doc->error, sizeof doc->error,
             "config '%s' does not fit in 64 bits", key);
    return false;
  }
  return true;
}

static bool no_unknown_keys(config_doc *doc) {
  for (size_t i = 0; i < doc->count; i++)
    if (!doc->entries[i].used) {
      snprintf(doc->error, sizeof doc->error, "config has unknown key '%s'",
               doc->entries[i].key);
      return false;
    }
  return true;
}

/* --- the library's C API, as opendbc's libsafety harness declares it --- */

typedef struct library {
  int (*set_safety_hooks)(uint16_t mode, uint16_t param);
  void (*set_alternative_experience)(int mode);
  void (*set_timer)(uint32_t t);
  void (*safety_tick)(void);
  bool (*safety_config_valid)(void);
  int (*safety_fwd_hook)(int bus, int address);
  bool (*safety_rx_hook)(void *packet);
  bool (*safety_tx_hook)(void *packet);
  bool (*get_controls_allowed)(void);
  bool (*get_gas_pressed_prev)(void);
  bool (*get_brake_pressed_prev)(void);
  bool (*get_cruise_engaged_prev)(void);
  bool (*get_vehicle_moving)(void);
  bool (*get_acc_main_on)(void);
  float (*get_vehicle_speed_min)(void);
  float (*get_vehicle_speed_max)(void);
} library;

#define RESOLVE(name)                                                    \
  if (!(*(void **)&lib->name = dlsym(handle, #name))) {                 \
    snprintf(error, size, "library '%s' does not export '%s'", path,    \
             #name);                                                     \
    return false;                                                        \
  }

/* Loads the library and resolves every symbol before anything runs. */
static bool bind(const char *path, library *lib, char *error, size_t size) {
  void *handle = dlopen(path, RTLD_NOW | RTLD_LOCAL);
  if (!handle) {
    snprintf(error, size, "cannot load library '%s': %s", path, dlerror());
    return false;
  }
  RESOLVE(set_safety_hooks)
  RESOLVE(set_alternative_experience)
  RESOLVE(set_timer)
  RESOLVE(safety_tick)
  RESOLVE(safety_config_valid)
  RESOLVE(safety_fwd_hook)
  RESOLVE(safety_rx_hook)
  RESOLVE(safety_tx_hook)
  RESOLVE(get_controls_allowed)
  RESOLVE(get_gas_pressed_prev)
  RESOLVE(get_brake_pressed_prev)
  RESOLVE(get_cruise_engaged_prev)
  RESOLVE(get_vehicle_moving)
  RESOLVE(get_acc_main_on)
  RESOLVE(get_vehicle_speed_min)
  RESOLVE(get_vehicle_speed_max)
  return true;
}

/* The packed CANPacket_t upstream's make_CANPacket builds. Header bits,
 * least significant first: fd 1, bus 3, data_len_code 4, rejected 1,
 * returned 1, extended 1, addr 29. The checksum byte stays 0. */
static void packet(const can_TimedFrame *frame, int bus,
                   uint8_t out[PACKET_BYTES]) {
  uint64_t header = ((uint64_t)(bus & 0x7) << 1) |
                    ((uint64_t)frame->length << 4) |
                    ((uint64_t)frame->address << 11);
  if (frame->address >= EXTENDED_FROM) header |= 1u << 10;
  memset(out, 0, PACKET_BYTES);
  for (int i = 0; i < HEADER_BYTES; i++) out[i] = (uint8_t)(header >> (8 * i));
  const uint8_t data[MAX_CLASSIC_LENGTH] = {frame->d0, frame->d1, frame->d2,
                                            frame->d3, frame->d4, frame->d5,
                                            frame->d6, frame->d7};
  memcpy(out + HEADER_BYTES + 1, data, frame->length);
}

/* --- the Participant ---------------------------------------------------- */

typedef struct participant {
  const sil_api_v1 *api;
  library lib;
  char frames[MAX_TEXT];
  char transmit[MAX_TEXT];
  char state[MAX_TEXT];
  uint64_t timer_origin_ns;
  uint64_t timer_unit_ns;
  uint64_t first_event_ns;
  uint64_t last_event_ns;
  uint64_t events;
} participant;

static void fail_at(participant *p, uint64_t t, const char *message) {
  char reason[REASON_BYTES];
  snprintf(reason, sizeof reason, "t=%" PRIu64 " ns: %s", t, message);
  p->api->fail(p->api->ctx, reason);
}

/* The failure builds first write $TMPDIR/libsafety-failure-event, so the
 * control can show that the Run reached the event and did not stop earlier;
 * a hung Run is killed before its log is kept. */
static void inject_failure(uint64_t event) {
#ifdef LIBSAFETY_FAILURE_AT_EVENT
  if (event != LIBSAFETY_FAILURE_AT_EVENT) return;
  char path[MAX_TEXT];
  const char *directory = getenv("TMPDIR");
  snprintf(path, sizeof path, "%s/libsafety-failure-event",
           directory ? directory : "/tmp");
  FILE *marker = fopen(path, "w");
  if (marker) {
    fprintf(marker, "%" PRIu64 "\n", event);
    fclose(marker);
  }
#ifdef LIBSAFETY_FAILURE_CRASH
  raise(SIGSEGV);
#endif
#ifdef LIBSAFETY_FAILURE_HANG
  for (;;) pause();
#endif
#else
  (void)event;
#endif
}

/* set_timer and, when warm, safety_tick: the start of one upstream event. */
static void start_event(participant *p, uint64_t event_ns) {
  p->lib.set_timer((uint32_t)((p->timer_origin_ns + event_ns) /
                              p->timer_unit_ns % TIMER_MODULUS));
  if (event_ns > p->first_event_ns + TICK_MARGIN_NS &&
      event_ns + TICK_MARGIN_NS < p->last_event_ns)
    p->lib.safety_tick();
}

/* Takes the next frame on channel into frame. Returns 1 when there is one,
 * 0 when there is none, -1 after a failure. */
static int take_frame(participant *p, uint64_t t, const char *channel,
                      can_TimedFrame *frame) {
  const void *data;
  size_t len;
  int r = p->api->take(p->api->ctx, channel, &data, &len);
  if (r != 1) return r == 0 ? 0 : -1;
  char message[MESSAGE_BYTES];
  if (len != sizeof *frame) {
    snprintf(message, sizeof message, "Channel '%s' carries %zu bytes, "
             "can.TimedFrame %zu", channel, len, sizeof *frame);
    fail_at(p, t, message);
    return -1;
  }
  memcpy(frame, data, sizeof *frame);
  if (frame->length > MAX_CLASSIC_LENGTH) {
    snprintf(message, sizeof message, "a %u-byte payload on '%s' is not "
             "classic CAN", frame->length, channel);
    fail_at(p, t, message);
    return -1;
  }
  return 1;
}

static bool same_instant(participant *p, uint64_t t, const char *channel,
                         uint64_t event_ns, uint64_t frame_ns) {
  if (frame_ns == event_ns) return true;
  char message[MESSAGE_BYTES];
  snprintf(message, sizeof message, "'%s' holds a frame of %" PRIu64
           " ns in the activation of the Burst at %" PRIu64 " ns; the Task "
           "period must be shorter than the shortest interval between "
           "recorded events", channel, frame_ns, event_ns);
  fail_at(p, t, message);
  return false;
}

static void activate(void *user, uint64_t t) {
  participant *p = user;
  libsafety_NativeState out = {0};
  can_TimedFrame frame;
  uint8_t raw[PACKET_BYTES];
  int r;

  if ((r = take_frame(p, t, p->frames, &frame)) != 1) {
    if (r == 0 && (r = take_frame(p, t, p->transmit, &frame)) == 1)
      fail_at(p, t, "a transmit candidate has no can event at its instant");
    return;
  }
  out.event_ns = frame.event_ns;
  inject_failure(p->events++);
  start_event(p, out.event_ns);
  do {
    if (!same_instant(p, t, p->frames, out.event_ns, frame.event_ns)) return;
    p->lib.safety_fwd_hook(frame.src, (int)frame.address);
    packet(&frame, frame.src % 4, raw);
    if (p->lib.safety_rx_hook(raw))
      out.accepted++;
    else
      out.rejected++;
  } while ((r = take_frame(p, t, p->frames, &frame)) == 1);
  if (r < 0) return;

  bool started = false;
  while ((r = take_frame(p, t, p->transmit, &frame)) == 1) {
    if (!same_instant(p, t, p->transmit, out.event_ns, frame.event_ns)) return;
    if (!started) start_event(p, out.event_ns);
    started = true;
    packet(&frame, frame.src % 4, raw);
    if (p->lib.safety_tx_hook(raw))
      out.tx_accepted++;
    else
      out.tx_rejected++;
  }
  if (r < 0) return;

  out.config_valid = p->lib.safety_config_valid();
  out.controls_allowed = p->lib.get_controls_allowed();
  out.gas_pressed_prev = p->lib.get_gas_pressed_prev();
  out.brake_pressed_prev = p->lib.get_brake_pressed_prev();
  out.cruise_engaged_prev = p->lib.get_cruise_engaged_prev();
  out.vehicle_moving = p->lib.get_vehicle_moving();
  out.acc_main_on = p->lib.get_acc_main_on();
  out.vehicle_speed_min = p->lib.get_vehicle_speed_min();
  out.vehicle_speed_max = p->lib.get_vehicle_speed_max();
  p->api->publish(p->api->ctx, p->state, &out, sizeof out);
}

/* Reads the config, binds the library and sets the recorded contract. */
static bool configure(participant *p, config_doc *doc, uint64_t *period_ns) {
  char path[MAX_TEXT];
  uint64_t mode, param, alternative_experience;
  if (!string_value(doc, "library", path) ||
      !string_value(doc, "frames", p->frames) ||
      !string_value(doc, "transmit", p->transmit) ||
      !string_value(doc, "state", p->state) ||
      !unsigned_value(doc, "period_ns", period_ns) ||
      !unsigned_value(doc, "mode", &mode) ||
      !unsigned_value(doc, "param", &param) ||
      !unsigned_value(doc, "alternative_experience", &alternative_experience) ||
      !unsigned_value(doc, "timer_origin_ns", &p->timer_origin_ns) ||
      !unsigned_value(doc, "timer_unit_ns", &p->timer_unit_ns) ||
      !unsigned_value(doc, "first_event_ns", &p->first_event_ns) ||
      !unsigned_value(doc, "last_event_ns", &p->last_event_ns) ||
      !no_unknown_keys(doc))
    return false;
  if (*period_ns == 0 || p->timer_unit_ns == 0 || mode > UINT16_MAX ||
      param > UINT16_MAX || alternative_experience > INT32_MAX) {
    snprintf(doc->error, sizeof doc->error,
             "config needs a nonzero period and timer unit, a 16-bit mode "
             "and param, and a 31-bit alternative experience");
    return false;
  }
  if (!bind(path, &p->lib, doc->error, sizeof doc->error)) return false;
  int status = p->lib.set_safety_hooks((uint16_t)mode, (uint16_t)param);
  if (status != 0) {
    snprintf(doc->error, sizeof doc->error,
             "set_safety_hooks(%" PRIu64 ", %" PRIu64 ") returned %d, not 0",
             mode, param, status);
    return false;
  }
  p->lib.set_alternative_experience((int)alternative_experience);
  return true;
}

int sil_participant_init(const sil_api_v1 *api, const char *name,
                         const char *config_json) {
  static int instances;
  if (api->abi_version != SIL_ABI_VERSION) return SIL_ERR;
  if (instances > 0) {
    char reason[MAX_TEXT + 160];
    snprintf(reason, sizeof reason,
             "participant '%s': libsafety keeps its state in C globals, so "
             "one Run holds at most one instance of it", name);
    api->fail(api->ctx, reason);
    return SIL_ERR;
  }
  config_doc doc;
  uint64_t period_ns;
  participant *p = calloc(1, sizeof *p);
  if (!p) {
    api->fail(api->ctx, "cannot allocate the participant");
    return SIL_ERR;
  }
  p->api = api;
  if (!parse_config(config_json, &doc) || !configure(p, &doc, &period_ns)) {
    api->fail(api->ctx, doc.error);
    free(p);
    return SIL_ERR;
  }
  if (api->subscribe(api->ctx, p->frames) != SIL_OK ||
      api->subscribe(api->ctx, p->transmit) != SIL_OK) {
    free(p);
    return SIL_ERR;
  }
  instances++;
  /* Owned by the Run from here on; see the file comment. */
  return api->register_task(api->ctx, "libsafety", period_ns, 0, 0, activate,
                            p);
}
