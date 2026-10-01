/* The SiL Native participant adapter of the ADAS reference application.
 *
 * Only this file knows SiL: it exports sil_participant_init, registers one
 * 10 ms Task per Participant, and converts between the generated Schema
 * layouts (adas_messages.h, from schemas.json by silschema) and the
 * application's own types (adas_reference.h).
 *
 * Each Manifest entry gets its own heap-allocated controller behind the Task's
 * user pointer, so one loaded library backs any number of Participants in one
 * Run. Native ABI v1 has no termination callback: a controller lives for the
 * whole Run and the runner process exit reclaims it, so adas_ref_terminate is
 * never called here. The application holds no other resource.
 *
 * Every activation consumes, from each input Channel, the one Message
 * visible at t, and publishes one adas.Command for Sample time t + 10 ms.
 *
 * An adas.ObjectList carries its objects as flat arrays of capacity 8 and an
 * active count. The adapter checks the count before it reads any element,
 * requires every inactive element to be zero, and assembles the active
 * elements into the application's object structures. It never truncates.
 */
#include <sil/participant.h>

#include <errno.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "adas_messages.h"
#include "adas_reference.h"

#define MAX_TEXT 128
#define MAX_ENTRIES 16

#define CAPACITY(array) (sizeof(array) / sizeof *(array))

_Static_assert(CAPACITY(((adas_ObjectList *)0)->x_m) == ADAS_REF_MAX_OBJECTS,
               "the Schema arrays must have the application's capacity");

/* --- configuration ------------------------------------------------------ */

/* The Manifest's config object is flat: string and number values only. */
typedef struct entry {
  char key[MAX_TEXT];
  char text[MAX_TEXT];
  int is_string;
  int used;
} entry;

typedef struct config_doc {
  entry entries[MAX_ENTRIES];
  size_t count;
  char error[200];
} config_doc;

static const char *skip_space(const char *p) {
  while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r') p++;
  return p;
}

/* Reads a JSON string without escapes into out. */
static const char *read_string(const char *p, char *out, config_doc *doc) {
  if (*p != '"') {
    snprintf(doc->error, sizeof doc->error, "expected a string");
    return NULL;
  }
  size_t n = 0;
  for (p++; *p != '"'; p++) {
    if (*p == '\0' || *p == '\\' || n + 1 >= MAX_TEXT) {
      snprintf(doc->error, sizeof doc->error,
               "a string is unterminated, escaped or longer than %d bytes",
               MAX_TEXT - 1);
      return NULL;
    }
    out[n++] = *p;
  }
  out[n] = '\0';
  return p + 1;
}

static const char *read_number(const char *p, char *out, config_doc *doc) {
  size_t n = 0;
  while (*p != '\0' && strchr("+-0123456789.eE", *p) && n + 1 < MAX_TEXT)
    out[n++] = *p++;
  out[n] = '\0';
  if (n == 0) {
    snprintf(doc->error, sizeof doc->error,
             "a value is neither a string nor a number");
    return NULL;
  }
  return p;
}

static int parse_config(const char *json, config_doc *doc) {
  memset(doc, 0, sizeof *doc);
  const char *p = skip_space(json);
  if (*p++ != '{') {
    snprintf(doc->error, sizeof doc->error, "config is not a JSON object");
    return 0;
  }
  p = skip_space(p);
  if (*p == '}') return *skip_space(p + 1) == '\0';
  for (;;) {
    if (doc->count == MAX_ENTRIES) {
      snprintf(doc->error, sizeof doc->error, "config has too many keys");
      return 0;
    }
    entry *e = &doc->entries[doc->count];
    if (!(p = read_string(p, e->key, doc))) return 0;
    for (size_t i = 0; i < doc->count; i++)
      if (strcmp(doc->entries[i].key, e->key) == 0) {
        snprintf(doc->error, sizeof doc->error, "config repeats key '%s'",
                 e->key);
        return 0;
      }
    p = skip_space(p);
    if (*p++ != ':') {
      snprintf(doc->error, sizeof doc->error, "expected ':' after '%s'",
               e->key);
      return 0;
    }
    p = skip_space(p);
    e->is_string = *p == '"';
    p = e->is_string ? read_string(p, e->text, doc)
                     : read_number(p, e->text, doc);
    if (!p) return 0;
    doc->count++;
    p = skip_space(p);
    if (*p == ',') {
      p = skip_space(p + 1);
      continue;
    }
    if (*p == '}' && *skip_space(p + 1) == '\0') return 1;
    snprintf(doc->error, sizeof doc->error, "config is not a flat object");
    return 0;
  }
}

static entry *find(config_doc *doc, const char *key, int is_string) {
  for (size_t i = 0; i < doc->count; i++) {
    entry *e = &doc->entries[i];
    if (strcmp(e->key, key) != 0) continue;
    if (e->is_string != is_string) {
      snprintf(doc->error, sizeof doc->error, "config key '%s' must be a %s",
               key, is_string ? "string" : "number");
      return NULL;
    }
    e->used = 1;
    return e;
  }
  snprintf(doc->error, sizeof doc->error, "config is missing key '%s'", key);
  return NULL;
}

