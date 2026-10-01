/* ADAS radar/camera reference controller — reference profile 1.
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
}

void adas_ref_terminate(adas_ref_instance *instance) { instance->ready = 0; }

static adas_ref_status finite(const char *field, float value,
                              adas_ref_fault *fault) {
  if (isfinite(value)) return ADAS_REF_OK;
  describe(fault, "%s is not finite: %g", field, (double)value);
  return ADAS_REF_ERR_INPUT;
}

static adas_ref_status sampled_at(const char *field, uint64_t sample_time_ns,
                                  uint64_t t_ns, adas_ref_fault *fault) {
  if (sample_time_ns == t_ns) return ADAS_REF_OK;
  describe(fault,
           "%s %" PRIu64 " is not the activation time %" PRIu64
           "; inputs sampled at t are consumed at t",
           field, sample_time_ns, t_ns);
  return ADAS_REF_ERR_SAMPLE_TIME;
}

static adas_ref_status object_id(const char *field, int32_t id,
                                 adas_ref_fault *fault) {
  if (id >= ADAS_REF_NO_OBJECT) return ADAS_REF_OK;
  describe(fault, "%s %" PRId32 " is neither -1 (no object) nor >= 0", field,
           id);
  return ADAS_REF_ERR_INPUT;
}

#define CHECKED(call)                      \
  do {                                     \
    adas_ref_status status_ = (call);      \
    if (status_ != ADAS_REF_OK) return status_; \
  } while (0)

static adas_ref_status check_radar(const adas_ref_radar *r, uint64_t t,
                                   adas_ref_fault *fault) {
  CHECKED(sampled_at("radar.sample_time_ns", r->sample_time_ns, t, fault));
  CHECKED(object_id("radar.object_id", r->object_id, fault));
  CHECKED(finite("radar.x_m", r->x_m, fault));
  CHECKED(finite("radar.y_m", r->y_m, fault));
  CHECKED(finite("radar.relative_vx_mps", r->relative_vx_mps, fault));
  CHECKED(finite("radar.confidence", r->confidence, fault));
  return ADAS_REF_OK;
}

static adas_ref_status check_camera(const adas_ref_camera *c, uint64_t t,
                                    adas_ref_fault *fault) {
  CHECKED(sampled_at("camera.sample_time_ns", c->sample_time_ns, t, fault));
  CHECKED(object_id("camera.object_id", c->object_id, fault));
  CHECKED(finite("camera.x_m", c->x_m, fault));
  CHECKED(finite("camera.y_m", c->y_m, fault));
  CHECKED(finite("camera.confidence", c->confidence, fault));
  return ADAS_REF_OK;
}

static adas_ref_status check_ego(const adas_ref_ego *e, uint64_t t,
                                 adas_ref_fault *fault) {
  CHECKED(sampled_at("ego.sample_time_ns", e->sample_time_ns, t, fault));
  CHECKED(finite("ego.speed_mps", e->speed_mps, fault));
  if (e->speed_mps < 0.0f) {
    describe(fault, "ego.speed_mps %g is negative", (double)e->speed_mps);
    return ADAS_REF_ERR_INPUT;
  }
  return ADAS_REF_OK;
}

static adas_ref_status check_inputs(const adas_ref_inputs *in, uint64_t t,
                                    adas_ref_fault *fault) {
  if (in->radar) CHECKED(check_radar(in->radar, t, fault));
  if (in->camera) CHECKED(check_camera(in->camera, t, fault));
  if (in->ego) CHECKED(check_ego(in->ego, t, fault));
  return ADAS_REF_OK;
}

static int eligible(const object *o) {
  return o->id != ADAS_REF_NO_OBJECT && o->x_m > 0.0 &&
         fabs(o->y_m) <= MAX_ABS_Y_M && o->confidence >= MIN_CONFIDENCE;
}

static int confirms(const object *radar, const object *camera) {
  return eligible(radar) && eligible(camera) &&
         fabs(camera->x_m - radar->x_m) <= MAX_ASSOCIATION_DX_M &&
         fabs(camera->y_m - radar->y_m) <= MAX_ASSOCIATION_DY_M;
}

static int hazard(const object *radar) {
  double closing_mps = fmax(0.0, CLOSING_SIGN * radar->relative_vx_mps);
  return radar->x_m < HAZARD_DISTANCE_M ||
         (closing_mps > 0.0 &&
          radar->x_m / closing_mps < HAZARD_TIME_TO_COLLISION_S);
}

static object from_radar(const adas_ref_radar *r) {
  object o = {r->x_m, r->y_m, r->relative_vx_mps, r->confidence, r->object_id};
  return o;
}

static object from_camera(const adas_ref_camera *c) {
  object o = {c->x_m, c->y_m, 0.0, c->confidence, c->object_id};
  return o;
}

static double limited(double from, double to, double max_change) {
  return from + fmin(max_change, fmax(-max_change, to - from));
}

adas_ref_status adas_ref_advance(adas_ref_instance *instance, uint64_t t_ns,
                                 const adas_ref_inputs *inputs,
                                 adas_ref_output *output,
                                 adas_ref_fault *fault) {
  if (!instance->ready) {
    describe(fault, "advance at %" PRIu64 " ns on an instance that is not "
             "initialized or is terminated", t_ns);
    return ADAS_REF_ERR_STATE;
  }
  if (t_ns > UINT64_MAX - instance->config.period_ns) {
    describe(fault, "activation time %" PRIu64 " ns + Period does not fit "
             "uint64", t_ns);
    return ADAS_REF_ERR_TIME;
  }
  CHECKED(check_inputs(inputs, t_ns, fault));

  uint32_t mode;
  int32_t selected = ADAS_REF_NO_OBJECT;
  if (!inputs->radar || !inputs->camera || !inputs->ego) {
    mode = ADAS_REF_MODE_SENSOR_UNAVAILABLE;
  } else {
    object radar = from_radar(inputs->radar);
    object camera = from_camera(inputs->camera);
    mode = ADAS_REF_MODE_CLEAR;
    if (confirms(&radar, &camera)) {
      selected = radar.id;
      if (hazard(&radar)) mode = ADAS_REF_MODE_HAZARD;
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
  return ADAS_REF_OK;
}
