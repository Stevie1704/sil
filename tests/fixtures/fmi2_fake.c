// A small FMI 2.0 co-simulation FMU for the FMI 2.0 importer tests.
//
// The Reference FMUs ship FMI 2.0 binaries for x86-64 only, so this stands in
// for them on every host the suite runs on. It reports each lifecycle call
// through the importer's logger, so a test reads the order the importer drove
// it in, and it allocates its instance through the importer's allocator, so
// the required memory callbacks are exercised. Every call answers fmi2OK
// unless this build was told otherwise. See add_fmi2_fmu in CMakeLists.txt.
//
// Variables (value references):
//   0 u      Real    input              4 n  Integer input
//   1 y      Real    output = gain * u  5 m  Integer output = n + steps
//   2 gain   Real    parameter          6 b  Boolean input
//   3 offset Real    calculatedParameter = 2 * gain, set in initialization
//                                       7 c  Boolean output = !b

#include <stddef.h>
#include <stdio.h>
#include <string.h>

#ifndef FMI2_INIT_STATUS
#define FMI2_INIT_STATUS 0
#endif
#ifndef FMI2_STEP_STATUS
#define FMI2_STEP_STATUS 0
#endif
#ifndef FMI2_TERMINATE_STATUS
#define FMI2_TERMINATE_STATUS 0
#endif

typedef void (*logger_t)(void *, const char *, int, const char *, const char *,
                         ...);
typedef void *(*allocate_t)(size_t, size_t);
typedef void (*free_t)(void *);

typedef struct {
  logger_t logger;
  allocate_t allocate_memory;
  free_t free_memory;
  void *step_finished;
  void *component_environment;
} callbacks_t;

typedef struct {
  callbacks_t callbacks;
  double time;
  double real[4];
  int integer[2];
  int boolean[2];
  int steps;
} instance_t;

enum { U, Y, GAIN, OFFSET, N, M, B, C };

// The message is the format, with no arguments: an importer written with
// ctypes cannot format C varargs, so it prints the format as given.
static void report(instance_t *instance, const char *message) {
  instance->callbacks.logger(instance->callbacks.component_environment,
                             "fake", 0, "call", message);
}

static void update(instance_t *instance) {
  instance->real[Y] = instance->real[GAIN] * instance->real[U];
  instance->integer[M - N] = instance->integer[0] + instance->steps;
  instance->boolean[C - B] = !instance->boolean[0];
}

void *fmi2Instantiate(const char *instance_name, int fmu_type,
                      const char *guid, const char *resource_location,
                      const callbacks_t *functions, int visible,
                      int logging_on) {
  (void)instance_name;
  (void)guid;
  (void)visible;
  (void)logging_on;
  if (fmu_type != 1 || functions == NULL || functions->logger == NULL ||
      functions->allocate_memory == NULL || functions->free_memory == NULL)
    return NULL;
  instance_t *instance = functions->allocate_memory(1, sizeof(instance_t));
  if (instance == NULL) return NULL;
  instance->callbacks = *functions;
  instance->real[GAIN] = 1.0;
  update(instance);
  char message[1024];
  snprintf(message, sizeof message, "fmi2Instantiate %s",
           resource_location ? resource_location : "(null)");
  report(instance, message);
  return instance;
}

int fmi2SetupExperiment(void *component, int tolerance_defined,
                        double tolerance, double start_time,
                        int stop_time_defined, double stop_time) {
  instance_t *instance = component;
  (void)tolerance_defined;
  (void)tolerance;
  (void)stop_time;
  char message[128];
  snprintf(message, sizeof message, "fmi2SetupExperiment %g %d", start_time,
           stop_time_defined);
  report(instance, message);
  instance->time = start_time;
  return 0;
}

int fmi2EnterInitializationMode(void *component) {
  report(component, "fmi2EnterInitializationMode");
  return 0;
}

int fmi2ExitInitializationMode(void *component) {
  instance_t *instance = component;
  instance->real[OFFSET] = 2.0 * instance->real[GAIN];
  update(instance);
  report(instance, "fmi2ExitInitializationMode");
  return FMI2_INIT_STATUS;
}

int fmi2DoStep(void *component, double communication_point, double step_size,
               int no_set_prior) {
  instance_t *instance = component;
  (void)no_set_prior;
  char message[128];
  snprintf(message, sizeof message, "fmi2DoStep %g %g", communication_point,
           step_size);
  report(instance, message);
  instance->steps += 1;
  instance->time = communication_point + step_size;
  update(instance);
  return FMI2_STEP_STATUS;
}

int fmi2Terminate(void *component) {
  report(component, "fmi2Terminate");
  return FMI2_TERMINATE_STATUS;
}

void fmi2FreeInstance(void *component) {
  instance_t *instance = component;
  report(instance, "fmi2FreeInstance");
  instance->callbacks.free_memory(instance);
}

#define ACCESSORS(name, type, values, first, count)                        \
  int fmi2Get##name(void *component, const unsigned *references,          \
                    size_t n, type *out) {                                 \
    instance_t *instance = component;                                      \
    for (size_t i = 0; i < n; ++i) {                                       \
      if (references[i] < (first) || references[i] >= (first) + (count))  \
        return 3;                                                          \
      out[i] = instance->values[references[i] - (first)];                 \
    }                                                                      \
    return 0;                                                              \
  }                                                                        \
  int fmi2Set##name(void *component, const unsigned *references,          \
                    size_t n, const type *in) {                            \
    instance_t *instance = component;                                      \
    for (size_t i = 0; i < n; ++i) {                                       \
      if (references[i] < (first) || references[i] >= (first) + (count))  \
        return 3;                                                          \
      instance->values[references[i] - (first)] = in[i];                   \
    }                                                                      \
    update(instance);                                                      \
    return 0;                                                              \
  }

ACCESSORS(Real, double, real, U, 4)
ACCESSORS(Integer, int, integer, N, 2)
ACCESSORS(Boolean, int, boolean, B, 2)
