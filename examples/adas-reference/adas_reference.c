/* ADAS radar/camera reference controller — reference profile 3.
 * See adas_reference.h for the interface and docs/adas-reference.md for the
 * profile. Each input is converted once from its Float32 field to binary64;
 * all reference arithmetic is binary64; outputs round to Float32 once, when
 * the output is written. The arithmetic needs IEEE 754 binary64 evaluation
 * without contraction or reassociation: do not build it with -ffast-math.
 */
#include "adas_reference.h"

#include <inttypes.h>
#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#if defined(__FAST_MATH__)
#error "the reference arithmetic requires IEEE 754 semantics; drop -ffast-math"
#endif

/* Reference behavior. These constants define test behavior, not vehicle
 * requirements. */
#define MIN_CONFIDENCE 0.5
#define MAX_ABS_Y_M 1.5
#define MAX_ASSOCIATION_DX_M 2.0
#define MAX_ASSOCIATION_DY_M 0.5
#define HAZARD_DISTANCE_M 8.0
#define HAZARD_TIME_TO_COLLISION_S 2.0

/* Parameter ranges. */
#define MIN_HAZARD_ACCELERATION_MPS2 (-10.0)
#define MAX_CHANGE_LIMIT_MPS2 10.0

/* SiL drives the reference with -DADAS_REFERENCE_WRONG_SIGN only to show
 * that the comparison catches a sign error: closing speed from +vx. */
#ifdef ADAS_REFERENCE_WRONG_SIGN
#define CLOSING_SIGN 1.0
#else
#define CLOSING_SIGN (-1.0)
#endif

/* SiL drives the reference with -DADAS_REFERENCE_ARRIVAL_TIME only to show
 * that the comparison catches an age counted from arrival instead of from
 * the Sample time. */
#ifdef ADAS_REFERENCE_ARRIVAL_TIME
#define AGE_ORIGIN(sample_time_ns, t_ns) ((void)(sample_time_ns), (t_ns))
#else
#define AGE_ORIGIN(sample_time_ns, t_ns) ((void)(t_ns), (sample_time_ns))
#endif

typedef struct object {
  double x_m;
  double y_m;
  double relative_vx_mps;
  double confidence;
  int32_t id;
} object;

static void describe(adas_ref_fault *fault, const char *format, ...) {
  if (!fault) return;
  va_list args;
  va_start(args, format);
  vsnprintf(fault->message, sizeof fault->message, format, args);
  va_end(args);
}

adas_ref_status adas_ref_init(adas_ref_instance *instance,
                              const adas_ref_config *config,
                              adas_ref_fault *fault) {
  memset(instance, 0, sizeof *instance);
  if (config->period_ns != ADAS_REF_PERIOD_NS) {
    describe(fault,
             "period_ns %" PRIu64 " is not supported; profile %u runs only "
             "at %llu ns",
             config->period_ns, ADAS_REF_PROFILE_VERSION, ADAS_REF_PERIOD_NS);
    return ADAS_REF_ERR_PERIOD;
  }
  double hazard = config->hazard_acceleration_mps2;
  if (!(isfinite(hazard) && hazard >= MIN_HAZARD_ACCELERATION_MPS2 &&
        hazard < 0.0)) {
    describe(fault, "hazard_acceleration_mps2 %g is outside [-10, 0)", hazard);
    return ADAS_REF_ERR_CONFIG;
  }
  double change = config->max_change_mps2;
  if (!(isfinite(change) && change > 0.0 && change <= MAX_CHANGE_LIMIT_MPS2)) {
    describe(fault, "max_change_mps2 %g is outside (0, 10]", change);
    return ADAS_REF_ERR_CONFIG;
  }
  instance->config = *config;
  instance->ready = 1;
  return ADAS_REF_OK;
}

void adas_ref_reset(adas_ref_instance *instance) {
  instance->acceleration_mps2 = 0.0;
  instance->activations = 0;
  instance->ignored_observations = 0;
  memset(&instance->radar_held, 0, sizeof instance->radar_held);
  memset(&instance->camera_held, 0, sizeof instance->camera_held);
  memset(&instance->ego_held, 0, sizeof instance->ego_held);
}

void adas_ref_terminate(adas_ref_instance *instance) { instance->ready = 0; }

static adas_ref_status finite(const char *field, float value,
                              adas_ref_fault *fault) {
  if (isfinite(value)) return ADAS_REF_OK;
  describe(fault, "%s is not finite: %g", field, (double)value);
  return ADAS_REF_ERR_INPUT;
}

static adas_ref_status not_future(const char *field, uint64_t sample_time_ns,
                                  uint64_t t_ns, adas_ref_fault *fault) {
  if (sample_time_ns <= t_ns) return ADAS_REF_OK;
  describe(fault,
           "%s %" PRIu64 " is after the activation time %" PRIu64
           "; a future Sample time is malformed",
           field, sample_time_ns, t_ns);
  return ADAS_REF_ERR_SAMPLE_TIME;
}

