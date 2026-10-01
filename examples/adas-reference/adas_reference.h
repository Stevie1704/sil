/* ADAS radar/camera reference controller — reference profile 3.
 *
 * An intentionally simplified C application: it receives bounded lists of
 * processed radar objects, lists of processed camera objects and ego motion,
 * each at its own Period, holds the last accepted observation of each sensor,
 * and commands a longitudinal acceleration every activation. It is test
 * coverage for the SiL Native participant path, not a vehicle function: its
 * constants define test behavior, not vehicle requirements, and it makes no
 * perception-accuracy, fusion or safety claim. docs/adas-reference.md is the
 * profile.
 *
 * The application knows nothing about SiL. The caller owns each instance and
 * passes Virtual time to every advance. The application keeps no global
 * state, starts no thread, opens no file or socket, reads no clock and
 * allocates no memory, so any number of instances run side by side. It keeps
 * no track: each accepted list replaces the held list of its sensor.
 *
 * Freshness: the age of a held observation at activation t is t minus its
 * Sample time, never minus the time it arrived. An age equal to the sensor's
 * limit is fresh, a greater age is stale.
 *
 * Coordinates: one ego frame (frame_id 1) for every detection, x forward,
 * y left, in metres; relative_vx_mps is the object's longitudinal speed
 * relative to the ego, in m/s, negative when the object comes closer.
 */
#ifndef ADAS_REFERENCE_H
#define ADAS_REFERENCE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define ADAS_REF_PROFILE "sil.adas-reference.radar-camera"
#define ADAS_REF_PROFILE_VERSION 3u

/* The one supported activation Period: 10 ms. */
#define ADAS_REF_PERIOD_NS 10000000ull

/* Object IDs are >= 0; -1 is reserved for "no object selected". Radar and
 * camera IDs are independent: the same number names unrelated objects. */
#define ADAS_REF_NO_OBJECT (-1)

/* The reference capacity of one list. An example limit for test coverage,
 * not a sensor recommendation. */
#define ADAS_REF_MAX_OBJECTS 8u

#define ADAS_REF_RADAR_SENSOR_ID 1u
#define ADAS_REF_CAMERA_SENSOR_ID 2u
/* The ego frame. A list in any other frame is rejected, never transformed. */
#define ADAS_REF_EGO_FRAME_ID 1u

/* Freshness limits: the greatest age at which a held observation is still
 * fresh. Reference policy for test coverage, not a stale-data requirement. */
#define ADAS_REF_RADAR_MAX_AGE_NS 40000000ull
#define ADAS_REF_CAMERA_MAX_AGE_NS 80000000ull
#define ADAS_REF_EGO_MAX_AGE_NS 20000000ull

/* The age the output reports for a sensor with no accepted observation. */
#define ADAS_REF_NO_AGE (-1)

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
  ADAS_REF_ERR_SAMPLE_TIME = 4, /* an input Sample time after t */
  ADAS_REF_ERR_TIME = 5,        /* t + Period does not fit uint64, or t
                                   precedes a held observation */
  ADAS_REF_ERR_STATE = 6        /* advance on an instance that is not ready */
} adas_ref_status;

typedef struct adas_ref_config {
  uint64_t period_ns;              /* must be ADAS_REF_PERIOD_NS */
  double hazard_acceleration_mps2; /* finite, in [-10, 0) */
  double max_change_mps2;          /* per activation; finite, in (0, 10] */
} adas_ref_config;

/* One processed object. The camera reports no speed: its relative_vx_mps
 * is 0. */
typedef struct adas_ref_object {
  int32_t id;
  float x_m;
  float y_m;
  float relative_vx_mps;
  float confidence; /* in [0, 1] */
} adas_ref_object;

/* One sensor's complete list at one Sample time. Only objects[0, count) are
 * active; the caller need not set the others. */
typedef struct adas_ref_object_list {
  uint64_t sample_time_ns;
  uint32_t sensor_id;
  uint32_t frame_id;
  uint32_t sequence; /* increases within a Run; no wrap */
  uint32_t count;    /* at most ADAS_REF_MAX_OBJECTS */
  uint8_t validity; /* 1 valid, 0 the sensor reports no usable list */
  adas_ref_object objects[ADAS_REF_MAX_OBJECTS];
} adas_ref_object_list;

