/* The ADAS reference application's lifecycle, without SiL (issues #222,
 * #223, #224).
 *
 * The trajectories are checked at the Run boundary against enumerated
 * expectations (tests/test_example_adas_reference.py). This test covers what
 * a Run cannot show: the caller-owned lifecycle, reset, terminate, the
 * rejections that must leave an instance's state unchanged, a list the
 * adapter cannot deliver, such as one with more than eight objects, and the
 * held-observation rules at their exact limits.
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

#define MS 1000000ull

static const adas_ref_config REFERENCE = {ADAS_REF_PERIOD_NS, -3.0, 0.5};

/* The sequence of an observation sampled at t: one per Period. */
static uint32_t sequence_at(uint64_t t) {
  return (uint32_t)(t / ADAS_REF_PERIOD_NS);
}

/* A confirmed object 30 m ahead, closing at 20 m/s: 1.5 s to collision. */
static adas_ref_object_list radar_at(uint64_t t) {
  adas_ref_object_list r = {t, ADAS_REF_RADAR_SENSOR_ID, ADAS_REF_EGO_FRAME_ID,
                            sequence_at(t), 1, 1,
                            {{7, 30.0f, 0.0f, -20.0f, 0.9f}}};
  return r;
}

static adas_ref_object_list camera_at(uint64_t t) {
  adas_ref_object_list c = {t, ADAS_REF_CAMERA_SENSOR_ID,
                            ADAS_REF_EGO_FRAME_ID, sequence_at(t), 1, 1,
                            {{3, 30.5f, 0.25f, 0.0f, 0.8f}}};
  return c;
}

static adas_ref_ego ego_at(uint64_t t) {
  adas_ref_ego e = {t, sequence_at(t), 1, 20.0f};
  return e;
}

/* Receives the three observations sampled at t, then advances at t. */
static adas_ref_status hazard_step(adas_ref_instance *instance, uint64_t t,
                                   adas_ref_output *out) {
  adas_ref_object_list r = radar_at(t);
  adas_ref_object_list c = camera_at(t);
  adas_ref_ego e = ego_at(t);
  adas_ref_fault fault;
  adas_ref_status status;
  if ((status = adas_ref_receive_radar(instance, t, &r, &fault)) ||
      (status = adas_ref_receive_camera(instance, t, &c, &fault)) ||
      (status = adas_ref_receive_ego(instance, t, &e, &fault)))
    return status;
  return adas_ref_advance(instance, t, out, &fault);
}

static adas_ref_status advance(adas_ref_instance *instance, uint64_t t,
                               adas_ref_output *out) {
  adas_ref_fault fault;
  return adas_ref_advance(instance, t, out, &fault);
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
    CHECK(out.radar_age_ns == 0 && out.camera_age_ns == 0 &&
          out.ego_age_ns == 0);
  }

  /* Reset forgets the held observations: sequence 0 is new again. */
  adas_ref_reset(&instance);
  CHECK(advance(&instance, 0, &out) == ADAS_REF_OK);
  CHECK(out.mode == ADAS_REF_MODE_SENSOR_UNAVAILABLE);
  CHECK(out.radar_age_ns == ADAS_REF_NO_AGE);
  adas_ref_reset(&instance);
  CHECK(hazard_step(&instance, 0, &out) == ADAS_REF_OK);
  CHECK(out.sequence == 1);
  CHECK(out.acceleration_mps2 == -0.5f);
  CHECK(out.ignored_observations == 0);

  adas_ref_terminate(&instance);
  adas_ref_object_list r = radar_at(ADAS_REF_PERIOD_NS);
  CHECK(adas_ref_receive_radar(&instance, ADAS_REF_PERIOD_NS, &r, &fault) ==
        ADAS_REF_ERR_STATE);
  CHECK(advance(&instance, ADAS_REF_PERIOD_NS, &out) == ADAS_REF_ERR_STATE);
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

  CHECK(advance(&clear, 0, &out) == ADAS_REF_OK);
  CHECK(out.mode == ADAS_REF_MODE_SENSOR_UNAVAILABLE);
  CHECK(out.selected_object_id == ADAS_REF_NO_OBJECT);
  CHECK(out.acceleration_mps2 == -0.5f);
  CHECK(out.sequence == 1);
  CHECK(out.radar_age_ns == ADAS_REF_NO_AGE &&
        out.camera_age_ns == ADAS_REF_NO_AGE &&
        out.ego_age_ns == ADAS_REF_NO_AGE);

  CHECK(hazard_step(&hazard, 2 * ADAS_REF_PERIOD_NS, &out) == ADAS_REF_OK);
  CHECK(out.acceleration_mps2 == -1.5f);
  CHECK(out.sequence == 3);
}

