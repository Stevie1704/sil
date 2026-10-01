/* ADAS radar/camera reference controller — reference profile 1.
 *
 * An intentionally simplified C application: it consumes at most one
 * processed radar object, at most one processed camera object and the ego
 * speed per activation, and commands a longitudinal acceleration. It is test
 * coverage for the SiL Native participant path, not a vehicle function: its
 * constants define test behavior, not vehicle requirements, and it makes no
 * perception-accuracy or safety claim. docs/adas-reference.md is the profile.
 *
 * The application knows nothing about SiL. The caller owns each instance and
 * passes Virtual time to every advance. The application keeps no global
 * state, starts no thread, opens no file or socket, reads no clock and
 * allocates no memory, so any number of instances run side by side.
 *
 * Coordinates: one ego frame for every detection, x forward, y left, in
 * metres; relative_vx_mps is the object's longitudinal speed relative to the
 * ego, in m/s, negative when the object comes closer.
 */
#ifndef ADAS_REFERENCE_H
#define ADAS_REFERENCE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define ADAS_REF_PROFILE "sil.adas-reference.radar-camera"
#define ADAS_REF_PROFILE_VERSION 1u

/* The one supported activation Period: 10 ms. */
#define ADAS_REF_PERIOD_NS 10000000ull

/* An object ID of -1 means "no object"; a present object has an ID >= 0. */
#define ADAS_REF_NO_OBJECT (-1)

typedef enum adas_ref_mode {
  ADAS_REF_MODE_CLEAR = 0,
  ADAS_REF_MODE_HAZARD = 1,
  ADAS_REF_MODE_SENSOR_UNAVAILABLE = 2
} adas_ref_mode;

typedef enum adas_ref_status {
  ADAS_REF_OK = 0,
  ADAS_REF_ERR_CONFIG = 1,      /* a parameter is outside its range */
  ADAS_REF_ERR_PERIOD = 2,      /* an unsupported activation Period */
  ADAS_REF_ERR_INPUT = 3,       /* a nonfinite or out-of-range input value */
  ADAS_REF_ERR_SAMPLE_TIME = 4, /* an input Sample time other than t */
  ADAS_REF_ERR_TIME = 5,        /* t + Period does not fit uint64 */
  ADAS_REF_ERR_STATE = 6        /* advance on an instance that is not ready */
} adas_ref_status;

typedef struct adas_ref_config {
  uint64_t period_ns;              /* must be ADAS_REF_PERIOD_NS */
  double hazard_acceleration_mps2; /* finite, in [-10, 0) */
  double max_change_mps2;          /* per activation; finite, in (0, 10] */
} adas_ref_config;

typedef struct adas_ref_radar {
  uint64_t sample_time_ns;
  uint32_t sequence;
  uint32_t sensor_id;
  int32_t object_id;
  float x_m;
  float y_m;
  float relative_vx_mps;
  float confidence;
} adas_ref_radar;

typedef struct adas_ref_camera {
  uint64_t sample_time_ns;
  uint32_t sequence;
  uint32_t sensor_id;
  int32_t object_id;
  float x_m;
  float y_m;
  float confidence;
} adas_ref_camera;

typedef struct adas_ref_ego {
  uint64_t sample_time_ns;
  uint32_t sequence;
  float speed_mps;
} adas_ref_ego;

/* The inputs of one activation. A pointer of NULL means the sensor has no
 * observation for this activation: the required sensing is unavailable. */
typedef struct adas_ref_inputs {
  const adas_ref_radar *radar;
  const adas_ref_camera *camera;
  const adas_ref_ego *ego;
} adas_ref_inputs;

typedef struct adas_ref_output {
  uint64_t sample_time_ns; /* t + Period: the end of the advanced interval */
  uint32_t sequence;       /* activations completed, from 1 */
  uint32_t mode;           /* adas_ref_mode */
  int32_t selected_object_id;
  float target_acceleration_mps2;
  float acceleration_mps2;
} adas_ref_output;

/* A rejected configuration or activation names its cause here. */
typedef struct adas_ref_fault {
  char message[160];
} adas_ref_fault;

/* Caller-owned. Its members are private to the application. */
typedef struct adas_ref_instance {
  adas_ref_config config;
  double acceleration_mps2;
  uint32_t activations;
  int ready;
} adas_ref_instance;

/* Checks the configuration and puts the instance in its initial state. */
adas_ref_status adas_ref_init(adas_ref_instance *instance,
                              const adas_ref_config *config,
                              adas_ref_fault *fault);

/* Consumes the inputs sampled at t_ns, advances the state over
 * [t_ns, t_ns + Period] and writes the output for Sample time t_ns + Period.
 * A rejected activation leaves the state unchanged. */
adas_ref_status adas_ref_advance(adas_ref_instance *instance, uint64_t t_ns,
                                 const adas_ref_inputs *inputs,
                                 adas_ref_output *output,
                                 adas_ref_fault *fault);

/* Returns an initialized instance to its initial state, same configuration. */
void adas_ref_reset(adas_ref_instance *instance);

/* Ends the instance's lifecycle. It holds no resource, so nothing is freed;
 * an advance after terminate is rejected. */
void adas_ref_terminate(adas_ref_instance *instance);

#ifdef __cplusplus
}
#endif

#endif /* ADAS_REFERENCE_H */
