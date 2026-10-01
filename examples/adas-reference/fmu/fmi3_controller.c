/* The FMI 3.0 Co-Simulation interface of the ADAS reference application.
 *
 * Only this file knows FMI: it exports the FMI 3.0 functions, keeps the
 * values of the variables modelDescription.xml declares, and converts them
 * to the application's own types (adas_reference.h). The application is the
 * same source the Native participant links (sil_adapter.c); this file adds
 * no control behavior. docs/adas-reference.md is the profile and the FMU
 * section there is this interface.
 *
 * Each fmi3DoStep is one activation of the application at the step's
 * communication point t, over [t, t + 10 ms]. The step size is fixed: a
 * step of any other size, or at any other point, is an error, never
 * rescaled.
 *
 * FMI inputs hold their last value. A sensor's inputs are a new observation
 * when its header (sample_time_ns, sequence) differs from the header at the
 * previous step; otherwise the sensor delivers nothing at this step. The
 * start header (sample_time_ns UINT64_MAX, sequence UINT32_MAX) is "nothing
 * received yet": no accepted observation can carry that Sample time, because
 * it is after every activation time. At most one observation per sensor is
 * delivered per step, radar, camera, then ego, before the advance.
 *
 * The instance is allocated in fmi3InstantiateCoSimulation and freed in
 * fmi3FreeInstance. The FMU reads no resource, starts no thread, opens no
 * file and reads no clock. Every rejected call logs its cause through the
 * importer's logMessage callback (category logStatusError) and returns
 * fmi3Error. After a failed initialization or a failed step, only
 * fmi3Reset, fmi3FreeInstance and the getters are accepted.
 */
#include <inttypes.h>
#include <math.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "adas_reference.h"
#include "fmi3Functions.h"

#ifndef ADAS_FMU_INSTANTIATION_TOKEN
#error "package.py defines ADAS_FMU_INSTANTIATION_TOKEN"
#endif

#define CAPACITY ADAS_REF_MAX_OBJECTS
#define NO_SAMPLE_TIME UINT64_MAX
#define NO_SEQUENCE UINT32_MAX
/* How far a communication point may lie from the FMU's own next point. A
 * double holds seconds: at 1e7 s its resolution is about 2 ns, and it is
 * finer than this tolerance below about 2^62 ns. */
#define POINT_TOLERANCE_NS 1000.0
/* Virtual time starts below 2^63 ns, so llround cannot overflow. */
#define START_LIMIT_NS 9223372036854775808.0

/* --- variables ---------------------------------------------------------- */

typedef struct list_inputs {
  uint64_t sample_time_ns;
  uint32_t sensor_id;
  uint32_t frame_id;
  uint32_t sequence;
  uint32_t count;
  uint8_t validity;
  int32_t object_id[CAPACITY];
  float x_m[CAPACITY];
  float y_m[CAPACITY];
  float relative_vx_mps[CAPACITY];
  float confidence[CAPACITY];
} list_inputs;

typedef struct ego_inputs {
  uint64_t sample_time_ns;
  uint32_t sequence;
  uint8_t validity;
  float speed_mps;
} ego_inputs;

/* The value of every declared variable. */
typedef struct values {
  double time;
  double hazard_acceleration_mps2;
  double max_change_mps2;
  list_inputs radar;
  list_inputs camera;
  ego_inputs ego;
  adas_ref_output command;
} values;

typedef enum var_type {
  FLOAT32, FLOAT64, INT32, INT64, UINT8, UINT32, UINT64
} var_type;

static const char *const TYPE_NAMES[] = {"Float32", "Float64", "Int32",
                                         "Int64",   "UInt8",   "UInt32",
                                         "UInt64"};
static const size_t TYPE_SIZES[] = {sizeof(float),    sizeof(double),
                                    sizeof(int32_t),  sizeof(int64_t),
                                    sizeof(uint8_t),  sizeof(uint32_t),
                                    sizeof(uint64_t)};

typedef enum causality { INDEPENDENT, PARAMETER, INPUT, OUTPUT } causality;

typedef struct variable {
  fmi3ValueReference vr;
  const char *name;
  var_type type;
  causality causality;
  size_t offset; /* into values */
  size_t size;   /* of all its elements, in bytes */
} variable;

#define VAR(vr, name, type, causality, member)                             \
  {vr, name, type, causality, offsetof(values, member),                    \
   sizeof(((values *)0)->member)}