/* Expects the radar list to be rejected at t with status, naming field. */
static void check_radar_rejected(adas_ref_instance *instance, uint64_t t,
                                 const adas_ref_object_list *r,
                                 adas_ref_status status, const char *field) {
  adas_ref_fault fault;
  fault.message[0] = '\0';
  CHECK(adas_ref_receive_radar(instance, t, r, &fault) == status);
  if (!strstr(fault.message, field)) {
    fprintf(stderr, "fault '%s' does not name '%s'\n", fault.message, field);
    failures++;
  }
}

static void check_camera_rejected(adas_ref_instance *instance, uint64_t t,
                                  const adas_ref_object_list *c,
                                  adas_ref_status status, const char *field) {
  adas_ref_fault fault;
  fault.message[0] = '\0';
  CHECK(adas_ref_receive_camera(instance, t, c, &fault) == status);
  if (!strstr(fault.message, field)) {
    fprintf(stderr, "fault '%s' does not name '%s'\n", fault.message, field);
    failures++;
  }
}

static void check_ego_rejected(adas_ref_instance *instance, uint64_t t,
                               const adas_ref_ego *e, adas_ref_status status,
                               const char *field) {
  adas_ref_fault fault;
  fault.message[0] = '\0';
  CHECK(adas_ref_receive_ego(instance, t, e, &fault) == status);
  if (!strstr(fault.message, field)) {
    fprintf(stderr, "fault '%s' does not name '%s'\n", fault.message, field);
    failures++;
  }
}

static void test_rejected_observation_leaves_state_unchanged(void) {
  adas_ref_instance instance;
  adas_ref_fault fault;
  adas_ref_output out;
  CHECK(adas_ref_init(&instance, &REFERENCE, &fault) == ADAS_REF_OK);
  CHECK(hazard_step(&instance, 0, &out) == ADAS_REF_OK);

  const uint64_t t = ADAS_REF_PERIOD_NS;
  adas_ref_object_list r = radar_at(t);
  adas_ref_object_list c = camera_at(t);
  adas_ref_ego e = ego_at(t);

  r.objects[0].x_m = NAN;
  check_radar_rejected(&instance, t, &r, ADAS_REF_ERR_INPUT, "radar.x_m[0]");
  c.objects[0].confidence = INFINITY;
  check_camera_rejected(&instance, t, &c, ADAS_REF_ERR_INPUT,
                        "camera.confidence[0]");
  e.speed_mps = -1.0f;
  check_ego_rejected(&instance, t, &e, ADAS_REF_ERR_INPUT, "ego.speed_mps");
  c = camera_at(t);
  c.sample_time_ns = t + 1;
  check_camera_rejected(&instance, t, &c, ADAS_REF_ERR_SAMPLE_TIME,
                        "camera.sample_time_ns 10000001 is after the "
                        "activation time 10000000");

  /* Nothing rejected was held: the observations from 0 are still held. */
  CHECK(advance(&instance, t, &out) == ADAS_REF_OK);
  CHECK(out.sequence == 2);
  CHECK(out.acceleration_mps2 == -1.0f);
  CHECK(out.radar_age_ns == 10 * (int64_t)MS);
  CHECK(out.camera_age_ns == 10 * (int64_t)MS);
  CHECK(out.ignored_observations == 0);

  uint64_t last = UINT64_MAX - ADAS_REF_PERIOD_NS + 1;
  CHECK(advance(&instance, last, &out) == ADAS_REF_ERR_TIME);
  /* An advance before a held Sample time would give a negative age. */
  r = radar_at(5 * ADAS_REF_PERIOD_NS);
  CHECK(adas_ref_receive_radar(&instance, 5 * ADAS_REF_PERIOD_NS, &r,
                               &fault) == ADAS_REF_OK);
  CHECK(adas_ref_advance(&instance, 4 * ADAS_REF_PERIOD_NS, &out, &fault) ==
        ADAS_REF_ERR_TIME);
  CHECK(strstr(fault.message, "precedes") != NULL);
}