static int string_value(config_doc *doc, const char *key, char *out) {
  entry *e = find(doc, key, 1);
  if (!e) return 0;
  memcpy(out, e->text, MAX_TEXT);
  return 1;
}

static int unsigned_value(config_doc *doc, const char *key, uint64_t *out) {
  entry *e = find(doc, key, 0);
  if (!e) return 0;
  char *end;
  errno = 0;
  unsigned long long v = strtoull(e->text, &end, 10);
  if (e->text[0] == '-' || *end != '\0' || errno == ERANGE) {
    snprintf(doc->error, sizeof doc->error,
             "config key '%s' is not an unsigned 64-bit integer: %s", key,
             e->text);
    return 0;
  }
  *out = v;
  return 1;
}

static int real_value(config_doc *doc, const char *key, double *out) {
  entry *e = find(doc, key, 0);
  if (!e) return 0;
  char *end;
  errno = 0;
  double v = strtod(e->text, &end);
  if (*end != '\0' || errno == ERANGE) {
    snprintf(doc->error, sizeof doc->error,
             "config key '%s' is not a finite number: %s", key, e->text);
    return 0;
  }
  *out = v;
  return 1;
}

static int no_unknown_keys(config_doc *doc) {
  for (size_t i = 0; i < doc->count; i++)
    if (!doc->entries[i].used) {
      snprintf(doc->error, sizeof doc->error, "config has unknown key '%s'",
               doc->entries[i].key);
      return 0;
    }
  return 1;
}

/* --- the Participant ---------------------------------------------------- */

typedef struct controller {
  const sil_api_v1 *api;
  char radar[MAX_TEXT];
  char camera[MAX_TEXT];
  char ego[MAX_TEXT];
  char command[MAX_TEXT];
  adas_ref_instance app;
} controller;

static void fail_at(controller *c, uint64_t t, const char *message) {
  char reason[300];
  snprintf(reason, sizeof reason, "t=%" PRIu64 " ns: %s", t, message);
  c->api->fail(c->api->ctx, reason);
}

/* Takes the one Message visible on channel at t into payload (size bytes).
 * Returns 1 when there is one, 0 when there is none, -1 after a failure. */
static int take_one(controller *c, uint64_t t, const char *channel,
                    void *payload, size_t size) {
  const void *data;
  size_t len;
  int r = c->api->take(c->api->ctx, channel, &data, &len);
  if (r != 1) return r == 0 ? 0 : -1;
  char message[200];
  if (len != size) {
    snprintf(message, sizeof message,
             "Channel '%s' carries %zu bytes, its Schema %zu", channel, len,
             size);
    fail_at(c, t, message);
    return -1;
  }
  memcpy(payload, data, size);
  r = c->api->take(c->api->ctx, channel, &data, &len);
  if (r == 0) return 1;
  if (r == 1) {
    snprintf(message, sizeof message,
             "Channel '%s' has more than one Message at this activation",
             channel);
    fail_at(c, t, message);
  }
  return -1;
}

/* Fails unless elements [count, capacity) of array are all-bits zero. */
static int inactive_zero(controller *c, uint64_t t, const char *sensor,
                         const char *field, const void *array, size_t size,
                         uint32_t count) {
  static const unsigned char zero[sizeof(double)];
  const unsigned char *bytes = array;
  for (uint32_t i = count; i < ADAS_REF_MAX_OBJECTS; i++) {
    if (memcmp(bytes + i * size, zero, size) == 0) continue;
    char message[200];
    snprintf(message, sizeof message,
             "%s.%s[%" PRIu32 "] is inactive (count %" PRIu32
             ") but not zero; inactive elements must be zero",
             sensor, field, i, count);
    fail_at(c, t, message);
    return 0;
  }
  return 1;
}

