#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifndef FMI_FAILURE_STATUS
#define FMI_FAILURE_STATUS 2
#endif
#ifndef FMI_TERMINATE_STATUS
#define FMI_TERMINATE_STATUS 0
#endif
#ifndef FMI_TERMINATE_FLAG
#define FMI_TERMINATE_FLAG 0
#endif

typedef void (*fmi3_log_message)(void *, int, const char *, const char *);

void *fmi3InstantiateCoSimulation(
    const char *instance_name, const char *instantiation_token,
    const char *resource_path, bool visible, bool logging_on,
    bool event_mode_used, bool early_return_allowed,
    const uint32_t *required_intermediate_variables,
    size_t n_required_intermediate_variables, void *intermediate_update,
    fmi3_log_message log_message, void *environment) {
  static int instance;
  (void)instance_name;
  (void)instantiation_token;
  (void)resource_path;
  (void)visible;
  (void)logging_on;
  (void)event_mode_used;
  (void)early_return_allowed;
  (void)required_intermediate_variables;
  (void)n_required_intermediate_variables;
  (void)intermediate_update;
  (void)log_message;
  (void)environment;
  return &instance;
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
  return 0;
}

int fmi3ExitInitializationMode(void *instance) {
  (void)instance;
  return 0;
}

int fmi3DoStep(void *instance, double communication_point, double step_size,
               bool no_set_fmu_state_prior_to_current_point,
               bool *event_handling_needed, bool *terminate_simulation,
               bool *early_return, double *last_successful_time) {
  (void)instance;
  (void)communication_point;
  (void)step_size;
  (void)no_set_fmu_state_prior_to_current_point;
  (void)event_handling_needed;
#if FMI_TERMINATE_FLAG
  *terminate_simulation = true;
#else
  (void)terminate_simulation;
#endif
  (void)early_return;
  (void)last_successful_time;
  return FMI_FAILURE_STATUS;
}

int fmi3GetFloat64(void *instance, const uint32_t *value_references,
                   size_t n_value_references, double *values,
                   size_t n_values) {
  (void)instance;
  (void)value_references;
  (void)n_value_references;
  (void)values;
  (void)n_values;
  return 0;
}

int fmi3SetFloat64(void *instance, const uint32_t *value_references,
                   size_t n_value_references, double *values,
                   size_t n_values) {
  (void)instance;
  (void)value_references;
  (void)n_value_references;
  (void)values;
  (void)n_values;
  return 0;
}

int fmi3Terminate(void *instance) {
  (void)instance;
  return FMI_TERMINATE_STATUS;
}

void fmi3FreeInstance(void *instance) { (void)instance; }
