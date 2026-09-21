// A stand-in for the acceptance fixture's CAN node: Event Mode, a triggered
// output Clock gating a Binary buffer, and a triggered input Clock that feeds
// one back.
//
// It exists because the fixture's own FMU is built by
// `proofs/fmi-ls-bus/run-proof.sh` inside a pinned container and is not
// vendored here. What this file reproduces is the node's *behavior* as
// `proofs/fmi-ls-bus/expected.json` states it, so the importer can be held to
// that statement without docker:
//
//   - instantiating without `eventModeUsed` is refused, as the node refuses it;
//   - initialization ends in Event Mode with the output Clock already active,
//     carrying the two Configuration operations of the expected exchange;
//   - one CanTransmit operation is appended for every 300 ms boundary a step
//     crosses, and the output Clock is raised at the end of that step;
//   - the Clock reads active exactly once per activation — reading it clears
//     it, which is what makes a duplicated readout lose an operation;
//   - an activated input Clock echoes the buffer it was handed back out on the
//     output Clock, one discrete-state update later.
//
// The operation bytes are the expected exchange's own, and the tests compare
// what the importer publishes against that same file, so a fixture that is
// rebuilt and changes takes both ends with it.
//
// Every variant is this source built once more, with the behavior under test
// chosen by the preprocessor. See add_clocked_fmu in CMakeLists.txt.

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

// The value references of the fixture's CAN node
// (proofs/fmi-ls-bus/evidence/profile.json).
#define RX_DATA 0
#define TX_DATA 1
#define RX_CLOCK 2
#define TX_CLOCK 3

// The node transmits every 300 ms, and its buffers declare maxSize 2048.
#define TRANSMIT_INTERVAL_MS 300
#define MAX_SIZE 2048

// Answer another discrete-state update forever, so an importer without a
// bound on its event iteration never leaves the event.
#ifndef FMI_CLOCK_NEVER_CONVERGES
#define FMI_CLOCK_NEVER_CONVERGES 0
#endif
// Declare a next event time, as an absolute number of milliseconds, so the
// build says which instant it asks to be stepped onto. Zero declares none.
#ifndef FMI_CLOCK_NEXT_EVENT_MS
#define FMI_CLOCK_NEXT_EVENT_MS 0
#endif
// Return from fmi3DoStep before the interval is over, which an importer that
// declared earlyReturnAllowed false never asked for.
#ifndef FMI_CLOCK_EARLY_RETURN
#define FMI_CLOCK_EARLY_RETURN 0
#endif
// Ask for the simulation to be terminated from inside the event.
#ifndef FMI_CLOCK_TERMINATE_IN_EVENT
#define FMI_CLOCK_TERMINATE_IN_EVENT 0
#endif
// Raise the output Clock in the discrete-state update that ends the event
// rather than before it, which is the activation an importer loses if it
// stops reading the Clock once the FMU says there is nothing more to update.
#ifndef FMI_CLOCK_ACTIVATE_IN_UPDATE
#define FMI_CLOCK_ACTIVATE_IN_UPDATE 0
#endif

// The expected exchange's Configuration payload: CAN baud rate 100 000, then
// arbitration-lost behavior BufferAndRetransmit.
static const unsigned char CONFIGURATION[] = {
    0x40, 0x00, 0x00, 0x00, 0x0d, 0x00, 0x00, 0x00, 0x01, 0xa0, 0x86, 0x01,
    0x00, 0x40, 0x00, 0x00, 0x00, 0x0a, 0x00, 0x00, 0x00, 0x04, 0x01};

// One CanTransmit operation: ID 0x1, no IDE, no RTR, payload 01 02 03 04.
static const unsigned char TRANSMIT[] = {0x10, 0x00, 0x00, 0x00, 0x14, 0x00,
                                         0x00, 0x00, 0x01, 0x00, 0x00, 0x00,
                                         0x00, 0x00, 0x04, 0x00, 0x01, 0x02,
                                         0x03, 0x04};

typedef struct {
  // The buffer the output Clock gates, and how much of it is the payload.
  unsigned char tx[MAX_SIZE];
  size_t tx_length;
  bool tx_clock;
  // What an activated input Clock was handed, held until the discrete-state
  // update that echoes it.
  unsigned char rx[MAX_SIZE];
  size_t rx_length;
  bool rx_pending;
  // How many transmit operations the last interval produced, when this build
  // raises the Clock from the discrete-state update instead of the Step.
  size_t tx_pending;
  double event_time;
} Node;

static Node node;

typedef void (*fmi3_log_message)(void *, int, const char *, const char *);

static long long milliseconds(double seconds) {
  // The communication points are whole milliseconds derived from the kernel's
  // integer nanoseconds, so rounding to the nearest millisecond recovers the
  // integer the importer started from rather than a truncated double.
  return (long long)(seconds * 1000.0 + 0.5);
}

static void raise_tx(const unsigned char *operation, size_t length,
                     size_t count) {
  node.tx_length = 0;
  for (size_t i = 0; i < count; i++) {
    memcpy(node.tx + node.tx_length, operation, length);
    node.tx_length += length;
  }
  node.tx_clock = count > 0;
}

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
  (void)early_return_allowed;
  (void)required_intermediate_variables;
  (void)n_required_intermediate_variables;
  (void)intermediate_update;
  (void)environment;
  if (!event_mode_used) {
    // What the fixture's node does, in the same words its log uses.
    if (log_message) {
      log_message(environment, 3, "logStatusError",
                  "Event mode is must be supported by the importer to use "
                  "this FMU.");
    }
    return NULL;
  }
  memset(&node, 0, sizeof(node));
  return &node;
}