static void test_malformed_lists_are_rejected(void) {
  adas_ref_instance instance;
  adas_ref_fault fault;
  adas_ref_output out;
  CHECK(adas_ref_init(&instance, &REFERENCE, &fault) == ADAS_REF_OK);
  const uint64_t t = 0;
  adas_ref_object_list r = radar_at(t);
  adas_ref_object_list c = camera_at(t);
  adas_ref_ego e = ego_at(t);

  r.count = ADAS_REF_MAX_OBJECTS + 1;
  check_radar_rejected(&instance, t, &r, ADAS_REF_ERR_INPUT, "radar.count 9");
  r = radar_at(t);
  r.frame_id = 2;
  check_radar_rejected(&instance, t, &r, ADAS_REF_ERR_INPUT,
                       "radar.frame_id 2");
  r = radar_at(t);
  c.sensor_id = ADAS_REF_RADAR_SENSOR_ID;
  check_camera_rejected(&instance, t, &c, ADAS_REF_ERR_INPUT,
                        "camera.sensor_id 1");
  c = camera_at(t);
  r.validity = 2;
  check_radar_rejected(&instance, t, &r, ADAS_REF_ERR_INPUT,
                       "radar.validity 2");
  r = radar_at(t);
  e.validity = 7;
  check_ego_rejected(&instance, t, &e, ADAS_REF_ERR_INPUT, "ego.validity 7");
  e = ego_at(t);
  r.objects[0].id = ADAS_REF_NO_OBJECT;
  check_radar_rejected(&instance, t, &r, ADAS_REF_ERR_INPUT,
                       "radar.object_id[0] -1");
  r = radar_at(t);
  r.count = 2;
  r.objects[1] = r.objects[0];
  r.objects[1].x_m = 50.0f;
  check_radar_rejected(&instance, t, &r, ADAS_REF_ERR_INPUT,
                       "radar.object_id[1] 7 repeats radar.object_id[0]");
  r = radar_at(t);
  c.objects[0].confidence = 1.5f;
  check_camera_rejected(&instance, t, &c, ADAS_REF_ERR_INPUT,
                        "camera.confidence[0] 1.5 is outside [0, 1]");
  c = camera_at(t);
  r.objects[0].confidence = -0.25f;
  check_radar_rejected(&instance, t, &r, ADAS_REF_ERR_INPUT,
                       "radar.confidence[0] -0.25 is outside [0, 1]");
  r = radar_at(t);
  c.objects[0].relative_vx_mps = 1.0f;
  check_camera_rejected(&instance, t, &c, ADAS_REF_ERR_INPUT,
                        "camera.relative_vx_mps[0] 1 is not 0");
  c = camera_at(t);

  /* Only active objects are checked: an inactive element may hold anything,
   * because the application never reads it. */
  r.objects[1].x_m = NAN;
  r.objects[1].id = ADAS_REF_NO_OBJECT;
  CHECK(adas_ref_receive_radar(&instance, t, &r, &fault) == ADAS_REF_OK);
  CHECK(adas_ref_receive_camera(&instance, t, &c, &fault) == ADAS_REF_OK);
  CHECK(adas_ref_receive_ego(&instance, t, &e, &fault) == ADAS_REF_OK);
  CHECK(advance(&instance, t, &out) == ADAS_REF_OK);
  CHECK(out.sequence == 1);
  CHECK(out.selected_object_id == 7);
}

static void receive_all(adas_ref_instance *instance, uint64_t t,
                        const adas_ref_object_list *r,
                        const adas_ref_object_list *c, const adas_ref_ego *e) {
  adas_ref_fault fault;
  CHECK(adas_ref_receive_radar(instance, t, r, &fault) == ADAS_REF_OK);
  CHECK(adas_ref_receive_camera(instance, t, c, &fault) == ADAS_REF_OK);
  CHECK(adas_ref_receive_ego(instance, t, e, &fault) == ADAS_REF_OK);
}

