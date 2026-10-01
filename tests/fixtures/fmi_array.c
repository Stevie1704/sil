// An FMI 3.0 co-simulation FMU of fixed-size numeric arrays (issue #190).
//
// Each input is copied to the output of the same type and shape, except the
// [2,3] matrix, whose output depends on each element's own indices:
//
//   matrix_out[i][j] = matrix_in[i][j] + bias[i][j] + 10 * (i + 1) + (j + 1)
//
// so an importer that flattened it in any order but FMI's row-major one
// reads values the test did not author. `bias` is a fixed parameter: it is
// accepted in the instantiated state only, which is where an importer applies
// start values.
//
// Every accessor checks `nValues` against the values its references hold
// together, and refuses a call that names a variable of another type, so an
// importer that assumes one value per reference is answered with Error rather
// than with a silently shifted buffer. The description is written by
// `tests/array_fixture.py`; the value references and shapes here are its.

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum { OK = 0, ERROR = 3 };
enum { ROWS = 2, COLUMNS = 3, OBJECTS = 8, TRIMS = 3 };
enum kind { FLOAT32, FLOAT64, INT32, UINT32, UINT64 };
enum causality { PARAMETER, INPUT, OUTPUT };

typedef void (*fmi3_log_message)(void *, int, const char *, const char *);

typedef struct {
  double bias[ROWS][COLUMNS];
  double matrix_in[ROWS][COLUMNS];
  double matrix_out[ROWS][COLUMNS];
  float range_in[OBJECTS], range_out[OBJECTS];
  float trim_in[TRIMS], trim_out[TRIMS];
  float gain_in, gain_out;
  int32_t id_in[OBJECTS], id_out[OBJECTS];
  uint32_t count_in, count_out;
  uint32_t class_in[OBJECTS], class_out[OBJECTS];
  uint64_t sample_in[OBJECTS], sample_out[OBJECTS];
  bool initialized;
  fmi3_log_message log;
  void *environment;
} instance_t;

typedef struct {
  uint32_t reference;
  enum kind kind;
  enum causality causality;
  size_t offset;
  size_t count;
} variable_t;

// The bytes of one value of each kind, as a constant expression so that the
// table below can count each variable's values at compile time.
#define ELEMENT_SIZE(kind)                                                     \
  ((kind) == FLOAT32   ? sizeof(float)                                         \
   : (kind) == FLOAT64 ? sizeof(double)                                        \
   : (kind) == INT32   ? sizeof(int32_t)                                       \
   : (kind) == UINT32  ? sizeof(uint32_t)                                      \
                       : sizeof(uint64_t))

// One variable: its member of the instance, and how many values it holds.
#define VARIABLE(ref, kind, causality, member)                                 \
  {ref, kind, causality, offsetof(instance_t, member),                         \
   sizeof(((instance_t *)0)->member) / ELEMENT_SIZE(kind)}

static const variable_t variables[] = {
    VARIABLE(1, FLOAT64, PARAMETER, bias),
    VARIABLE(2, FLOAT64, INPUT, matrix_in),
    VARIABLE(3, FLOAT32, INPUT, range_in),
    VARIABLE(4, FLOAT32, INPUT, trim_in),
    VARIABLE(5, FLOAT32, INPUT, gain_in),
    VARIABLE(6, INT32, INPUT, id_in),
    VARIABLE(7, UINT32, INPUT, count_in),
    VARIABLE(8, UINT32, INPUT, class_in),
    VARIABLE(9, UINT64, INPUT, sample_in),
    VARIABLE(11, FLOAT64, OUTPUT, matrix_out),
    VARIABLE(12, FLOAT32, OUTPUT, range_out),
    VARIABLE(13, FLOAT32, OUTPUT, trim_out),
    VARIABLE(14, FLOAT32, OUTPUT, gain_out),
    VARIABLE(15, INT32, OUTPUT, id_out),
    VARIABLE(16, UINT32, OUTPUT, count_out),
    VARIABLE(17, UINT32, OUTPUT, class_out),
    VARIABLE(18, UINT64, OUTPUT, sample_out),
};

static void log_error(instance_t *instance, const char *message) {
  if (instance->log != NULL) {
    instance->log(instance->environment, ERROR, "logStatusError", message);
  }
}

static const variable_t *find(uint32_t reference) {
  for (size_t i = 0; i < sizeof variables / sizeof *variables; ++i) {
    if (variables[i].reference == reference) {
      return &variables[i];
    }
  }
  return NULL;
}