static adas_ref_status equals(const char *field, uint32_t value,
                              uint32_t required, const char *what,
                              const char *note, adas_ref_fault *fault) {
  if (value == required) return ADAS_REF_OK;
  describe(fault, "%s %" PRIu32 " is not %s %" PRIu32 "%s", field, value,
           what, required, note);
  return ADAS_REF_ERR_INPUT;
}

static adas_ref_status validity(const char *field, uint8_t value,
                                adas_ref_fault *fault) {
  if (value <= 1) return ADAS_REF_OK;
  describe(fault, "%s %u is neither 0 (invalid) nor 1 (valid)", field,
           (unsigned)value);
  return ADAS_REF_ERR_INPUT;
}

#define CHECKED(call)                      \
  do {                                     \
    adas_ref_status status_ = (call);      \
    if (status_ != ADAS_REF_OK) return status_; \
  } while (0)

/* Names a list field, "radar.count", or an element, "radar.x_m[3]". */
typedef struct field_name {
  char text[48];
} field_name;

static field_name named(const char *sensor, const char *field) {
  field_name n;
  snprintf(n.text, sizeof n.text, "%s.%s", sensor, field);
  return n;
}

static field_name element(const char *sensor, const char *field, uint32_t i) {
  field_name n;
  snprintf(n.text, sizeof n.text, "%s.%s[%" PRIu32 "]", sensor, field, i);
  return n;
}

/* What the profile requires of one sensor's lists. */
typedef struct sensor_rules {
  const char *name;
  uint32_t sensor_id;
  int reports_speed; /* 0: relative_vx_mps must be 0 */
  uint64_t max_age_ns;
} sensor_rules;

static const sensor_rules RADAR = {"radar", ADAS_REF_RADAR_SENSOR_ID, 1,
                                   ADAS_REF_RADAR_MAX_AGE_NS};
static const sensor_rules CAMERA = {"camera", ADAS_REF_CAMERA_SENSOR_ID, 0,
                                    ADAS_REF_CAMERA_MAX_AGE_NS};

static adas_ref_status check_object(const sensor_rules *rules,
                                    const adas_ref_object_list *list,
                                    uint32_t i, adas_ref_fault *fault) {
  const char *sensor = rules->name;
  const adas_ref_object *o = &list->objects[i];
  if (o->id < 0) {
    describe(fault, "%s %" PRId32 " is negative; active IDs are >= 0 and "
             "-1 is reserved for no selection",
             element(sensor, "object_id", i).text, o->id);
    return ADAS_REF_ERR_INPUT;
  }
  for (uint32_t j = 0; j < i; j++)
    if (list->objects[j].id == o->id) {
      describe(fault, "%s %" PRId32 " repeats %s",
               element(sensor, "object_id", i).text, o->id,
               element(sensor, "object_id", j).text);
      return ADAS_REF_ERR_INPUT;
    }
  CHECKED(finite(element(sensor, "x_m", i).text, o->x_m, fault));
  CHECKED(finite(element(sensor, "y_m", i).text, o->y_m, fault));
  CHECKED(finite(element(sensor, "relative_vx_mps", i).text,
                 o->relative_vx_mps, fault));
  CHECKED(finite(element(sensor, "confidence", i).text, o->confidence, fault));
  if (!(o->confidence >= 0.0f && o->confidence <= 1.0f)) {
    describe(fault, "%s %g is outside [0, 1]",
             element(sensor, "confidence", i).text, (double)o->confidence);
    return ADAS_REF_ERR_INPUT;
  }
  if (!rules->reports_speed && o->relative_vx_mps != 0.0f) {
    describe(fault, "%s %g is not 0; this sensor reports no speed",
             element(sensor, "relative_vx_mps", i).text,
             (double)o->relative_vx_mps);
    return ADAS_REF_ERR_INPUT;
  }
  return ADAS_REF_OK;
}

/* Every header field, then the count before any element is read. */
static adas_ref_status check_list(const sensor_rules *rules,
                                  const adas_ref_object_list *list, uint64_t t,
                                  adas_ref_fault *fault) {
  const char *sensor = rules->name;
  CHECKED(not_future(named(sensor, "sample_time_ns").text,
                     list->sample_time_ns, t, fault));
  char what[32];
  snprintf(what, sizeof what, "the %s sensor", sensor);
  CHECKED(equals(named(sensor, "sensor_id").text, list->sensor_id,
                 rules->sensor_id, what, "", fault));
  CHECKED(equals(named(sensor, "frame_id").text, list->frame_id,
                 ADAS_REF_EGO_FRAME_ID, "the ego frame",
                 "; the profile transforms no frame", fault));
  CHECKED(validity(named(sensor, "validity").text, list->validity, fault));
  if (list->count > ADAS_REF_MAX_OBJECTS) {
    describe(fault, "%s %" PRIu32 " exceeds the capacity %u",
             named(sensor, "count").text, list->count, ADAS_REF_MAX_OBJECTS);
    return ADAS_REF_ERR_INPUT;
  }
  for (uint32_t i = 0; i < list->count; i++)
    CHECKED(check_object(rules, list, i, fault));
  return ADAS_REF_OK;
}

