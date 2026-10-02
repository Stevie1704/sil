// A small FMI 2.0 co-simulation FMU with OSMP binary variables, for the
// OSMP mapping tests of the importer (issue #244).
//
// OSMPDummySensor needs Protobuf, OSI and an x86-64 host, so this stands in
// for it on every host the suite runs on. It carries one OSMP binary input
// `OSMPIn` and one OSMP binary output `OSMPOut`, each three fmi2Integer
// variables: the address of the bytes in two 32-bit halves and their size.
// In each fmi2DoStep it reads the input bytes from the importer's address and
// writes them reversed into a buffer of its own, which it frees and allocates
// again on the next fmi2DoStep. So an importer that does not keep its input
// buffer valid for the whole step, or that reads the output after the next
// step, reads the wrong bytes. See add_fmi2_osmp_fmu in CMakeLists.txt.
//
// Variables (value references, all Integer):
//   0 OSMPIn.base.lo   input      3 OSMPOut.base.lo  output
//   1 OSMPIn.base.hi   input      4 OSMPOut.base.hi  output
//   2 OSMPIn.size      input      5 OSMPOut.size     output
//
// A build can report a wrong output instead: OSMP_REPORTED_SIZE replaces the
// size it reports, and OSMP_NULL_ADDRESS reports address zero.

#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

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
  int integer[6];
  unsigned char *output;
} instance_t;

enum { IN_LO, IN_HI, IN_SIZE, OUT_LO, OUT_HI, OUT_SIZE };

static void *decode(int lo, int hi) {
  return (void *)(((uintptr_t)(unsigned)hi << 32) | (uintptr_t)(unsigned)lo);
}

static void encode(instance_t *instance, const void *address, int size) {
  uintptr_t value = (uintptr_t)address;
#ifdef OSMP_NULL_ADDRESS
  value = 0;
#endif
#ifdef OSMP_REPORTED_SIZE
  size = OSMP_REPORTED_SIZE;
#endif
  instance->integer[OUT_LO] = (int)(unsigned)(value & 0xFFFFFFFFu);
  instance->integer[OUT_HI] = (int)(unsigned)(value >> 32);
  instance->integer[OUT_SIZE] = size;
}

void *fmi2Instantiate(const char *instance_name, int fmu_type,
                      const char *guid, const char *resource_location,
                      const callbacks_t *functions, int visible,
                      int logging_on) {
  (void)instance_name;
  (void)guid;
  (void)resource_location;
  (void)visible;
  (void)logging_on;
  if (fmu_type != 1 || functions == NULL) return NULL;
  instance_t *instance = calloc(1, sizeof(instance_t));
  if (instance == NULL) return NULL;
  instance->callbacks = *functions;
  return instance;
}

int fmi2SetupExperiment(void *component, int tolerance_defined,
                        double tolerance, double start_time,
                        int stop_time_defined, double stop_time) {
  (void)component;
  (void)tolerance_defined;
  (void)tolerance;
  (void)start_time;
  (void)stop_time_defined;
  (void)stop_time;
  return 0;
}

int fmi2EnterInitializationMode(void *component) {
  (void)component;
  return 0;
}

int fmi2ExitInitializationMode(void *component) {
  (void)component;
  return 0;
}

int fmi2DoStep(void *component, double communication_point, double step_size,
               int no_set_prior) {
  instance_t *instance = component;
  (void)communication_point;
  (void)step_size;
  (void)no_set_prior;
  int size = instance->integer[IN_SIZE];
  const unsigned char *input =
      decode(instance->integer[IN_LO], instance->integer[IN_HI]);
  if (size < 0 || (size > 0 && input == NULL)) return 3;
  // The previous output is freed here, as OSMP allows: it is valid only
  // until the next fmi2DoStep.
  free(instance->output);
  instance->output = malloc(size > 0 ? (size_t)size : 1);
  if (instance->output == NULL) return 3;
  for (int i = 0; i < size; ++i) instance->output[i] = input[size - 1 - i];
  encode(instance, instance->output, size);
  return 0;
}

int fmi2Terminate(void *component) {
  (void)component;
  return 0;
}

void fmi2FreeInstance(void *component) {
  instance_t *instance = component;
  free(instance->output);
  free(instance);
}

int fmi2GetInteger(void *component, const unsigned *references, size_t n,
                   int *out) {
  instance_t *instance = component;
  for (size_t i = 0; i < n; ++i) {
    if (references[i] > OUT_SIZE) return 3;
    out[i] = instance->integer[references[i]];
  }
  return 0;
}

int fmi2SetInteger(void *component, const unsigned *references, size_t n,
                   const int *in) {
  instance_t *instance = component;
  for (size_t i = 0; i < n; ++i) {
    if (references[i] > IN_SIZE) return 3;
    instance->integer[references[i]] = in[i];
  }
  return 0;
}