#define LIST_VARS(base, sensor)                                             \
  VAR(base + 0, #sensor ".sample_time_ns", UINT64, INPUT,                   \
      sensor.sample_time_ns),                                              \
  VAR(base + 1, #sensor ".sensor_id", UINT32, INPUT, sensor.sensor_id),     \
  VAR(base + 2, #sensor ".frame_id", UINT32, INPUT, sensor.frame_id),       \
  VAR(base + 3, #sensor ".sequence", UINT32, INPUT, sensor.sequence),       \
  VAR(base + 4, #sensor ".count", UINT32, INPUT, sensor.count),             \
  VAR(base + 5, #sensor ".validity", UINT8, INPUT, sensor.validity),        \
  VAR(base + 6, #sensor ".object_id", INT32, INPUT, sensor.object_id),      \
  VAR(base + 7, #sensor ".x_m", FLOAT32, INPUT, sensor.x_m),                \
  VAR(base + 8, #sensor ".y_m", FLOAT32, INPUT, sensor.y_m),                \
  VAR(base + 9, #sensor ".relative_vx_mps", FLOAT32, INPUT,                 \
      sensor.relative_vx_mps),                                             \
  VAR(base + 10, #sensor ".confidence", FLOAT32, INPUT, sensor.confidence)

/* Every variable modelDescription.xml declares, with its value reference. */
static const variable VARIABLES[] = {
    VAR(0, "time", FLOAT64, INDEPENDENT, time),
    VAR(1, "hazard_acceleration_mps2", FLOAT64, PARAMETER,
        hazard_acceleration_mps2),
    VAR(2, "max_change_mps2", FLOAT64, PARAMETER, max_change_mps2),
    LIST_VARS(100, radar),
    LIST_VARS(200, camera),
    VAR(300, "ego.sample_time_ns", UINT64, INPUT, ego.sample_time_ns),
    VAR(301, "ego.sequence", UINT32, INPUT, ego.sequence),
    VAR(302, "ego.validity", UINT8, INPUT, ego.validity),
    VAR(303, "ego.speed_mps", FLOAT32, INPUT, ego.speed_mps),
    VAR(400, "command.sample_time_ns", UINT64, OUTPUT,
        command.sample_time_ns),
    VAR(401, "command.sequence", UINT32, OUTPUT, command.sequence),
    VAR(402, "command.mode", UINT32, OUTPUT, command.mode),
    VAR(403, "command.selected_object_id", INT32, OUTPUT,
        command.selected_object_id),
    VAR(404, "command.target_acceleration_mps2", FLOAT32, OUTPUT,
        command.target_acceleration_mps2),
    VAR(405, "command.acceleration_mps2", FLOAT32, OUTPUT,
        command.acceleration_mps2),
    VAR(406, "command.radar_age_ns", INT64, OUTPUT, command.radar_age_ns),
    VAR(407, "command.camera_age_ns", INT64, OUTPUT, command.camera_age_ns),
    VAR(408, "command.ego_age_ns", INT64, OUTPUT, command.ego_age_ns),
    VAR(409, "command.ignored_observations", UINT32, OUTPUT,
        command.ignored_observations),
};

#define VARIABLE_COUNT (sizeof VARIABLES / sizeof *VARIABLES)

static const variable *find_variable(fmi3ValueReference vr) {
  for (size_t i = 0; i < VARIABLE_COUNT; i++)
    if (VARIABLES[i].vr == vr) return &VARIABLES[i];
  return NULL;
}

/* The start values modelDescription.xml declares. */
static void start_list(list_inputs *list, uint32_t sensor_id) {
  memset(list, 0, sizeof *list);
  list->sample_time_ns = NO_SAMPLE_TIME;
  list->sensor_id = sensor_id;
  list->frame_id = ADAS_REF_EGO_FRAME_ID;
  list->sequence = NO_SEQUENCE;
}

static void start_values(values *v) {
  memset(v, 0, sizeof *v);
  v->hazard_acceleration_mps2 = -3.0;
  v->max_change_mps2 = 0.5;
  start_list(&v->radar, ADAS_REF_RADAR_SENSOR_ID);
  start_list(&v->camera, ADAS_REF_CAMERA_SENSOR_ID);
  v->ego.sample_time_ns = NO_SAMPLE_TIME;
  v->ego.sequence = NO_SEQUENCE;
  /* No activation yet: sequence 0, nothing held, nothing selected. */
  v->command.mode = ADAS_REF_MODE_SENSOR_UNAVAILABLE;
  v->command.selected_object_id = ADAS_REF_NO_OBJECT;
  v->command.radar_age_ns = ADAS_REF_NO_AGE;
  v->command.camera_age_ns = ADAS_REF_NO_AGE;
  v->command.ego_age_ns = ADAS_REF_NO_AGE;
}

/* --- the instance ------------------------------------------------------- */

typedef enum phase {
  INSTANTIATED, INITIALIZATION, STEP, TERMINATED, FAILED
} phase;

static const char *const PHASE_NAMES[] = {"Instantiated", "Initialization",
                                          "Step", "Terminated", "failed"};

/* The header of the last observation a sensor's inputs carried. */
typedef struct header {
  uint64_t sample_time_ns;
  uint32_t sequence;
} header;

typedef struct instance {
  char name[64];
  fmi3InstanceEnvironment environment;
  fmi3LogMessageCallback log_message;
  phase phase;
  values v;
  uint64_t next_ns; /* the communication point of the next step */
  header radar_seen;
  header camera_seen;
  header ego_seen;
  adas_ref_instance app;
} instance;

static void log_error_to(fmi3LogMessageCallback log_message,
                         fmi3InstanceEnvironment environment,
                         const char *format, va_list arguments) {
  char message[300];
  vsnprintf(message, sizeof message, format, arguments);
  if (log_message) log_message(environment, fmi3Error, "logStatusError",
                               message);
}

/* Logs the cause of a rejected call. */
static fmi3Status reject(instance *self, const char *format, ...) {
  va_list arguments;
  va_start(arguments, format);
  log_error_to(self->log_message, self->environment, format, arguments);
  va_end(arguments);
  return fmi3Error;
}

/* Logs the cause and leaves the instance failed. */
static fmi3Status fail(instance *self, const char *format, ...) {
  va_list arguments;
  va_start(arguments, format);
  log_error_to(self->log_message, self->environment, format, arguments);
  va_end(arguments);
  self->phase = FAILED;
  return fmi3Error;
}

static const header NOTHING_SEEN = {NO_SAMPLE_TIME, NO_SEQUENCE};

static void reset_instance(instance *self) {
  self->phase = INSTANTIATED;
  start_values(&self->v);
  self->next_ns = 0;
  self->radar_seen = NOTHING_SEEN;
  self->camera_seen = NOTHING_SEEN;
  self->ego_seen = NOTHING_SEEN;
  memset(&self->app, 0, sizeof self->app);
}

/* --- getting and setting ------------------------------------------------ */

static int may_get(phase phase) { return phase != INSTANTIATED; }

static int may_set(causality causality, phase phase) {
  switch (causality) {
    case PARAMETER:
      return phase == INSTANTIATED || phase == INITIALIZATION;
    case INPUT:
      return phase == INSTANTIATED || phase == INITIALIZATION ||
             phase == STEP;
    default:
      return 0;
  }
}

/* Looks every reference up and checks its type and the number of values.
 * Returns fmi3OK or the logged rejection. */
static fmi3Status check_references(instance *self, const char *function,
                                   var_type type,
                                   const fmi3ValueReference vrs[],
                                   size_t n_vrs, size_t n_values,
                                   int setting) {
  size_t total = 0;
  for (size_t i = 0; i < n_vrs; i++) {
    const variable *var = find_variable(vrs[i]);
    if (!var)
      return reject(self, "%s: no variable has value reference %" PRIu32,
                    function, vrs[i]);
    if (var->type != type)
      return reject(self, "%s: %s is a %s variable", function, var->name,
                    TYPE_NAMES[var->type]);
    if (setting && !may_set(var->causality, self->phase))
      return reject(self, "%s: %s cannot be set in the %s phase", function,
                    var->name, PHASE_NAMES[self->phase]);
    total += var->size / TYPE_SIZES[type];
  }
  if (total != n_values)
    return reject(self, "%s: the references hold %zu values, not %zu",
                  function, total, n_values);
  return fmi3OK;
}

static fmi3Status get_values(fmi3Instance instance_, const char *function,
                             var_type type, const fmi3ValueReference vrs[],
                             size_t n_vrs, void *out, size_t n_values) {
  instance *self = instance_;
  if (!may_get(self->phase))
    return reject(self, "%s is not allowed in the %s phase", function,
                  PHASE_NAMES[self->phase]);
  fmi3Status status =
      check_references(self, function, type, vrs, n_vrs, n_values, 0);
  if (status != fmi3OK) return status;
  unsigned char *to = out;
  for (size_t i = 0; i < n_vrs; i++) {
    const variable *var = find_variable(vrs[i]);
    memcpy(to, (const unsigned char *)&self->v + var->offset, var->size);
    to += var->size;
  }
  return fmi3OK;
}

/* Sets every value or, when any reference is rejected, none. */
static fmi3Status set_values(fmi3Instance instance_, const char *function,
                             var_type type, const fmi3ValueReference vrs[],
                             size_t n_vrs, const void *in, size_t n_values) {
  instance *self = instance_;
  fmi3Status status =
      check_references(self, function, type, vrs, n_vrs, n_values, 1);
  if (status != fmi3OK) return status;
  const unsigned char *from = in;
  for (size_t i = 0; i < n_vrs; i++) {
    const variable *var = find_variable(vrs[i]);
    memcpy((unsigned char *)&self->v + var->offset, from, var->size);
    from += var->size;
  }
  return fmi3OK;
}

#define GETTER_SETTER(Type, TYPE)                                           \
  fmi3Status fmi3Get##Type(fmi3Instance instance,                           \
                           const fmi3ValueReference valueReferences[],      \
                           size_t nValueReferences, fmi3##Type values[],    \
                           size_t nValues) {                                \
    return get_values(instance, "fmi3Get" #Type, TYPE, valueReferences,     \
                      nValueReferences, values, nValues);                   \
  }                                                                         \
  fmi3Status fmi3Set##Type(fmi3Instance instance,                           \
                           const fmi3ValueReference valueReferences[],      \
                           size_t nValueReferences,                         \
                           const fmi3##Type values[], size_t nValues) {     \
    return set_values(instance, "fmi3Set" #Type, TYPE, valueReferences,     \
                      nValueReferences, values, nValues);                   \
  }

GETTER_SETTER(Float32, FLOAT32)
GETTER_SETTER(Float64, FLOAT64)
GETTER_SETTER(Int32, INT32)
GETTER_SETTER(Int64, INT64)
GETTER_SETTER(UInt8, UINT8)
GETTER_SETTER(UInt32, UINT32)
GETTER_SETTER(UInt64, UINT64)

/* --- the step ----------------------------------------------------------- */

/* Fails unless elements [count, capacity) of array are all-bits zero. */
static int inactive_zero(instance *self, uint64_t t, const char *sensor,
                         const char *field, const void *array, size_t size,
                         uint32_t count) {
  static const unsigned char zero[sizeof(double)];
  const unsigned char *bytes = array;
  for (uint32_t i = count; i < CAPACITY; i++) {
    if (memcmp(bytes + i * size, zero, size) == 0) continue;
    fail(self,
         "t=%" PRIu64 " ns: %s.%s[%" PRIu32 "] is inactive (count %" PRIu32
         ") but not zero; inactive elements must be zero",
         t, sensor, field, i, count);
    return 0;
  }
  return 1;
}

#define INACTIVE_ZERO(field)                                               \
  inactive_zero(self, t, sensor, #field, in->field, sizeof *in->field,     \
                in->count)

/* Assembles the application's list from the inputs, or fails. */
static int to_list(instance *self, uint64_t t, const char *sensor,
                   const list_inputs *in, adas_ref_object_list *list) {
  if (in->count > CAPACITY) {
    fail(self, "t=%" PRIu64 " ns: %s.count %" PRIu32
               " exceeds the capacity %u",
         t, sensor, in->count, CAPACITY);
    return 0;
  }
  if (!INACTIVE_ZERO(object_id) || !INACTIVE_ZERO(x_m) ||
      !INACTIVE_ZERO(y_m) || !INACTIVE_ZERO(relative_vx_mps) ||
      !INACTIVE_ZERO(confidence))
    return 0;
  memset(list, 0, sizeof *list);
  list->sample_time_ns = in->sample_time_ns;
  list->sensor_id = in->sensor_id;
  list->frame_id = in->frame_id;
  list->sequence = in->sequence;
  list->count = in->count;
  list->validity = in->validity;
  for (uint32_t i = 0; i < in->count; i++)
    list->objects[i] = (adas_ref_object){in->object_id[i], in->x_m[i],
                                         in->y_m[i], in->relative_vx_mps[i],
                                         in->confidence[i]};
  return 1;
}

/* Whether the inputs carry a header the previous step did not see; the
 * header is then seen. */
static int is_new(header *seen, uint64_t sample_time_ns, uint32_t sequence) {
  if (seen->sample_time_ns == sample_time_ns && seen->sequence == sequence)
    return 0;
  *seen = (header){sample_time_ns, sequence};
  return 1;
}

static int succeeded(instance *self, uint64_t t, adas_ref_status status,
                     const adas_ref_fault *fault) {
  if (status == ADAS_REF_OK) return 1;
  fail(self, "t=%" PRIu64 " ns: %s", t, fault->message);
  return 0;
}

typedef adas_ref_status (*receive_list_fn)(adas_ref_instance *, uint64_t,
                                           const adas_ref_object_list *,
                                           adas_ref_fault *);

static int deliver_list(instance *self, uint64_t t, const char *sensor,
                        const list_inputs *in, header *seen,
                        receive_list_fn receive) {
  if (!is_new(seen, in->sample_time_ns, in->sequence)) return 1;
  adas_ref_object_list list;
  adas_ref_fault fault;
  return to_list(self, t, sensor, in, &list) &&
         succeeded(self, t, receive(&self->app, t, &list, &fault), &fault);
}

static int deliver_ego(instance *self, uint64_t t) {
  const ego_inputs *in = &self->v.ego;
  if (!is_new(&self->ego_seen, in->sample_time_ns, in->sequence)) return 1;
  adas_ref_ego ego = {in->sample_time_ns, in->sequence, in->validity,
                      in->speed_mps};
  adas_ref_fault fault;
  return succeeded(self, t, adas_ref_receive_ego(&self->app, t, &ego, &fault),
                   &fault);
}

/* Whether the step is the fixed Period, to the nanosecond, at the FMU's
 * next point; otherwise the instance fails. The step is never rescaled. */
static int is_fixed_step(instance *self, double point, double step) {
  if (!(step > 0.0 && llround(step * 1e9) == (long long)ADAS_REF_PERIOD_NS)) {
    fail(self,
         "communicationStepSize %.17g s is not the fixed step %llu ns; the "
         "FMU has no variable step",
         step, ADAS_REF_PERIOD_NS);
    return 0;
  }
  if (!(fabs(point * 1e9 - (double)self->next_ns) <= POINT_TOLERANCE_NS)) {
    fail(self,
         "currentCommunicationPoint %.17g s is not the next point %" PRIu64
         " ns",
         point, self->next_ns);
    return 0;
  }
  return 1;
}

fmi3Status fmi3DoStep(fmi3Instance instance_,
                      fmi3Float64 currentCommunicationPoint,
                      fmi3Float64 communicationStepSize,
                      fmi3Boolean noSetFMUStatePriorToCurrentPoint,
                      fmi3Boolean *eventHandlingNeeded,
                      fmi3Boolean *terminateSimulation,
                      fmi3Boolean *earlyReturn,
                      fmi3Float64 *lastSuccessfulTime) {
  instance *self = instance_;
  (void)noSetFMUStatePriorToCurrentPoint;
  *eventHandlingNeeded = fmi3False;
  *terminateSimulation = fmi3False;
  *earlyReturn = fmi3False;
  *lastSuccessfulTime = currentCommunicationPoint;
  if (self->phase != STEP)
    return reject(self, "fmi3DoStep is not allowed in the %s phase",
                  PHASE_NAMES[self->phase]);
  if (!is_fixed_step(self, currentCommunicationPoint, communicationStepSize))
    return fmi3Error;
  uint64_t t = self->next_ns;
  if (!deliver_list(self, t, "radar", &self->v.radar, &self->radar_seen,
                    adas_ref_receive_radar) ||
      !deliver_list(self, t, "camera", &self->v.camera, &self->camera_seen,
                    adas_ref_receive_camera) ||
      !deliver_ego(self, t))
    return fmi3Error;
  adas_ref_fault fault;
  if (!succeeded(self, t,
                 adas_ref_advance(&self->app, t, &self->v.command, &fault),
                 &fault))
    return fmi3Error;
  self->next_ns = self->v.command.sample_time_ns;
  self->v.time = (double)self->next_ns / 1e9;
  *lastSuccessfulTime = self->v.time;
  return fmi3OK;
}

/* --- lifecycle ---------------------------------------------------------- */

const char *fmi3GetVersion(void) { return fmi3Version; }

fmi3Status fmi3SetDebugLogging(fmi3Instance instance, fmi3Boolean loggingOn,
                               size_t nCategories,
                               const fmi3String categories[]) {
  /* Errors are always logged; there is no other category. */
  (void)instance;
  (void)loggingOn;
  (void)nCategories;
  (void)categories;
  return fmi3OK;
}

fmi3Instance fmi3InstantiateCoSimulation(
    fmi3String instanceName, fmi3String instantiationToken,
    fmi3String resourcePath, fmi3Boolean visible, fmi3Boolean loggingOn,
    fmi3Boolean eventModeUsed, fmi3Boolean earlyReturnAllowed,
    const fmi3ValueReference requiredIntermediateVariables[],
    size_t nRequiredIntermediateVariables,
    fmi3InstanceEnvironment instanceEnvironment,
    fmi3LogMessageCallback logMessage,
    fmi3IntermediateUpdateCallback intermediateUpdate) {
  /* The FMU reads no resource; resourcePath may be NULL. It never returns
   * early, so earlyReturnAllowed changes nothing. */
  (void)resourcePath;
  (void)visible;
  (void)loggingOn;
  (void)earlyReturnAllowed;
  (void)requiredIntermediateVariables;
  (void)intermediateUpdate;
  instance refused = {.environment = instanceEnvironment,
                      .log_message = logMessage};
  if (!instantiationToken ||
      strcmp(instantiationToken, ADAS_FMU_INSTANTIATION_TOKEN) != 0) {
    reject(&refused, "the instantiation token %s is not this FMU's %s",
           instantiationToken ? instantiationToken : "(null)",
           ADAS_FMU_INSTANTIATION_TOKEN);
    return NULL;
  }
  if (eventModeUsed) {
    reject(&refused, "the FMU has no Event Mode (hasEventMode=false)");
    return NULL;
  }
  if (nRequiredIntermediateVariables != 0) {
    reject(&refused, "the FMU provides no intermediate update");
    return NULL;
  }
  instance *self = calloc(1, sizeof *self);
  if (!self) {
    reject(&refused, "cannot allocate the instance");
    return NULL;
  }
  snprintf(self->name, sizeof self->name, "%s",
           instanceName ? instanceName : "");
  self->environment = instanceEnvironment;
  self->log_message = logMessage;
  reset_instance(self);
  return self;
}

fmi3Status fmi3EnterInitializationMode(fmi3Instance instance_,
                                       fmi3Boolean toleranceDefined,
                                       fmi3Float64 tolerance,
                                       fmi3Float64 startTime,
                                       fmi3Boolean stopTimeDefined,
                                       fmi3Float64 stopTime) {
  instance *self = instance_;
  (void)toleranceDefined;
  (void)tolerance;
  (void)stopTimeDefined;
  (void)stopTime;
  if (self->phase != INSTANTIATED)
    return reject(self,
                  "fmi3EnterInitializationMode is not allowed in the %s "
                  "phase",
                  PHASE_NAMES[self->phase]);
  /* Virtual time is whole nanoseconds; the start time is rounded to the
   * nearest one. */
  if (!(startTime >= 0.0 && startTime * 1e9 < START_LIMIT_NS))
    return fail(self, "the start time %.17g s is not in [0, 2^63 ns)",
                startTime);
  self->next_ns = (uint64_t)llround(startTime * 1e9);
  self->v.time = startTime;
  self->phase = INITIALIZATION;
  return fmi3OK;
}

fmi3Status fmi3ExitInitializationMode(fmi3Instance instance_) {
  instance *self = instance_;
  if (self->phase != INITIALIZATION)
    return reject(self,
                  "fmi3ExitInitializationMode is not allowed in the %s phase",
                  PHASE_NAMES[self->phase]);
  adas_ref_config config = {ADAS_REF_PERIOD_NS,
                            self->v.hazard_acceleration_mps2,
                            self->v.max_change_mps2};
  adas_ref_fault fault;
  if (adas_ref_init(&self->app, &config, &fault) != ADAS_REF_OK)
    return fail(self, "initialization failed: %s", fault.message);
  self->phase = STEP;
  return fmi3OK;
}

fmi3Status fmi3Terminate(fmi3Instance instance_) {
  instance *self = instance_;
  if (self->phase != STEP)
    return reject(self, "fmi3Terminate is not allowed in the %s phase",
                  PHASE_NAMES[self->phase]);
  adas_ref_terminate(&self->app);
  self->phase = TERMINATED;
  return fmi3OK;
}

fmi3Status fmi3Reset(fmi3Instance instance) {
  reset_instance(instance);
  return fmi3OK;
}

void fmi3FreeInstance(fmi3Instance instance) { free(instance); }

/* --- what this FMU does not provide ------------------------------------- */

/* Each function below belongs to a capability modelDescription.xml does
 * not declare: no Model Exchange, Scheduled Execution, Event Mode, Clocks,
 * FMU state, derivatives, configuration, or variables of other types. */
#define UNSUPPORTED(function)                                               \
  reject(instance, "%s is not supported by this FMU", #function)

fmi3Instance fmi3InstantiateModelExchange(
    fmi3String instanceName, fmi3String instantiationToken,
    fmi3String resourcePath, fmi3Boolean visible, fmi3Boolean loggingOn,
    fmi3InstanceEnvironment instanceEnvironment,
    fmi3LogMessageCallback logMessage) {
  (void)instanceName, (void)instantiationToken, (void)resourcePath;
  (void)visible, (void)loggingOn, (void)instanceEnvironment, (void)logMessage;
  return NULL;
}

fmi3Instance fmi3InstantiateScheduledExecution(
    fmi3String instanceName, fmi3String instantiationToken,
    fmi3String resourcePath, fmi3Boolean visible, fmi3Boolean loggingOn,
    fmi3InstanceEnvironment instanceEnvironment,
    fmi3LogMessageCallback logMessage, fmi3ClockUpdateCallback clockUpdate,
    fmi3LockPreemptionCallback lockPreemption,
    fmi3UnlockPreemptionCallback unlockPreemption) {
  (void)instanceName, (void)instantiationToken, (void)resourcePath;
  (void)visible, (void)loggingOn, (void)instanceEnvironment, (void)logMessage;
  (void)clockUpdate, (void)lockPreemption, (void)unlockPreemption;
  return NULL;
}

#define UNSUPPORTED_GETTER_SETTER(Type)                                     \
  fmi3Status fmi3Get##Type(fmi3Instance instance,                           \
                           const fmi3ValueReference valueReferences[],      \
                           size_t nValueReferences, fmi3##Type values[],    \
                           size_t nValues) {                                \
    (void)valueReferences, (void)nValueReferences, (void)values,            \
        (void)nValues;                                                      \
    return UNSUPPORTED(fmi3Get##Type);                                      \
  }                                                                         \
  fmi3Status fmi3Set##Type(fmi3Instance instance,                           \
                           const fmi3ValueReference valueReferences[],      \
                           size_t nValueReferences,                         \
                           const fmi3##Type values[], size_t nValues) {     \
    (void)valueReferences, (void)nValueReferences, (void)values,            \
        (void)nValues;                                                      \
    return UNSUPPORTED(fmi3Set##Type);                                      \
  }

UNSUPPORTED_GETTER_SETTER(Int8)
UNSUPPORTED_GETTER_SETTER(UInt16)
UNSUPPORTED_GETTER_SETTER(Int16)
UNSUPPORTED_GETTER_SETTER(Boolean)
UNSUPPORTED_GETTER_SETTER(String)

fmi3Status fmi3GetBinary(fmi3Instance instance,
                         const fmi3ValueReference valueReferences[],
                         size_t nValueReferences, size_t valueSizes[],
                         fmi3Binary values[], size_t nValues) {
  (void)valueReferences, (void)nValueReferences, (void)valueSizes;
  (void)values, (void)nValues;
  return UNSUPPORTED(fmi3GetBinary);
}

fmi3Status fmi3SetBinary(fmi3Instance instance,
                         const fmi3ValueReference valueReferences[],
                         size_t nValueReferences, const size_t valueSizes[],
                         const fmi3Binary values[], size_t nValues) {
  (void)valueReferences, (void)nValueReferences, (void)valueSizes;
  (void)values, (void)nValues;
  return UNSUPPORTED(fmi3SetBinary);
}

fmi3Status fmi3GetClock(fmi3Instance instance,
                        const fmi3ValueReference valueReferences[],
                        size_t nValueReferences, fmi3Clock values[]) {
  (void)valueReferences, (void)nValueReferences, (void)values;
  return UNSUPPORTED(fmi3GetClock);
}

fmi3Status fmi3SetClock(fmi3Instance instance,
                        const fmi3ValueReference valueReferences[],
                        size_t nValueReferences, const fmi3Clock values[]) {
  (void)valueReferences, (void)nValueReferences, (void)values;
  return UNSUPPORTED(fmi3SetClock);
}

fmi3Status fmi3EnterEventMode(fmi3Instance instance) {
  return UNSUPPORTED(fmi3EnterEventMode);
}

fmi3Status fmi3EnterStepMode(fmi3Instance instance) {
  return UNSUPPORTED(fmi3EnterStepMode);
}

fmi3Status fmi3EnterConfigurationMode(fmi3Instance instance) {
  return UNSUPPORTED(fmi3EnterConfigurationMode);
}

fmi3Status fmi3ExitConfigurationMode(fmi3Instance instance) {
  return UNSUPPORTED(fmi3ExitConfigurationMode);
}

fmi3Status fmi3GetNumberOfVariableDependencies(
    fmi3Instance instance, fmi3ValueReference valueReference,
    size_t *nDependencies) {
  (void)valueReference, (void)nDependencies;
  return UNSUPPORTED(fmi3GetNumberOfVariableDependencies);
}

fmi3Status fmi3GetVariableDependencies(
    fmi3Instance instance, fmi3ValueReference dependent,
    size_t elementIndicesOfDependent[], fmi3ValueReference independents[],
    size_t elementIndicesOfIndependents[],
    fmi3DependencyKind dependencyKinds[], size_t nDependencies) {
  (void)dependent, (void)elementIndicesOfDependent, (void)independents;
  (void)elementIndicesOfIndependents, (void)dependencyKinds;
  (void)nDependencies;
  return UNSUPPORTED(fmi3GetVariableDependencies);
}

fmi3Status fmi3GetFMUState(fmi3Instance instance, fmi3FMUState *FMUState) {
  (void)FMUState;
  return UNSUPPORTED(fmi3GetFMUState);
}

fmi3Status fmi3SetFMUState(fmi3Instance instance, fmi3FMUState FMUState) {
  (void)FMUState;
  return UNSUPPORTED(fmi3SetFMUState);
}

fmi3Status fmi3FreeFMUState(fmi3Instance instance, fmi3FMUState *FMUState) {
  (void)FMUState;
  return UNSUPPORTED(fmi3FreeFMUState);
}

fmi3Status fmi3SerializedFMUStateSize(fmi3Instance instance,
                                      fmi3FMUState FMUState, size_t *size) {
  (void)FMUState, (void)size;
  return UNSUPPORTED(fmi3SerializedFMUStateSize);
}

fmi3Status fmi3SerializeFMUState(fmi3Instance instance, fmi3FMUState FMUState,
                                 fmi3Byte serializedState[], size_t size) {
  (void)FMUState, (void)serializedState, (void)size;
  return UNSUPPORTED(fmi3SerializeFMUState);
}

fmi3Status fmi3DeserializeFMUState(fmi3Instance instance,
                                   const fmi3Byte serializedState[],
                                   size_t size, fmi3FMUState *FMUState) {
  (void)serializedState, (void)size, (void)FMUState;
  return UNSUPPORTED(fmi3DeserializeFMUState);
}

fmi3Status fmi3GetDirectionalDerivative(
    fmi3Instance instance, const fmi3ValueReference unknowns[],
    size_t nUnknowns, const fmi3ValueReference knowns[], size_t nKnowns,
    const fmi3Float64 seed[], size_t nSeed, fmi3Float64 sensitivity[],
    size_t nSensitivity) {
  (void)unknowns, (void)nUnknowns, (void)knowns, (void)nKnowns;
  (void)seed, (void)nSeed, (void)sensitivity, (void)nSensitivity;
  return UNSUPPORTED(fmi3GetDirectionalDerivative);
}

fmi3Status fmi3GetAdjointDerivative(
    fmi3Instance instance, const fmi3ValueReference unknowns[],
    size_t nUnknowns, const fmi3ValueReference knowns[], size_t nKnowns,
    const fmi3Float64 seed[], size_t nSeed, fmi3Float64 sensitivity[],
    size_t nSensitivity) {
  (void)unknowns, (void)nUnknowns, (void)knowns, (void)nKnowns;
  (void)seed, (void)nSeed, (void)sensitivity, (void)nSensitivity;
  return UNSUPPORTED(fmi3GetAdjointDerivative);
}

fmi3Status fmi3GetIntervalDecimal(fmi3Instance instance,
                                  const fmi3ValueReference valueReferences[],
                                  size_t nValueReferences,
                                  fmi3Float64 intervals[],
                                  fmi3IntervalQualifier qualifiers[]) {
  (void)valueReferences, (void)nValueReferences, (void)intervals;
  (void)qualifiers;
  return UNSUPPORTED(fmi3GetIntervalDecimal);
}

fmi3Status fmi3GetIntervalFraction(fmi3Instance instance,
                                   const fmi3ValueReference valueReferences[],
                                   size_t nValueReferences,
                                   fmi3UInt64 counters[],
                                   fmi3UInt64 resolutions[],
                                   fmi3IntervalQualifier qualifiers[]) {
  (void)valueReferences, (void)nValueReferences, (void)counters;
  (void)resolutions, (void)qualifiers;
  return UNSUPPORTED(fmi3GetIntervalFraction);
}

fmi3Status fmi3GetShiftDecimal(fmi3Instance instance,
                               const fmi3ValueReference valueReferences[],
                               size_t nValueReferences, fmi3Float64 shifts[]) {
  (void)valueReferences, (void)nValueReferences, (void)shifts;
  return UNSUPPORTED(fmi3GetShiftDecimal);
}

fmi3Status fmi3GetShiftFraction(fmi3Instance instance,
                                const fmi3ValueReference valueReferences[],
                                size_t nValueReferences,
                                fmi3UInt64 counters[],
                                fmi3UInt64 resolutions[]) {
  (void)valueReferences, (void)nValueReferences, (void)counters;
  (void)resolutions;
  return UNSUPPORTED(fmi3GetShiftFraction);
}

fmi3Status fmi3SetIntervalDecimal(fmi3Instance instance,
                                  const fmi3ValueReference valueReferences[],
                                  size_t nValueReferences,
                                  const fmi3Float64 intervals[]) {
  (void)valueReferences, (void)nValueReferences, (void)intervals;
  return UNSUPPORTED(fmi3SetIntervalDecimal);
}

fmi3Status fmi3SetIntervalFraction(fmi3Instance instance,
                                   const fmi3ValueReference valueReferences[],
                                   size_t nValueReferences,
                                   const fmi3UInt64 counters[],
                                   const fmi3UInt64 resolutions[]) {
  (void)valueReferences, (void)nValueReferences, (void)counters;
  (void)resolutions;
  return UNSUPPORTED(fmi3SetIntervalFraction);
}

fmi3Status fmi3SetShiftDecimal(fmi3Instance instance,
                               const fmi3ValueReference valueReferences[],
                               size_t nValueReferences,
                               const fmi3Float64 shifts[]) {
  (void)valueReferences, (void)nValueReferences, (void)shifts;
  return UNSUPPORTED(fmi3SetShiftDecimal);
}

fmi3Status fmi3SetShiftFraction(fmi3Instance instance,
                                const fmi3ValueReference valueReferences[],
                                size_t nValueReferences,
                                const fmi3UInt64 counters[],
                                const fmi3UInt64 resolutions[]) {
  (void)valueReferences, (void)nValueReferences, (void)counters;
  (void)resolutions;
  return UNSUPPORTED(fmi3SetShiftFraction);
}

fmi3Status fmi3EvaluateDiscreteStates(fmi3Instance instance) {
  return UNSUPPORTED(fmi3EvaluateDiscreteStates);
}

fmi3Status fmi3UpdateDiscreteStates(
    fmi3Instance instance, fmi3Boolean *discreteStatesNeedUpdate,
    fmi3Boolean *terminateSimulation,
    fmi3Boolean *nominalsOfContinuousStatesChanged,
    fmi3Boolean *valuesOfContinuousStatesChanged,
    fmi3Boolean *nextEventTimeDefined, fmi3Float64 *nextEventTime) {
  (void)discreteStatesNeedUpdate, (void)terminateSimulation;
  (void)nominalsOfContinuousStatesChanged, (void)valuesOfContinuousStatesChanged;
  (void)nextEventTimeDefined, (void)nextEventTime;
  return UNSUPPORTED(fmi3UpdateDiscreteStates);
}

fmi3Status fmi3EnterContinuousTimeMode(fmi3Instance instance) {
  return UNSUPPORTED(fmi3EnterContinuousTimeMode);
}

fmi3Status fmi3CompletedIntegratorStep(fmi3Instance instance,
                                       fmi3Boolean noSetFMUStatePriorToCurrentPoint,
                                       fmi3Boolean *enterEventMode,
                                       fmi3Boolean *terminateSimulation) {
  (void)noSetFMUStatePriorToCurrentPoint, (void)enterEventMode;
  (void)terminateSimulation;
  return UNSUPPORTED(fmi3CompletedIntegratorStep);
}

fmi3Status fmi3SetTime(fmi3Instance instance, fmi3Float64 time) {
  (void)time;
  return UNSUPPORTED(fmi3SetTime);
}

fmi3Status fmi3SetContinuousStates(fmi3Instance instance,
                                   const fmi3Float64 continuousStates[],
                                   size_t nContinuousStates) {
  (void)continuousStates, (void)nContinuousStates;
  return UNSUPPORTED(fmi3SetContinuousStates);
}

fmi3Status fmi3GetContinuousStateDerivatives(fmi3Instance instance,
                                             fmi3Float64 derivatives[],
                                             size_t nContinuousStates) {
  (void)derivatives, (void)nContinuousStates;
  return UNSUPPORTED(fmi3GetContinuousStateDerivatives);
}

fmi3Status fmi3GetEventIndicators(fmi3Instance instance,
                                  fmi3Float64 eventIndicators[],
                                  size_t nEventIndicators) {
  (void)eventIndicators, (void)nEventIndicators;
  return UNSUPPORTED(fmi3GetEventIndicators);
}

fmi3Status fmi3GetContinuousStates(fmi3Instance instance,
                                   fmi3Float64 continuousStates[],
                                   size_t nContinuousStates) {
  (void)continuousStates, (void)nContinuousStates;
  return UNSUPPORTED(fmi3GetContinuousStates);
}

fmi3Status fmi3GetNominalsOfContinuousStates(fmi3Instance instance,
                                             fmi3Float64 nominals[],
                                             size_t nContinuousStates) {
  (void)nominals, (void)nContinuousStates;
  return UNSUPPORTED(fmi3GetNominalsOfContinuousStates);
}

fmi3Status fmi3GetNumberOfEventIndicators(fmi3Instance instance,
                                          size_t *nEventIndicators) {
  (void)nEventIndicators;
  return UNSUPPORTED(fmi3GetNumberOfEventIndicators);
}

fmi3Status fmi3GetNumberOfContinuousStates(fmi3Instance instance,
                                           size_t *nContinuousStates) {
  (void)nContinuousStates;
  return UNSUPPORTED(fmi3GetNumberOfContinuousStates);
}

fmi3Status fmi3GetOutputDerivatives(fmi3Instance instance,
                                    const fmi3ValueReference valueReferences[],
                                    size_t nValueReferences,
                                    const fmi3Int32 orders[],
                                    fmi3Float64 values[], size_t nValues) {
  (void)valueReferences, (void)nValueReferences, (void)orders;
  (void)values, (void)nValues;
  return UNSUPPORTED(fmi3GetOutputDerivatives);
}

fmi3Status fmi3ActivateModelPartition(fmi3Instance instance,
                                      fmi3ValueReference clockReference,
                                      fmi3Float64 activationTime) {
  (void)clockReference, (void)activationTime;
  return UNSUPPORTED(fmi3ActivateModelPartition);
}