static adas_ref_status check_ego(const adas_ref_ego *e, uint64_t t,
                                 adas_ref_fault *fault) {
  CHECKED(not_future("ego.sample_time_ns", e->sample_time_ns, t, fault));
  CHECKED(validity("ego.validity", e->validity, fault));
  CHECKED(finite("ego.speed_mps", e->speed_mps, fault));
  if (e->speed_mps < 0.0f) {
    describe(fault, "ego.speed_mps %g is negative", (double)e->speed_mps);
    return ADAS_REF_ERR_INPUT;
  }
  return ADAS_REF_OK;
}

static adas_ref_status check_ready(const adas_ref_instance *instance,
                                   uint64_t t_ns, adas_ref_fault *fault) {
  if (instance->ready) return ADAS_REF_OK;
  describe(fault, "activation at %" PRIu64 " ns on an instance that is not "
           "initialized or is terminated", t_ns);
  return ADAS_REF_ERR_STATE;
}

/* Holds an observation whose sequence exceeds the held one; ignores and
 * counts any other. Returns 1 when it is held. */
static int accept(adas_ref_instance *instance, adas_ref_held *held,
                  uint32_t sequence, uint64_t sample_time_ns, uint8_t validity,
                  uint64_t t_ns) {
  if (held->present && sequence <= held->sequence) {
    instance->ignored_observations++;
    return 0;
  }
  held->present = 1;
  held->sequence = sequence;
  held->origin_ns = AGE_ORIGIN(sample_time_ns, t_ns);
  held->validity = validity;
  return 1;
}

static adas_ref_status receive_list(adas_ref_instance *instance,
                                    const sensor_rules *rules,
                                    adas_ref_held *held,
                                    adas_ref_object_list *slot, uint64_t t_ns,
                                    const adas_ref_object_list *list,
                                    adas_ref_fault *fault) {
  CHECKED(check_ready(instance, t_ns, fault));
  CHECKED(check_list(rules, list, t_ns, fault));
  if (accept(instance, held, list->sequence, list->sample_time_ns,
             list->validity, t_ns))
    *slot = *list;
  return ADAS_REF_OK;
}

adas_ref_status adas_ref_receive_radar(adas_ref_instance *instance,
                                       uint64_t t_ns,
                                       const adas_ref_object_list *list,
                                       adas_ref_fault *fault) {
  return receive_list(instance, &RADAR, &instance->radar_held,
                      &instance->radar, t_ns, list, fault);
}

adas_ref_status adas_ref_receive_camera(adas_ref_instance *instance,
                                        uint64_t t_ns,
                                        const adas_ref_object_list *list,
                                        adas_ref_fault *fault) {
  return receive_list(instance, &CAMERA, &instance->camera_held,
                      &instance->camera, t_ns, list, fault);
}

adas_ref_status adas_ref_receive_ego(adas_ref_instance *instance,
                                     uint64_t t_ns, const adas_ref_ego *ego,
                                     adas_ref_fault *fault) {
  CHECKED(check_ready(instance, t_ns, fault));
  CHECKED(check_ego(ego, t_ns, fault));
  accept(instance, &instance->ego_held, ego->sequence, ego->sample_time_ns,
         ego->validity, t_ns);
  return ADAS_REF_OK;
}

/* The age of the held observation at t, or ADAS_REF_NO_AGE. */
static int64_t age(const adas_ref_held *held, uint64_t t_ns) {
  return held->present ? (int64_t)(t_ns - held->origin_ns) : ADAS_REF_NO_AGE;
}

static int fresh(const adas_ref_held *held, uint64_t t_ns,
                 uint64_t max_age_ns) {
  return held->present && held->validity &&
         t_ns - held->origin_ns <= max_age_ns;
}

static int available(const adas_ref_instance *instance, uint64_t t_ns) {
  return fresh(&instance->radar_held, t_ns, RADAR.max_age_ns) &&
         fresh(&instance->camera_held, t_ns, CAMERA.max_age_ns) &&
         fresh(&instance->ego_held, t_ns, ADAS_REF_EGO_MAX_AGE_NS);
}