typedef struct adas_ref_ego {
  uint64_t sample_time_ns;
  uint32_t sequence;
  uint8_t validity; /* 1 valid, 0 invalid */
  float speed_mps;
} adas_ref_ego;

typedef struct adas_ref_output {
  uint64_t sample_time_ns; /* t + Period: the end of the advanced interval */
  uint32_t sequence;       /* activations completed, from 1 */
  uint32_t mode;           /* adas_ref_mode */
  int32_t selected_object_id; /* radar ID, or ADAS_REF_NO_OBJECT */
  float target_acceleration_mps2;
  float acceleration_mps2;
  int64_t radar_age_ns;          /* of the held list, or ADAS_REF_NO_AGE */
  int64_t camera_age_ns;         /* of the held list, or ADAS_REF_NO_AGE */
  int64_t ego_age_ns;            /* of the held ego motion, or ADAS_REF_NO_AGE */
  uint32_t ignored_observations; /* duplicates and regressions, from 0 */
} adas_ref_output;

/* A rejected configuration or activation names its cause here. */
typedef struct adas_ref_fault {
  char message[160];
} adas_ref_fault;

/* What the application keeps of one sensor's last accepted observation. */
typedef struct adas_ref_held {
  int present; /* 0 until the sensor's first accepted observation */
  uint32_t sequence;
  uint64_t origin_ns; /* the time its age counts from */
  uint8_t validity;
} adas_ref_held;

/* Caller-owned. Its members are private to the application. */
typedef struct adas_ref_instance {
  adas_ref_config config;
  double acceleration_mps2;
  uint32_t activations;
  uint32_t ignored_observations;
  adas_ref_held radar_held;
  adas_ref_held camera_held;
  adas_ref_held ego_held;
  adas_ref_object_list radar; /* the held lists */
  adas_ref_object_list camera;
  int ready;
} adas_ref_instance;

/* Checks the configuration and puts the instance in its initial state. */
adas_ref_status adas_ref_init(adas_ref_instance *instance,
                              const adas_ref_config *config,
                              adas_ref_fault *fault);

/* Receives one observation delivered for the activation at t_ns. The
 * caller passes every delivered observation, in Publish order, before the
 * advance at t_ns. A Sample time after t_ns is malformed and rejected. An
 * observation whose sequence does not exceed the held one is ignored and
 * counted; it does not refresh the age. Any other observation, valid or
 * invalid, becomes the held one. A rejected observation leaves the state
 * unchanged. */
adas_ref_status adas_ref_receive_radar(adas_ref_instance *instance,
                                       uint64_t t_ns,
                                       const adas_ref_object_list *list,
                                       adas_ref_fault *fault);
adas_ref_status adas_ref_receive_camera(adas_ref_instance *instance,
                                        uint64_t t_ns,
                                        const adas_ref_object_list *list,
                                        adas_ref_fault *fault);
adas_ref_status adas_ref_receive_ego(adas_ref_instance *instance,
                                     uint64_t t_ns, const adas_ref_ego *ego,
                                     adas_ref_fault *fault);

/* Decides on the held observations at t_ns, advances the state over
 * [t_ns, t_ns + Period] and writes the output for Sample time t_ns + Period.
 * The required sensing is available when every sensor holds a valid, fresh
 * observation. A rejected activation leaves the state unchanged. */
adas_ref_status adas_ref_advance(adas_ref_instance *instance, uint64_t t_ns,
                                 adas_ref_output *output,
                                 adas_ref_fault *fault);

/* Returns an initialized instance to its initial state, same configuration:
 * no observation held, nothing ignored. */
void adas_ref_reset(adas_ref_instance *instance);

/* Ends the instance's lifecycle. It holds no resource, so nothing is freed;
 * an advance after terminate is rejected. */
void adas_ref_terminate(adas_ref_instance *instance);

#ifdef __cplusplus
}
#endif

#endif /* ADAS_REFERENCE_H */