static void test_selection_over_lists(void) {
  adas_ref_instance instance;
  adas_ref_fault fault;
  adas_ref_output out;
  CHECK(adas_ref_init(&instance, &REFERENCE, &fault) == ADAS_REF_OK);
  const uint64_t t = 0;
  /* Camera 4 confirms radar 9 and radar 4, which are at the same distance:
   * the lower radar ID wins. Camera ID 4 and radar ID 4 are unrelated.
   * Radar 2 is nearer but no camera detection matches it; radar 5 is
   * confirmed but farther. */
  adas_ref_object_list r = {t, ADAS_REF_RADAR_SENSOR_ID, ADAS_REF_EGO_FRAME_ID,
                            0, 4, 1,
                            {{5, 40.0f, 0.0f, 0.0f, 0.9f},
                             {9, 30.0f, 0.0f, 0.0f, 0.9f},
                             {2, 20.0f, 1.0f, 0.0f, 0.9f},
                             {4, 30.0f, 0.25f, 0.0f, 0.9f}}};
  adas_ref_object_list c = {t, ADAS_REF_CAMERA_SENSOR_ID,
                            ADAS_REF_EGO_FRAME_ID, 0, 2, 1,
                            {{4, 30.5f, 0.0f, 0.0f, 0.8f},
                             {0, 40.0f, 0.0f, 0.0f, 0.8f}}};
  adas_ref_ego e = ego_at(t);
  receive_all(&instance, t, &r, &c, &e);
  CHECK(advance(&instance, t, &out) == ADAS_REF_OK);
  CHECK(out.mode == ADAS_REF_MODE_CLEAR);
  CHECK(out.selected_object_id == 4);

  /* The same lists in reverse order select the same object. */
  const uint64_t t1 = ADAS_REF_PERIOD_NS;
  adas_ref_object_list reversed = r;
  for (uint32_t i = 0; i < r.count; i++)
    reversed.objects[i] = r.objects[r.count - 1 - i];
  reversed.sample_time_ns = c.sample_time_ns = t1;
  reversed.sequence = c.sequence = 1;
  e = ego_at(t1);
  receive_all(&instance, t1, &reversed, &c, &e);
  CHECK(advance(&instance, t1, &out) == ADAS_REF_OK);
  CHECK(out.selected_object_id == 4);

  /* Valid, fresh, empty lists: no confirmed lead. An invalid list: the
   * sensing is unavailable. */
  const uint64_t t2 = 2 * ADAS_REF_PERIOD_NS;
  adas_ref_object_list empty_radar = {t2, ADAS_REF_RADAR_SENSOR_ID,
                                      ADAS_REF_EGO_FRAME_ID, 2, 0, 1, {{0}}};
  adas_ref_object_list empty_camera = {t2, ADAS_REF_CAMERA_SENSOR_ID,
                                       ADAS_REF_EGO_FRAME_ID, 2, 0, 1, {{0}}};
  e = ego_at(t2);
  receive_all(&instance, t2, &empty_radar, &empty_camera, &e);
  CHECK(advance(&instance, t2, &out) == ADAS_REF_OK);
  CHECK(out.mode == ADAS_REF_MODE_CLEAR);
  CHECK(out.selected_object_id == ADAS_REF_NO_OBJECT);
  const uint64_t t3 = 3 * ADAS_REF_PERIOD_NS;
  empty_radar.sample_time_ns = empty_camera.sample_time_ns = t3;
  empty_radar.sequence = empty_camera.sequence = 3;
  empty_camera.validity = 0;
  e = ego_at(t3);
  receive_all(&instance, t3, &empty_radar, &empty_camera, &e);
  CHECK(advance(&instance, t3, &out) == ADAS_REF_OK);
  CHECK(out.mode == ADAS_REF_MODE_SENSOR_UNAVAILABLE);
  CHECK(out.selected_object_id == ADAS_REF_NO_OBJECT);
}

/* Each sensor is fresh at an age equal to its limit, stale one nanosecond
 * later, and fresh again with a later observation. */
static void check_limit(int sensor, uint64_t limit_ns) {
  adas_ref_instance instance;
  adas_ref_fault fault;
  adas_ref_output out;
  CHECK(adas_ref_init(&instance, &REFERENCE, &fault) == ADAS_REF_OK);
  CHECK(hazard_step(&instance, 0, &out) == ADAS_REF_OK);
  /* The other two sensors are refreshed at the probe times. */
  const uint64_t probes[] = {limit_ns, limit_ns + 1};
  const uint32_t modes[] = {ADAS_REF_MODE_HAZARD,
                            ADAS_REF_MODE_SENSOR_UNAVAILABLE};
  for (int i = 0; i < 2; i++) {
    uint64_t t = probes[i];
    adas_ref_object_list r = radar_at(t), c = camera_at(t);
    adas_ref_ego e = ego_at(t);
    r.sequence = c.sequence = e.sequence = (uint32_t)(i + 1);
    if (sensor != 0)
      CHECK(adas_ref_receive_radar(&instance, t, &r, &fault) == ADAS_REF_OK);
    if (sensor != 1)
      CHECK(adas_ref_receive_camera(&instance, t, &c, &fault) == ADAS_REF_OK);
    if (sensor != 2)
      CHECK(adas_ref_receive_ego(&instance, t, &e, &fault) == ADAS_REF_OK);
    CHECK(advance(&instance, t, &out) == ADAS_REF_OK);
    CHECK(out.mode == modes[i]);
    int64_t age = sensor == 0   ? out.radar_age_ns
                  : sensor == 1 ? out.camera_age_ns
                                : out.ego_age_ns;
    CHECK(age == (int64_t)t);
  }
}