/* Fails when t precedes the origin of a held observation. */
static adas_ref_status not_before(const char *sensor, const adas_ref_held *held,
                                  uint64_t t_ns, adas_ref_fault *fault) {
  if (!held->present || held->origin_ns <= t_ns) return ADAS_REF_OK;
  describe(fault,
           "activation time %" PRIu64 " ns precedes the held %s observation "
           "from %" PRIu64 " ns",
           t_ns, sensor, held->origin_ns);
  return ADAS_REF_ERR_TIME;
}

static object from(const adas_ref_object *o) {
  object converted = {o->x_m, o->y_m, o->relative_vx_mps, o->confidence,
                      o->id};
  return converted;
}

static int eligible(const object *o) {
  return o->x_m > 0.0 && fabs(o->y_m) <= MAX_ABS_Y_M &&
         o->confidence >= MIN_CONFIDENCE;
}

/* Association is geometric: radar and camera IDs are never compared. */
static int confirms(const object *radar, const object *camera) {
  return eligible(radar) && eligible(camera) &&
         fabs(camera->x_m - radar->x_m) <= MAX_ASSOCIATION_DX_M &&
         fabs(camera->y_m - radar->y_m) <= MAX_ASSOCIATION_DY_M;
}

static int confirmed(const object *radar, const adas_ref_object_list *camera) {
  for (uint32_t j = 0; j < camera->count; j++) {
    object c = from(&camera->objects[j]);
    if (confirms(radar, &c)) return 1;
  }
  return 0;
}

/* Nearer: smaller x; at equal x, the smaller radar ID. A total order on
 * distinct IDs, so the selection does not depend on list order. */
static int nearer(const object *a, const object *b) {
  return a->x_m < b->x_m || (a->x_m == b->x_m && a->id < b->id);
}

/* The nearest confirmed radar object; returns 0 when there is none. */
static int select_lead(const adas_ref_object_list *radar,
                       const adas_ref_object_list *camera, object *lead) {
  int found = 0;
  for (uint32_t i = 0; i < radar->count; i++) {
    object r = from(&radar->objects[i]);
    if (!confirmed(&r, camera) || (found && !nearer(&r, lead))) continue;
    *lead = r;
    found = 1;
  }
  return found;
}

static int hazard(const object *radar) {
  double closing_mps = fmax(0.0, CLOSING_SIGN * radar->relative_vx_mps);
  return radar->x_m < HAZARD_DISTANCE_M ||
         (closing_mps > 0.0 &&
          radar->x_m / closing_mps < HAZARD_TIME_TO_COLLISION_S);
}

static double limited(double from, double to, double max_change) {
  return from + fmin(max_change, fmax(-max_change, to - from));
}

adas_ref_status adas_ref_advance(adas_ref_instance *instance, uint64_t t_ns,
                                 adas_ref_output *output,
                                 adas_ref_fault *fault) {
  CHECKED(check_ready(instance, t_ns, fault));
  if (t_ns > UINT64_MAX - instance->config.period_ns) {
    describe(fault, "activation time %" PRIu64 " ns + Period does not fit "
             "uint64", t_ns);
    return ADAS_REF_ERR_TIME;
  }
  CHECKED(not_before("radar", &instance->radar_held, t_ns, fault));
  CHECKED(not_before("camera", &instance->camera_held, t_ns, fault));
  CHECKED(not_before("ego", &instance->ego_held, t_ns, fault));

  uint32_t mode;
  int32_t selected = ADAS_REF_NO_OBJECT;
  object lead;
  if (!available(instance, t_ns)) {
    mode = ADAS_REF_MODE_SENSOR_UNAVAILABLE;
  } else {
    mode = ADAS_REF_MODE_CLEAR;
    if (select_lead(&instance->radar, &instance->camera, &lead)) {
      selected = lead.id;
      if (hazard(&lead)) mode = ADAS_REF_MODE_HAZARD;
    }
  }
  double target = mode == ADAS_REF_MODE_CLEAR
                      ? 0.0
                      : instance->config.hazard_acceleration_mps2;
  instance->acceleration_mps2 = limited(
      instance->acceleration_mps2, target, instance->config.max_change_mps2);
  instance->activations++;

  output->sample_time_ns = t_ns + instance->config.period_ns;
  output->sequence = instance->activations;
  output->mode = mode;
  output->selected_object_id = selected;
  output->target_acceleration_mps2 = (float)target;
  output->acceleration_mps2 = (float)instance->acceleration_mps2;
  output->radar_age_ns = age(&instance->radar_held, t_ns);
  output->camera_age_ns = age(&instance->camera_held, t_ns);
  output->ego_age_ns = age(&instance->ego_held, t_ns);
  output->ignored_observations = instance->ignored_observations;
  return ADAS_REF_OK;
}
