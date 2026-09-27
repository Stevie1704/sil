/*
 * EgoMotion: the FMU of the recorded-data example (issue #186).
 *
 * An FMI 3.0 co-simulation FMU with one Float64 input, two parameters and two
 * outputs, every one of them declared with a unit in modelDescription.xml:
 *
 *   acceleration      input      m/s2
 *   initial_speed     parameter  m/s
 *   initial_position  parameter  m
 *   speed             output     m/s
 *   position          output     m
 *
 * fmi3DoStep holds the acceleration constant over the step and integrates it
 * exactly, so the outputs at any communication point have a closed form a
 * reference can state without running this code.
 *
 * It declares its own entry points instead of including the FMI headers, as
 * the test fixtures do, so the example builds with nothing but a C compiler:
 *
 *   cc -shared -fPIC -O2 -o EgoMotion.so ego_motion.c
 */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

enum { FMI_OK = 0, FMI_ERROR = 3 };

enum {
  VR_TIME = 0,
  VR_ACCELERATION = 1,
  VR_INITIAL_SPEED = 2,
  VR_INITIAL_POSITION = 3,
  VR_SPEED = 4,
  VR_POSITION = 5,
};

#define TOKEN "{6f1c2c55-3c1e-4c55-9d0e-186e90a10186}"

typedef void (*fmi3_log_message)(void *, int, const char *, const char *);

typedef struct {
  double values[6];
  bool initialized;
} Instance;

void *fmi3InstantiateCoSimulation(
    const char *instance_name, const char *instantiation_token,
    const char *resource_path, bool visible, bool logging_on,
    bool event_mode_used, bool early_return_allowed,
    const uint32_t *required_intermediate_variables,
    size_t n_required_intermediate_variables, void *intermediate_update,
    fmi3_log_message log_message, void *environment) {
  (void)instance_name;
  (void)resource_path;
  (void)visible;
  (void)logging_on;
  (void)early_return_allowed;
  (void)required_intermediate_variables;
  (void)n_required_intermediate_variables;
  (void)intermediate_update;
  (void)log_message;
  (void)environment;
  if (instantiation_token == NULL || strcmp(instantiation_token, TOKEN) != 0 ||
      event_mode_used) {
    return NULL;
  }
  return calloc(1, sizeof(Instance));
}

int fmi3EnterInitializationMode(void *instance, bool tolerance_defined,
                                double tolerance, double start_time,
                                bool stop_time_defined, double stop_time) {
  (void)instance;
  (void)tolerance_defined;
  (void)tolerance;
  (void)start_time;
  (void)stop_time_defined;
  (void)stop_time;
  return FMI_OK;
}

int fmi3ExitInitializationMode(void *instance) {
  Instance *self = instance;
  self->values[VR_SPEED] = self->values[VR_INITIAL_SPEED];
  self->values[VR_POSITION] = self->values[VR_INITIAL_POSITION];
  self->initialized = true;
  return FMI_OK;
}

int fmi3DoStep(void *instance, double communication_point, double step_size,
               bool no_set_fmu_state_prior_to_current_point,
               bool *event_handling_needed, bool *terminate_simulation,
               bool *early_return, double *last_successful_time) {
  Instance *self = instance;
  double *v = self->values;
  (void)no_set_fmu_state_prior_to_current_point;
  v[VR_POSITION] += v[VR_SPEED] * step_size +
                    0.5 * v[VR_ACCELERATION] * step_size * step_size;
  v[VR_SPEED] += v[VR_ACCELERATION] * step_size;
  v[VR_TIME] = communication_point + step_size;
  *event_handling_needed = false;
  *terminate_simulation = false;
  *early_return = false;
  *last_successful_time = v[VR_TIME];
  return FMI_OK;
}

int fmi3GetFloat64(void *instance, const uint32_t *value_references,
                   size_t n_value_references, double *values,
                   size_t n_values) {
  Instance *self = instance;
  if (n_values != n_value_references) {
    return FMI_ERROR;
  }
  for (size_t i = 0; i < n_value_references; ++i) {
    if (value_references[i] > VR_POSITION) {
      return FMI_ERROR;
    }
    values[i] = self->values[value_references[i]];
  }
  return FMI_OK;
}

int fmi3SetFloat64(void *instance, const uint32_t *value_references,
                   size_t n_value_references, const double *values,
                   size_t n_values) {
  Instance *self = instance;
  if (n_values != n_value_references) {
    return FMI_ERROR;
  }
  for (size_t i = 0; i < n_value_references; ++i) {
    uint32_t reference = value_references[i];
    bool parameter =
        reference == VR_INITIAL_SPEED || reference == VR_INITIAL_POSITION;
    // The parameters are fixed: they are set before initialization or not
    // at all. The outputs and the time are never set.
    if (!(reference == VR_ACCELERATION || (parameter && !self->initialized))) {
      return FMI_ERROR;
    }
    self->values[reference] = values[i];
  }
  return FMI_OK;
}

int fmi3Terminate(void *instance) {
  (void)instance;
  return FMI_OK;
}

void fmi3FreeInstance(void *instance) { free(instance); }