#define INACTIVE_ZERO(field)                                             \
  inactive_zero(c, t, sensor, #field, m->field, sizeof *m->field, m->count)

/* Assembles the application's list from the Message, or fails. */
static int to_list(controller *c, uint64_t t, const char *sensor,
                   const adas_ObjectList *m, adas_ref_object_list *list) {
  if (m->count > ADAS_REF_MAX_OBJECTS) {
    char message[200];
    snprintf(message, sizeof message,
             "%s.count %" PRIu32 " exceeds the capacity %u", sensor, m->count,
             ADAS_REF_MAX_OBJECTS);
    fail_at(c, t, message);
    return 0;
  }
  if (!INACTIVE_ZERO(object_id) || !INACTIVE_ZERO(x_m) ||
      !INACTIVE_ZERO(y_m) || !INACTIVE_ZERO(relative_vx_mps) ||
      !INACTIVE_ZERO(confidence))
    return 0;
  memset(list, 0, sizeof *list);
  list->sample_time_ns = m->sample_time_ns;
  list->sensor_id = m->sensor_id;
  list->frame_id = m->frame_id;
  list->sequence = m->sequence;
  list->count = m->count;
  list->validity = m->validity;
  for (uint32_t i = 0; i < m->count; i++)
    list->objects[i] = (adas_ref_object){m->object_id[i], m->x_m[i],
                                         m->y_m[i], m->relative_vx_mps[i],
                                         m->confidence[i]};
  return 1;
}

/* Takes the sensor's list visible at t into *list, and points *in at it.
 * Returns 1 on success (also when there is none), 0 after a failure. */
static int take_list(controller *c, uint64_t t, const char *sensor,
                     const char *channel, adas_ref_object_list *list,
                     const adas_ref_object_list **in) {
  adas_ObjectList m;
  int r = take_one(c, t, channel, &m, sizeof m);
  if (r < 0) return 0;
  if (r == 0) return 1;
  if (!to_list(c, t, sensor, &m, list)) return 0;
  *in = list;
  return 1;
}

static void activate(void *user, uint64_t t) {
  controller *c = user;
  adas_ref_object_list radar, camera;
  adas_EgoMotion ego_msg;
  adas_ref_ego ego;
  adas_ref_inputs in = {NULL, NULL, NULL};

  if (!take_list(c, t, "radar", c->radar, &radar, &in.radar) ||
      !take_list(c, t, "camera", c->camera, &camera, &in.camera))
    return;
  int r = take_one(c, t, c->ego, &ego_msg, sizeof ego_msg);
  if (r < 0) return;
  if (r == 1) {
    ego = (adas_ref_ego){ego_msg.sample_time_ns, ego_msg.sequence,
                         ego_msg.validity, ego_msg.speed_mps};
    in.ego = &ego;
  }

  adas_ref_output out;
  adas_ref_fault fault;
  if (adas_ref_advance(&c->app, t, &in, &out, &fault) != ADAS_REF_OK) {
    fail_at(c, t, fault.message);
    return;
  }
  adas_Command command = {out.sample_time_ns, out.sequence, out.mode,
                          out.selected_object_id,
                          out.target_acceleration_mps2, out.acceleration_mps2};
  c->api->publish(c->api->ctx, c->command, &command, sizeof command);
}

static int configure(controller *c, config_doc *doc, adas_ref_config *config,
                     adas_ref_fault *fault) {
  char profile[MAX_TEXT];
  uint64_t version;
  if (!string_value(doc, "profile", profile) ||
      !unsigned_value(doc, "profile_version", &version) ||
      !string_value(doc, "radar", c->radar) ||
      !string_value(doc, "camera", c->camera) ||
      !string_value(doc, "ego", c->ego) ||
      !string_value(doc, "command", c->command) ||
      !unsigned_value(doc, "period_ns", &config->period_ns) ||
      !real_value(doc, "hazard_acceleration_mps2",
                  &config->hazard_acceleration_mps2) ||
      !real_value(doc, "max_change_mps2", &config->max_change_mps2) ||
      !no_unknown_keys(doc))
    return 0;
  if (strcmp(profile, ADAS_REF_PROFILE) != 0 ||
      version != ADAS_REF_PROFILE_VERSION) {
    snprintf(doc->error, sizeof doc->error,
             "config names profile '%s' version %" PRIu64
             "; this library implements '%s' version %u",
             profile, version, ADAS_REF_PROFILE, ADAS_REF_PROFILE_VERSION);
    return 0;
  }
  if (adas_ref_init(&c->app, config, fault) != ADAS_REF_OK) {
    snprintf(doc->error, sizeof doc->error, "%s", fault->message);
    return 0;
  }
  return 1;
}

int sil_participant_init(const sil_api_v1 *api, const char *name,
                         const char *config_json) {
  (void)name;
  if (api->abi_version != SIL_ABI_VERSION) return SIL_ERR;
  config_doc doc;
  adas_ref_config config;
  adas_ref_fault fault;
  controller *c = calloc(1, sizeof *c);
  if (!c) {
    api->fail(api->ctx, "cannot allocate the controller");
    return SIL_ERR;
  }
  c->api = api;
  if (!parse_config(config_json, &doc) ||
      !configure(c, &doc, &config, &fault)) {
    api->fail(api->ctx, doc.error);
    free(c);
    return SIL_ERR;
  }
  if (api->subscribe(api->ctx, c->radar) != SIL_OK ||
      api->subscribe(api->ctx, c->camera) != SIL_OK ||
      api->subscribe(api->ctx, c->ego) != SIL_OK) {
    free(c);
    return SIL_ERR;
  }
  /* Owned by the Run from here on; see the file comment. */
  return api->register_task(api->ctx, "control", config.period_ns, 0, 0,
                            activate, c);
}