static void compute(instance_t *s) {
  for (int i = 0; i < ROWS; ++i) {
    for (int j = 0; j < COLUMNS; ++j) {
      s->matrix_out[i][j] =
          s->matrix_in[i][j] + s->bias[i][j] + 10.0 * (i + 1) + (j + 1);
    }
  }
  memcpy(s->range_out, s->range_in, sizeof s->range_in);
  memcpy(s->trim_out, s->trim_in, sizeof s->trim_in);
  s->gain_out = s->gain_in;
  memcpy(s->id_out, s->id_in, sizeof s->id_in);
  s->count_out = s->count_in;
  memcpy(s->class_out, s->class_in, sizeof s->class_in);
  memcpy(s->sample_out, s->sample_in, sizeof s->sample_in);
}

// Check one call against the variables it names before touching any of
// them, so a refused call leaves every variable as it was.
static int check(instance_t *s, enum kind kind, const uint32_t *references,
                 size_t n_references, size_t n_values, bool set) {
  char message[160];
  size_t total = 0;
  for (size_t i = 0; i < n_references; ++i) {
    const variable_t *v = find(references[i]);
    if (v == NULL || v->kind != kind) {
      snprintf(message, sizeof message,
               "value reference %u is no variable of this type",
               (unsigned)references[i]);
      log_error(s, message);
      return ERROR;
    }
    if (set && (v->causality == OUTPUT ||
                (v->causality == PARAMETER && s->initialized))) {
      snprintf(message, sizeof message,
               "value reference %u cannot be set %s",
               (unsigned)references[i],
               v->causality == OUTPUT ? "at all" : "after initialization");
      log_error(s, message);
      return ERROR;
    }
    total += v->count;
  }
  if (total != n_values) {
    snprintf(message, sizeof message,
             "%zu value references hold %zu values, not nValues %zu",
             n_references, total, n_values);
    log_error(s, message);
    return ERROR;
  }
  return OK;
}

static int transfer(void *instance, enum kind kind, const uint32_t *references,
                  size_t n_references, void *values, size_t n_values,
                  bool set) {
  instance_t *s = instance;
  int status = check(s, kind, references, n_references, n_values, set);
  if (status != OK) {
    return status;
  }
  if (!set) {
    compute(s);
  }
  char *cursor = values;
  for (size_t i = 0; i < n_references; ++i) {
    const variable_t *v = find(references[i]);
    char *member = (char *)s + v->offset;
    size_t bytes = v->count * ELEMENT_SIZE(kind);
    memcpy(set ? member : cursor, set ? cursor : member, bytes);
    cursor += bytes;
  }
  return OK;
}

#define ACCESSORS(name, kind, type)                                            \
  int fmi3Get##name(void *instance, const uint32_t *references,               \
                    size_t n_references, type *values, size_t n_values) {     \
    return transfer(instance, kind, references, n_references, values,           \
                  n_values, false);                                            \
  }                                                                            \
  int fmi3Set##name(void *instance, const uint32_t *references,               \
                    size_t n_references, const type *values,                  \
                    size_t n_values) {                                         \
    return transfer(instance, kind, references, n_references, (void *)values,   \
                  n_values, true);                                             \
  }

ACCESSORS(Float32, FLOAT32, float)
ACCESSORS(Float64, FLOAT64, double)
ACCESSORS(Int32, INT32, int32_t)
ACCESSORS(UInt32, UINT32, uint32_t)
ACCESSORS(UInt64, UINT64, uint64_t)

void *fmi3InstantiateCoSimulation(
    const char *instance_name, const char *instantiation_token,
    const char *resource_path, bool visible, bool logging_on,
    bool event_mode_used, bool early_return_allowed,
    const uint32_t *required_intermediate_variables,
    size_t n_required_intermediate_variables, void *intermediate_update,
    fmi3_log_message log_message, void *environment) {
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
  instance_t *s = calloc(1, sizeof *s);
  if (s != NULL) {
    s->log = log_message;
    s->environment = environment;
  }
  return s;
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
  return OK;
}

int fmi3ExitInitializationMode(void *instance) {
  ((instance_t *)instance)->initialized = true;
  return OK;
}

int fmi3DoStep(void *instance, double communication_point, double step_size,
               bool no_set_fmu_state_prior_to_current_point,
               bool *event_handling_needed, bool *terminate_simulation,
               bool *early_return, double *last_successful_time) {
  (void)instance;
  (void)no_set_fmu_state_prior_to_current_point;
  *event_handling_needed = false;
  *terminate_simulation = false;
  *early_return = false;
  *last_successful_time = communication_point + step_size;
  return OK;
}

int fmi3Terminate(void *instance) {
  (void)instance;
  return OK;
}

void fmi3FreeInstance(void *instance) { free(instance); }
