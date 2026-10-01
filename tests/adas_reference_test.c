/* The ADAS reference application's lifecycle, without SiL (issue #222).
 *
 * The trajectories are checked at the Run boundary against enumerated
 * expectations (tests/test_example_adas_reference.py). This test covers what
 * a Run cannot show: the caller-owned lifecycle, reset, terminate, and the
 * rejections that must leave an instance's state unchanged.
 */
#include "adas_reference.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

static int failures;

#define CHECK(cond)                                              \
  do {                                                           \
    if (!(cond)) {                                               \
      fprintf(stderr, "%s:%d: CHECK(%s)\n", __FILE__, __LINE__, #cond); \
      failures++;                                                \
    }                                                            \
  } while (0)

static const adas_ref_config REFERENCE = {ADAS_REF_PERIOD_NS, -3.0, 0.5};

/* A confirmed object 30 m ahead, closing at 20 m/s: 1.5 s to collision. */
static adas_ref_radar radar_at(uint64_t t) {
  adas_ref_radar r = {t, 0, 1, 7, 30.0f, 0.0f, -20.0f, 0.9f};
  return r;
}

static adas_ref_camera camera_at(uint64_t t) {
  adas_ref_camera c = {t, 0, 2, 3, 30.5f, 0.25f, 0.8f};
  return c;
}

static adas_ref_ego ego_at(uint64_t t) {
  adas_ref_ego e = {t, 0, 20.0f};
  return e;
}

static adas_ref_status hazard_step(adas_ref_instance *instance, uint64_t t,
                                   adas_ref_output *out) {
  adas_ref_radar r = radar_at(t);
  adas_ref_camera c = camera_at(t);
  adas_ref_ego e = ego_at(t);
  adas_ref_inputs in = {&r, &c, &e};
  adas_ref_fault fault;
  return adas_ref_advance(instance, t, &in, out, &fault);
}

static void test_configuration_is_checked_before_stepping(void) {
  adas_ref_instance instance;
  adas_ref_fault fault;
  adas_ref_config config = REFERENCE;
  CHECK(adas_ref_init(&instance, &config, &fault) == ADAS_REF_OK);

  config = REFERENCE;
  config.period_ns = 20000000ull;
  CHECK(adas_ref_init(&instance, &config, &fault) == ADAS_REF_ERR_PERIOD);
  CHECK(strstr(fault.message, "20000000") != NULL);

  const double bad_hazard[] = {0.0, -10.5, NAN, -INFINITY};
  for (size_t i = 0; i < sizeof bad_hazard / sizeof *bad_hazard; i++) {
    config = REFERENCE;
    config.hazard_acceleration_mps2 = bad_hazard[i];
    CHECK(adas_ref_init(&instance, &config, &fault) == ADAS_REF_ERR_CONFIG);
    CHECK(strstr(fault.message, "hazard_acceleration_mps2") != NULL);
  }
  const double bad_change[] = {0.0, -0.5, 10.5, NAN, INFINITY};
  for (size_t i = 0; i < sizeof bad_change / sizeof *bad_change; i++) {
    config = REFERENCE;
    config.max_change_mps2 = bad_change[i];
    CHECK(adas_ref_init(&instance, &config, &fault) == ADAS_REF_ERR_CONFIG);
    CHECK(strstr(fault.message, "max_change_mps2") != NULL);
  }
  config = REFERENCE;
  config.hazard_acceleration_mps2 = -10.0;
  config.max_change_mps2 = 10.0;
  CHECK(adas_ref_init(&instance, &config, &fault) == ADAS_REF_OK);
}

static void test_rate_limit_reset_and_terminate(void) {
  adas_ref_instance instance;
  adas_ref_fault fault;
  adas_ref_output out;
  CHECK(adas_ref_init(&instance, &REFERENCE, &fault) == ADAS_REF_OK);
  const float expected[] = {-0.5f, -1.0f, -1.5f, -2.0f, -2.5f, -3.0f, -3.0f};
  for (uint32_t i = 0; i < 7; i++) {
    uint64_t t = i * ADAS_REF_PERIOD_NS;
    CHECK(hazard_step(&instance, t, &out) == ADAS_REF_OK);
    CHECK(out.sample_time_ns == t + ADAS_REF_PERIOD_NS);
    CHECK(out.sequence == i + 1);
    CHECK(out.mode == ADAS_REF_MODE_HAZARD);
    CHECK(out.selected_object_id == 7);
    CHECK(out.target_acceleration_mps2 == -3.0f);
    CHECK(out.acceleration_mps2 == expected[i]);
  }

  adas_ref_reset(&instance);
  CHECK(hazard_step(&instance, 0, &out) == ADAS_REF_OK);
  CHECK(out.sequence == 1);
  CHECK(out.acceleration_mps2 == -0.5f);

  adas_ref_terminate(&instance);
  CHECK(hazard_step(&instance, ADAS_REF_PERIOD_NS, &out) == ADAS_REF_ERR_STATE);
}

static void test_instances_keep_their_own_state(void) {
  adas_ref_instance hazard, clear;
  adas_ref_fault fault;
  adas_ref_output out;
  CHECK(adas_ref_init(&hazard, &REFERENCE, &fault) == ADAS_REF_OK);
  CHECK(adas_ref_init(&clear, &REFERENCE, &fault) == ADAS_REF_OK);
  CHECK(hazard_step(&hazard, 0, &out) == ADAS_REF_OK);
  CHECK(hazard_step(&hazard, ADAS_REF_PERIOD_NS, &out) == ADAS_REF_OK);
  CHECK(out.acceleration_mps2 == -1.0f);

  adas_ref_inputs nothing = {NULL, NULL, NULL};
  CHECK(adas_ref_advance(&clear, 0, &nothing, &out, &fault) == ADAS_REF_OK);
  CHECK(out.mode == ADAS_REF_MODE_SENSOR_UNAVAILABLE);
  CHECK(out.selected_object_id == ADAS_REF_NO_OBJECT);
  CHECK(out.acceleration_mps2 == -0.5f);
  CHECK(out.sequence == 1);

  CHECK(hazard_step(&hazard, 2 * ADAS_REF_PERIOD_NS, &out) == ADAS_REF_OK);
  CHECK(out.acceleration_mps2 == -1.5f);
  CHECK(out.sequence == 3);
}

static void test_rejected_activation_leaves_state_unchanged(void) {
  adas_ref_instance instance;
  adas_ref_fault fault;
  adas_ref_output out;
  CHECK(adas_ref_init(&instance, &REFERENCE, &fault) == ADAS_REF_OK);
  CHECK(hazard_step(&instance, 0, &out) == ADAS_REF_OK);

  uint64_t t = ADAS_REF_PERIOD_NS;
  adas_ref_radar r = radar_at(t);
  adas_ref_camera c = camera_at(t);
  adas_ref_ego e = ego_at(t);
  adas_ref_inputs in = {&r, &c, &e};

  r.x_m = NAN;
  CHECK(adas_ref_advance(&instance, t, &in, &out, &fault) ==
        ADAS_REF_ERR_INPUT);
  CHECK(strstr(fault.message, "radar.x_m") != NULL);
  r = radar_at(t);
  c.confidence = INFINITY;
  CHECK(adas_ref_advance(&instance, t, &in, &out, &fault) ==
        ADAS_REF_ERR_INPUT);
  CHECK(strstr(fault.message, "camera.confidence") != NULL);
  c = camera_at(t);
  e.speed_mps = -1.0f;
  CHECK(adas_ref_advance(&instance, t, &in, &out, &fault) ==
        ADAS_REF_ERR_INPUT);
  CHECK(strstr(fault.message, "ego.speed_mps") != NULL);
  e = ego_at(t);
  r.object_id = -2;
  CHECK(adas_ref_advance(&instance, t, &in, &out, &fault) ==
        ADAS_REF_ERR_INPUT);
  CHECK(strstr(fault.message, "radar.object_id") != NULL);
  r = radar_at(t);
  c.sample_time_ns = t - 1;
  CHECK(adas_ref_advance(&instance, t, &in, &out, &fault) ==
        ADAS_REF_ERR_SAMPLE_TIME);
  CHECK(strstr(fault.message, "camera.sample_time_ns") != NULL);
  c = camera_at(t);

  CHECK(adas_ref_advance(&instance, t, &in, &out, &fault) == ADAS_REF_OK);
  CHECK(out.sequence == 2);
  CHECK(out.acceleration_mps2 == -1.0f);

  uint64_t last = UINT64_MAX - ADAS_REF_PERIOD_NS + 1;
  r = radar_at(last);
  c = camera_at(last);
  e = ego_at(last);
  CHECK(adas_ref_advance(&instance, last, &in, &out, &fault) ==
        ADAS_REF_ERR_TIME);
}

int main(void) {
  test_configuration_is_checked_before_stepping();
  test_rate_limit_reset_and_terminate();
  test_instances_keep_their_own_state();
  test_rejected_activation_leaves_state_unchanged();
  if (failures) fprintf(stderr, "%d check(s) failed\n", failures);
  return failures ? 1 : 0;
}