static void test_freshness_limits_are_inclusive(void) {
  check_limit(0, ADAS_REF_RADAR_MAX_AGE_NS);
  check_limit(1, ADAS_REF_CAMERA_MAX_AGE_NS);
  check_limit(2, ADAS_REF_EGO_MAX_AGE_NS);
}

/* Age counts from the Sample time: an observation received late is as old
 * as its Sample time says, not new. */
static void test_age_counts_from_the_sample_time(void) {
  adas_ref_instance instance;
  adas_ref_fault fault;
  adas_ref_output out;
  CHECK(adas_ref_init(&instance, &REFERENCE, &fault) == ADAS_REF_OK);
  const uint64_t t = 30 * MS;
  adas_ref_object_list r = radar_at(0), c = camera_at(t);
  adas_ref_ego e = ego_at(t);
  receive_all(&instance, t, &r, &c, &e);
  CHECK(advance(&instance, t, &out) == ADAS_REF_OK);
#ifdef ADAS_REFERENCE_ARRIVAL_TIME
  CHECK(out.radar_age_ns == 0);
#else
  CHECK(out.radar_age_ns == 30 * (int64_t)MS);
#endif

  /* Received 50 ms after its Sample time: stale on arrival. */
  const uint64_t t2 = 60 * MS;
  r = radar_at(10 * MS);
  c = camera_at(t2);
  e = ego_at(t2);
  receive_all(&instance, t2, &r, &c, &e);
  CHECK(advance(&instance, t2, &out) == ADAS_REF_OK);
#ifndef ADAS_REFERENCE_ARRIVAL_TIME
  CHECK(out.radar_age_ns == 50 * (int64_t)MS);
  CHECK(out.mode == ADAS_REF_MODE_SENSOR_UNAVAILABLE);
#endif
}

/* Duplicates and regressions are ignored and counted; they do not refresh
 * the age. An invalid observation is held until a later valid one. */
static void test_sequences_must_increase(void) {
  adas_ref_instance instance;
  adas_ref_fault fault;
  adas_ref_output out;
  CHECK(adas_ref_init(&instance, &REFERENCE, &fault) == ADAS_REF_OK);
  CHECK(hazard_step(&instance, 0, &out) == ADAS_REF_OK);

  const uint64_t t = 10 * MS;
  adas_ref_object_list r = radar_at(t), c = camera_at(t);
  adas_ref_ego e = ego_at(t);
  CHECK(adas_ref_receive_camera(&instance, t, &c, &fault) == ADAS_REF_OK);
  CHECK(adas_ref_receive_ego(&instance, t, &e, &fault) == ADAS_REF_OK);
  r.sequence = 0; /* a duplicate */
  CHECK(adas_ref_receive_radar(&instance, t, &r, &fault) == ADAS_REF_OK);
  CHECK(advance(&instance, t, &out) == ADAS_REF_OK);
  CHECK(out.ignored_observations == 1);
  CHECK(out.radar_age_ns == 10 * (int64_t)MS);

  const uint64_t t2 = 20 * MS;
  r = radar_at(t2);
  r.validity = 0;
  r.sequence = 5;
  CHECK(adas_ref_receive_radar(&instance, t2, &r, &fault) == ADAS_REF_OK);
  r = radar_at(t2); /* a valid regression after the invalid list */
  r.sequence = 4;
  CHECK(adas_ref_receive_radar(&instance, t2, &r, &fault) == ADAS_REF_OK);
  CHECK(advance(&instance, t2, &out) == ADAS_REF_OK);
  CHECK(out.ignored_observations == 2);
  CHECK(out.radar_age_ns == 0);
  CHECK(out.mode == ADAS_REF_MODE_SENSOR_UNAVAILABLE);

  const uint64_t t3 = 30 * MS;
  r = radar_at(t3);
  r.sequence = 6;
  CHECK(adas_ref_receive_radar(&instance, t3, &r, &fault) == ADAS_REF_OK);
  CHECK(advance(&instance, t3, &out) == ADAS_REF_OK);
  CHECK(out.mode == ADAS_REF_MODE_HAZARD);
  CHECK(out.ignored_observations == 2);
}

int main(void) {
  test_configuration_is_checked_before_stepping();
  test_rate_limit_reset_and_terminate();
  test_instances_keep_their_own_state();
  test_rejected_observation_leaves_state_unchanged();
  test_malformed_lists_are_rejected();
  test_selection_over_lists();
  test_freshness_limits_are_inclusive();
  test_age_counts_from_the_sample_time();
  test_sequences_must_increase();
  if (failures) fprintf(stderr, "%d check(s) failed\n", failures);
  return failures ? 1 : 0;
}
