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

#include <errno.h>
#include <inttypes.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "libsafety_api.h"

#define MAX_TEXT 256
#define MAX_ENTRIES 16
/* A diagnostic holds a channel name and its numbers; a reason adds the
 * activation time. */
#define MESSAGE_BYTES 1024
#define REASON_BYTES (MESSAGE_BYTES + 64)

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

/* --- the Participant ---------------------------------------------------- */

typedef struct participant {
  const sil_api_v1 *api;
  library lib;
  char frames[MAX_TEXT];
  char transmit[MAX_TEXT];
  char state[MAX_TEXT];
  event_policy policy;
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
  can_packet raw;
  int r;

  if ((r = take_frame(p, t, p->frames, &frame)) != 1) {
    if (r == 0 && (r = take_frame(p, t, p->transmit, &frame)) == 1)
      fail_at(p, t, "a transmit candidate has no can event at its instant");
    return;
  }
  out.event_ns = frame.event_ns;
  inject_failure(p->events++);
  start_event(&p->lib, &p->policy, out.event_ns);
  do {
    if (!same_instant(p, t, p->frames, out.event_ns, frame.event_ns)) return;
    p->lib.safety_fwd_hook(frame.src, (int)frame.address);
    packet(&frame, frame.src % 4, &raw);
    if (p->lib.safety_rx_hook(raw.bytes))
      out.accepted++;
    else
      out.rejected++;
  } while ((r = take_frame(p, t, p->frames, &frame)) == 1);
  if (r < 0) return;

  bool started = false;
  while ((r = take_frame(p, t, p->transmit, &frame)) == 1) {
    if (!same_instant(p, t, p->transmit, out.event_ns, frame.event_ns)) return;
    if (!started) start_event(&p->lib, &p->policy, out.event_ns);
    started = true;
    packet(&frame, frame.src % 4, &raw);
    if (p->lib.safety_tx_hook(raw.bytes))
      out.tx_accepted++;
    else
      out.tx_rejected++;
  }
  if (r < 0) return;

  read_state(&p->lib, &out);
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
      !unsigned_value(doc, "timer_origin_ns", &p->policy.timer_origin_ns) ||
      !unsigned_value(doc, "timer_unit_ns", &p->policy.timer_unit_ns) ||
      !unsigned_value(doc, "first_event_ns", &p->policy.first_event_ns) ||
      !unsigned_value(doc, "last_event_ns", &p->policy.last_event_ns) ||
      !no_unknown_keys(doc))
    return false;
  if (*period_ns == 0 || p->policy.timer_unit_ns == 0 || mode > UINT16_MAX ||
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