int fmi3EnterInitializationMode(void *instance, bool tolerance_defined,
                                double tolerance, double start_time,
                                bool stop_time_defined, double stop_time) {
  (void)instance;
  (void)tolerance_defined;
  (void)tolerance;
  (void)stop_time_defined;
  (void)stop_time;
  node.event_time = start_time;
  return 0;
}

int fmi3ExitInitializationMode(void *instance) {
  (void)instance;
  // With Event Mode in use, initialization ends in Event Mode, and the node's
  // bus configuration is already waiting in it.
  raise_tx(CONFIGURATION, sizeof(CONFIGURATION), 1);
  return 0;
}

int fmi3EnterEventMode(void *instance) {
  (void)instance;
  return 0;
}

int fmi3EnterStepMode(void *instance) {
  (void)instance;
  return 0;
}

int fmi3DoStep(void *instance, double communication_point, double step_size,
               bool no_set_fmu_state_prior_to_current_point,
               bool *event_handling_needed, bool *terminate_simulation,
               bool *early_return, double *last_successful_time) {
  (void)instance;
  (void)no_set_fmu_state_prior_to_current_point;
  (void)terminate_simulation;
  long long from = milliseconds(communication_point);
  long long to = milliseconds(communication_point + step_size);
  // One operation for every transmit boundary this interval crossed, all of
  // them in the buffer the one Clock activation at its end carries.
  size_t crossed = (size_t)(to / TRANSMIT_INTERVAL_MS - from / TRANSMIT_INTERVAL_MS);
#if FMI_CLOCK_ACTIVATE_IN_UPDATE
  node.tx_pending = crossed;
#else
  raise_tx(TRANSMIT, sizeof(TRANSMIT), crossed);
#endif
  node.event_time = communication_point + step_size;
  *event_handling_needed = node.tx_clock || crossed > 0;
  *last_successful_time = communication_point + step_size;
#if FMI_CLOCK_EARLY_RETURN
  *early_return = true;
  *last_successful_time = communication_point + step_size / 2.0;
#else
  *early_return = false;
#endif
  return 0;
}

int fmi3GetClock(void *instance, const uint32_t *value_references,
                 size_t n_value_references, bool *values) {
  (void)instance;
  for (size_t i = 0; i < n_value_references; i++) {
    if (value_references[i] != TX_CLOCK) {
      return 3;
    }
    values[i] = node.tx_clock;
    // The Clock reads active exactly once per activation.
    node.tx_clock = false;
  }
  return 0;
}

int fmi3SetClock(void *instance, const uint32_t *value_references,
                 size_t n_value_references, const bool *values) {
  (void)instance;
  for (size_t i = 0; i < n_value_references; i++) {
    if (value_references[i] != RX_CLOCK) {
      return 3;
    }
    if (values[i]) {
      node.rx_pending = true;
    }
  }
  return 0;
}

int fmi3GetBinary(void *instance, const uint32_t *value_references,
                  size_t n_value_references, size_t *value_sizes,
                  const unsigned char **values, size_t n_values) {
  (void)instance;
  (void)n_values;
  for (size_t i = 0; i < n_value_references; i++) {
    if (value_references[i] != TX_DATA) {
      return 3;
    }
    value_sizes[i] = node.tx_length;
    values[i] = node.tx;
  }
  return 0;
}

int fmi3SetBinary(void *instance, const uint32_t *value_references,
                  size_t n_value_references, const size_t *value_sizes,
                  const unsigned char **values, size_t n_values) {
  (void)instance;
  (void)n_values;
  for (size_t i = 0; i < n_value_references; i++) {
    if (value_references[i] != RX_DATA || value_sizes[i] > MAX_SIZE) {
      return 3;
    }
    node.rx_length = value_sizes[i];
    memcpy(node.rx, values[i], value_sizes[i]);
  }
  return 0;
}

int fmi3UpdateDiscreteStates(void *instance, bool *discrete_states_need_update,
                             bool *terminate_simulation,
                             bool *nominals_changed, bool *values_changed,
                             bool *next_event_time_defined,
                             double *next_event_time) {
  (void)instance;
  *nominals_changed = false;
  *values_changed = false;
  *discrete_states_need_update = false;
  *terminate_simulation = false;
  *next_event_time_defined = false;
  *next_event_time = 0.0;
  if (node.rx_pending) {
    // The frame the input Clock delivered goes back out on the output Clock,
    // which the importer sees on the next iteration of this event.
    memcpy(node.tx, node.rx, node.rx_length);
    node.tx_length = node.rx_length;
    node.tx_clock = true;
    node.rx_pending = false;
    *discrete_states_need_update = true;
  }
#if FMI_CLOCK_ACTIVATE_IN_UPDATE
  if (node.tx_pending) {
    // The event ends with this update, and the Clock goes up in it: an
    // importer that read the Clock only before the update never sees it.
    raise_tx(TRANSMIT, sizeof(TRANSMIT), node.tx_pending);
    node.tx_pending = 0;
  }
#endif
#if FMI_CLOCK_NEVER_CONVERGES
  *discrete_states_need_update = true;
#endif
#if FMI_CLOCK_NEXT_EVENT_MS
  *next_event_time_defined = true;
  *next_event_time = FMI_CLOCK_NEXT_EVENT_MS / 1000.0;
#endif
#if FMI_CLOCK_TERMINATE_IN_EVENT
  *terminate_simulation = true;
#endif
  return 0;
}

int fmi3Terminate(void *instance) {
  (void)instance;
  return 0;
}

void fmi3FreeInstance(void *instance) { (void)instance; }
