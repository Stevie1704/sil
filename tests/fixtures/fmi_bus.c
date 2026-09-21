// A stand-in for the acceptance fixture's CAN bus simulation FMU: two
// transceiver terminals, a triggered input Clock per terminal that hands it a
// frame, and a countdown input Clock per terminal that states when the bus has
// finished transmitting one.
//
// It exists because the fixture's own FMU is built by
// `proofs/fmi-ls-bus/run-proof.sh` inside a pinned container and is not
// vendored here. What this file reproduces is the behavior upstream's
// `can-bus-simulation/src/App.c` states, at the revision
// `proofs/fmi-ls-bus/README.md` pins:
//
//   - instantiating without `eventModeUsed` is refused, in the same words;
//   - a frame handed to a terminal is queued, and a configuration operation
//     sets that terminal's baud rate and arbitration-lost behavior; bus
//     communication is enabled only once both terminals agree on one baud rate;
//   - transmission is scheduled through the countdown Clocks: their interval is
//     `(44 + dataLength) / baudRate` seconds, stated as the exact fraction it
//     is, where 44 is the constant length of a CAN frame in bits;
//   - both terminals' countdown Clocks state the same interval, so they are
//     activated in the same instant; a Clock activated on its own is refused,
//     as upstream refuses it;
//   - when they are activated, the queued frame of lowest CAN ID wins
//     arbitration: its originator is handed a `Confirm` operation and every
//     other terminal the frame itself. The next frame is then scheduled, which
//     is what makes two frames offered at one instant leave the bus one
//     transmission time apart;
//   - a clocked input may be written only before the discrete states of the
//     instant have been evaluated, and `fmi3GetBinary` evaluates them, which is
//     what decides the order an importer has to drive this FMU in.
//
// Two upstream behaviors are deliberately not reproduced, because nothing in
// the acceptance fixture configures them: the `DiscardAndNotify`
// arbitration-lost behavior, and the `BusError` operation upstream draws from
// `rand()`. Setting `BusErrorProbability` above zero is refused here rather
// than modelled, so no test can come to depend on a nondeterministic path.
//
// Every variant is this source built once more, with the behavior under test
// chosen by the preprocessor. See add_bus_fmu in CMakeLists.txt.

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define NODE_COUNT 2
#define MAX_SIZE 2048
#define QUEUE_SIZE 8
#define MAX_DATA 64

// The constant length of a CAN frame in bits, beside the variable length of
// its data. Upstream's own comment for the same number.
#define FRAME_BITS 44

// The value references of the fixture's bus FMU
// (proofs/fmi-ls-bus/evidence/profile.json), by terminal.
static const uint32_t RX_DATA[NODE_COUNT] = {0, 4};
static const uint32_t TX_DATA[NODE_COUNT] = {1, 5};
static const uint32_t RX_CLOCK[NODE_COUNT] = {2, 6};
static const uint32_t TX_CLOCK[NODE_COUNT] = {3, 7};
#define BUS_ERROR_PROBABILITY 255
#define SIMULATION_TIME 1024

// The CAN operations this FMU reads and writes, and the configuration
// parameters it understands, as fmi3LsBusCan.h defines them.
#define OP_CAN_TRANSMIT 0x0010u
#define OP_CONFIRM 0x0020u
#define OP_CONFIGURATION 0x0040u
#define PARAM_CAN_BAUDRATE 1
#define PARAM_ARBITRATION_LOST_BEHAVIOR 4

// fmi3IntervalQualifier.
#define INTERVAL_NOT_YET_KNOWN 0
#define INTERVAL_UNCHANGED 1
#define INTERVAL_CHANGED 2

// State a Clock activation of one terminal is only legal in. Upstream asserts
// the same transitions, and they are what catch an importer that enters Event
// Mode twice or writes a clocked input after the instant was evaluated.
typedef enum {
  STATE_INSTANTIATED,
  STATE_INITIALIZATION,
  STATE_EVENT,
  STATE_STEP,
  STATE_TERMINATED
} State;

// State an interval the bus cannot be activated at: a fraction that is no
// whole number of nanoseconds, so the instant the FMU asks for falls between
// two of the kernel's.
#ifndef FMI_BUS_RAGGED_INTERVAL
#define FMI_BUS_RAGGED_INTERVAL 0
#endif
// State an interval of zero, so the instant asked for is the one the interval
// was stated in — an activation that is already behind the group.
#ifndef FMI_BUS_INSTANT_INTERVAL
#define FMI_BUS_INSTANT_INTERVAL 0
#endif

typedef struct {
  uint32_t id;
  uint8_t ide;
  uint8_t rtr;
  uint16_t data_length;
  unsigned char data[MAX_DATA];
} Frame;

typedef struct {
  // What the terminal was handed, until the instant's discrete states read it.
  unsigned char rx[MAX_SIZE];
  size_t rx_length;
  bool rx_clock;
  // What the bus hands the terminal, gated by its countdown Clock.
  unsigned char tx[MAX_SIZE];
  size_t tx_length;
  bool tx_clock;
  int tx_qualifier;
  // The frames this terminal offered and the bus has not transmitted yet.
  Frame queue[QUEUE_SIZE];
  size_t queued;
  uint32_t baud_rate;
  uint8_t arbitration_lost_behavior;
} NodeData;

typedef struct {
  NodeData nodes[NODE_COUNT];
  uint32_t bus_baud_rate;
  double bus_error_probability;
  uint64_t tx_counter;
  uint64_t tx_resolution;
  bool tx_scheduled;
  bool evaluated;
  double time;
  State state;
} Bus;

typedef void (*fmi3_log_message)(void *, int, const char *, const char *);

static void put_u16(unsigned char *at, uint16_t value) {
  memcpy(at, &value, sizeof value);
}

static void put_u32(unsigned char *at, uint32_t value) {
  memcpy(at, &value, sizeof value);
}

static uint32_t take_u32(const unsigned char *at) {
  uint32_t value;
  memcpy(&value, at, sizeof value);
  return value;
}

// Start one operation in a terminal's transmit buffer and answer where its
// own fields go, or NULL when the buffer cannot hold it.
static unsigned char *begin_operation(NodeData *node, uint32_t op_code,
                                      uint32_t length) {
  if (node->tx_length + length > MAX_SIZE) {
    return NULL;
  }
  unsigned char *at = node->tx + node->tx_length;
  put_u32(at, op_code);
  put_u32(at + 4, length);
  node->tx_length += length;
  return at + 8;
}

static void put_transmit(NodeData *node, const Frame *frame) {
  unsigned char *body =
      begin_operation(node, OP_CAN_TRANSMIT, 16 + frame->data_length);
  if (body == NULL) {
    return;
  }
  put_u32(body, frame->id);
  body[4] = frame->ide;
  body[5] = frame->rtr;
  put_u16(body + 6, frame->data_length);
  memcpy(body + 8, frame->data, frame->data_length);
}

static void put_confirm(NodeData *node, uint32_t id) {
  unsigned char *body = begin_operation(node, OP_CONFIRM, 12);
  if (body != NULL) {
    put_u32(body, id);
  }
}

// Read every operation a terminal was handed, and clear what it was handed.
static void receive(NodeData *node) {
  size_t offset = 0;
  while (offset + 8 <= node->rx_length) {
    uint32_t op_code = take_u32(node->rx + offset);
    uint32_t length = take_u32(node->rx + offset + 4);
    if (length < 8 || offset + length > node->rx_length) {
      break;
    }
    const unsigned char *body = node->rx + offset + 8;
    if (op_code == OP_CAN_TRANSMIT && length >= 16) {
      if (node->queued < QUEUE_SIZE) {
        Frame *frame = &node->queue[node->queued++];
        frame->id = take_u32(body);
        frame->ide = body[4];
        frame->rtr = body[5];
        memcpy(&frame->data_length, body + 6, sizeof frame->data_length);
        if (frame->data_length > MAX_DATA) {
          frame->data_length = MAX_DATA;
        }
        memcpy(frame->data, body + 8, frame->data_length);
      }
    } else if (op_code == OP_CONFIGURATION && length >= 13 &&
               body[0] == PARAM_CAN_BAUDRATE) {
      node->baud_rate = take_u32(body + 1);
    } else if (op_code == OP_CONFIGURATION && length >= 10 &&
               body[0] == PARAM_ARBITRATION_LOST_BEHAVIOR) {
      node->arbitration_lost_behavior = body[1];
    }
    offset += length;
  }
  node->rx_clock = false;
  node->rx_length = 0;
}

// The queued frame that wins arbitration: the lowest CAN ID, and the first
// terminal offering it when two offer the same one.
static bool next_frame(const Bus *bus, size_t *originator) {
  bool found = false;
  for (size_t i = 0; i < NODE_COUNT; i++) {
    if (bus->nodes[i].queued == 0) {
      continue;
    }
    if (!found || bus->nodes[i].queue[0].id < bus->nodes[*originator].queue[0].id) {
      *originator = i;
      found = true;
    }
  }
  return found;
}

static void pop_frame(NodeData *node) {
  for (size_t i = 1; i < node->queued; i++) {
    node->queue[i - 1] = node->queue[i];
  }
  node->queued--;
}

static void schedule(Bus *bus) {
  size_t originator = 0;
  if (!next_frame(bus, &originator)) {
    return;
  }
  for (size_t i = 0; i < NODE_COUNT; i++) {
    bus->nodes[i].tx_qualifier = INTERVAL_CHANGED;
  }
#if FMI_BUS_RAGGED_INTERVAL
  bus->tx_counter = 1;
  bus->tx_resolution = 3;
#elif FMI_BUS_INSTANT_INTERVAL
  bus->tx_counter = 0;
  bus->tx_resolution = 1;
#else
  bus->tx_counter = FRAME_BITS + bus->nodes[originator].queue[0].data_length;
  bus->tx_resolution = bus->bus_baud_rate;
#endif
  bus->tx_scheduled = true;
}

// Everything one instant of this FMU decides. `fmi3GetBinary` and
// `fmi3UpdateDiscreteStates` both reach it, and only the first of them runs it.
static void evaluate(Bus *bus) {
  if (bus->evaluated) {
    return;
  }
  for (size_t i = 0; i < NODE_COUNT; i++) {
    if (bus->nodes[i].rx_clock) {
      receive(&bus->nodes[i]);
    }
  }
  // Bus communication needs one baud rate both terminals agreed on.
  bus->bus_baud_rate = 0;
  if (bus->nodes[0].baud_rate > 0 &&
      bus->nodes[0].baud_rate == bus->nodes[1].baud_rate) {
    bus->bus_baud_rate = bus->nodes[0].baud_rate;
  }
  if (bus->nodes[0].tx_clock && bus->nodes[1].tx_clock) {
    size_t originator = 0;
    if (next_frame(bus, &originator)) {
      Frame frame = bus->nodes[originator].queue[0];
      pop_frame(&bus->nodes[originator]);
      for (size_t i = 0; i < NODE_COUNT; i++) {
        if (i == originator) {
          put_confirm(&bus->nodes[i], frame.id);
        } else {
          put_transmit(&bus->nodes[i], &frame);
        }
      }
    }
    for (size_t i = 0; i < NODE_COUNT; i++) {
      bus->nodes[i].tx_qualifier = INTERVAL_NOT_YET_KNOWN;
    }
    bus->tx_scheduled = false;
  }
  // Only when no transmission is pending, so one already asked for is not
  // postponed by accident.
  if (!bus->tx_scheduled) {
    schedule(bus);
  }
  bus->evaluated = true;
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
  if (!event_mode_used) {
    // What the fixture's bus FMU does, in the same words its log uses.
    if (log_message) {
      log_message(environment, 3, "logStatusError",
                  "Event mode is must be supported by the importer to use "
                  "this FMU.");
    }
    return NULL;
  }
  Bus *bus = calloc(1, sizeof(Bus));
  if (bus == NULL) {
    return NULL;
  }
  for (size_t i = 0; i < NODE_COUNT; i++) {
    bus->nodes[i].tx_qualifier = INTERVAL_NOT_YET_KNOWN;
  }
  return bus;
}

int fmi3EnterInitializationMode(void *instance, bool tolerance_defined,
                                double tolerance, double start_time,
                                bool stop_time_defined, double stop_time) {
  Bus *bus = instance;
  (void)tolerance_defined;
  (void)tolerance;
  (void)stop_time_defined;
  (void)stop_time;
  if (bus->state != STATE_INSTANTIATED) {
    return 3;
  }
  bus->time = start_time;
  bus->state = STATE_INITIALIZATION;
  return 0;
}

int fmi3ExitInitializationMode(void *instance) {
  Bus *bus = instance;
  if (bus->state != STATE_INITIALIZATION) {
    return 3;
  }
  // With Event Mode in use, initialization ends in Event Mode.
  bus->state = STATE_EVENT;
  return 0;
}

int fmi3EnterEventMode(void *instance) {
  Bus *bus = instance;
  if (bus->state != STATE_STEP) {
    return 3;
  }
  bus->state = STATE_EVENT;
  return 0;
}

int fmi3EnterStepMode(void *instance) {
  Bus *bus = instance;
  if (bus->state != STATE_EVENT) {
    return 3;
  }
  bus->state = STATE_STEP;
  return 0;
}

int fmi3DoStep(void *instance, double communication_point, double step_size,
               bool no_set_fmu_state_prior_to_current_point,
               bool *event_handling_needed, bool *terminate_simulation,
               bool *early_return, double *last_successful_time) {
  Bus *bus = instance;
  (void)no_set_fmu_state_prior_to_current_point;
  if (bus->state != STATE_STEP) {
    return 3;
  }
  bus->time = communication_point + step_size;
  // The bus decides nothing in a Step: every instant it acts in is one a Clock
  // brought it into, so it never asks for an event of its own.
  *event_handling_needed = false;
  *terminate_simulation = false;
  *early_return = false;
  *last_successful_time = bus->time;
  return 0;
}

int fmi3GetClock(void *instance, const uint32_t *value_references,
                 size_t n_value_references, bool *values) {
  (void)instance;
  (void)value_references;
  (void)n_value_references;
  (void)values;
  // This FMU declares no output Clock, so there is none to read.
  return 3;
}

int fmi3SetClock(void *instance, const uint32_t *value_references,
                 size_t n_value_references, const bool *values) {
  Bus *bus = instance;
  if (bus->state != STATE_EVENT || bus->evaluated) {
    // A clocked input is written before the instant's discrete states are
    // evaluated; after that the answer would no longer depend on it.
    return 3;
  }
  for (size_t i = 0; i < n_value_references; i++) {
    bool matched = false;
    for (size_t node = 0; node < NODE_COUNT; node++) {
      if (value_references[i] == RX_CLOCK[node]) {
        bus->nodes[node].rx_clock = values[i];
        matched = true;
      } else if (value_references[i] == TX_CLOCK[node]) {
        bus->nodes[node].tx_clock = values[i];
        matched = true;
      }
    }
    if (!matched) {
      return 3;
    }
  }
  // Both terminals' countdown Clocks state the same interval, so they tick in
  // the same instant. One on its own is a transmission nobody is told about.
  if (bus->nodes[0].tx_clock != bus->nodes[1].tx_clock) {
    return 3;
  }
  return 0;
}

int fmi3GetBinary(void *instance, const uint32_t *value_references,
                  size_t n_value_references, size_t *value_sizes,
                  const unsigned char **values, size_t n_values) {
  Bus *bus = instance;
  (void)n_values;
  for (size_t i = 0; i < n_value_references; i++) {
    bool matched = false;
    for (size_t node = 0; node < NODE_COUNT; node++) {
      if (value_references[i] != TX_DATA[node]) {
        continue;
      }
      evaluate(bus);
      value_sizes[i] = bus->nodes[node].tx_length;
      values[i] = bus->nodes[node].tx;
      matched = true;
    }
    if (!matched) {
      return 3;
    }
  }
  return 0;
}

int fmi3SetBinary(void *instance, const uint32_t *value_references,
                  size_t n_value_references, const size_t *value_sizes,
                  const unsigned char **values, size_t n_values) {
  Bus *bus = instance;
  (void)n_values;
  if (bus->state != STATE_EVENT || bus->evaluated) {
    return 3;
  }
  for (size_t i = 0; i < n_value_references; i++) {
    bool matched = false;
    for (size_t node = 0; node < NODE_COUNT; node++) {
      if (value_references[i] != RX_DATA[node]) {
        continue;
      }
      if (bus->nodes[node].rx_length + value_sizes[i] > MAX_SIZE) {
        return 3;
      }
      memcpy(bus->nodes[node].rx + bus->nodes[node].rx_length, values[i],
             value_sizes[i]);
      bus->nodes[node].rx_length += value_sizes[i];
      matched = true;
    }
    if (!matched) {
      return 3;
    }
  }
  return 0;
}

int fmi3GetIntervalFraction(void *instance, const uint32_t *value_references,
                            size_t n_value_references, uint64_t *counters,
                            uint64_t *resolutions, int32_t *qualifiers) {
  Bus *bus = instance;
  for (size_t i = 0; i < n_value_references; i++) {
    bool matched = false;
    for (size_t node = 0; node < NODE_COUNT; node++) {
      if (value_references[i] != TX_CLOCK[node]) {
        continue;
      }
      qualifiers[i] = bus->nodes[node].tx_qualifier;
      if (bus->nodes[node].tx_qualifier == INTERVAL_CHANGED) {
        counters[i] = bus->tx_counter;
        resolutions[i] = bus->tx_resolution;
        // An interval is stated as changed once per change.
        bus->nodes[node].tx_qualifier = INTERVAL_UNCHANGED;
      }
      if (resolutions[i] == 0) {
        qualifiers[i] = INTERVAL_NOT_YET_KNOWN;
      }
      matched = true;
    }
    if (!matched) {
      return 3;
    }
  }
  return 0;
}

int fmi3SetFloat64(void *instance, const uint32_t *value_references,
                   size_t n_value_references, const double *values,
                   size_t n_values) {
  Bus *bus = instance;
  (void)n_values;
  for (size_t i = 0; i < n_value_references; i++) {
    if (value_references[i] != BUS_ERROR_PROBABILITY) {
      return 3;
    }
    if (values[i] != 0.0) {
      // Upstream draws a bus error from `rand()`. This stand-in models no
      // such path, so a probability above zero is refused rather than
      // silently ignored.
      return 3;
    }
    bus->bus_error_probability = values[i];
  }
  return 0;
}

int fmi3GetFloat64(void *instance, const uint32_t *value_references,
                   size_t n_value_references, double *values,
                   size_t n_values) {
  Bus *bus = instance;
  (void)n_values;
  for (size_t i = 0; i < n_value_references; i++) {
    if (value_references[i] == SIMULATION_TIME) {
      values[i] = bus->time;
    } else if (value_references[i] == BUS_ERROR_PROBABILITY) {
      values[i] = bus->bus_error_probability;
    } else {
      return 3;
    }
  }
  return 0;
}

int fmi3UpdateDiscreteStates(void *instance, bool *discrete_states_need_update,
                             bool *terminate_simulation,
                             bool *nominals_changed, bool *values_changed,
                             bool *next_event_time_defined,
                             double *next_event_time) {
  Bus *bus = instance;
  *nominals_changed = false;
  *values_changed = false;
  *discrete_states_need_update = false;
  *terminate_simulation = false;
  // Every instant this FMU acts in is one a Clock brought it into, so it names
  // no next event time of its own: the countdown Clocks are how it asks.
  *next_event_time_defined = false;
  *next_event_time = 0.0;
  if (bus->state != STATE_EVENT) {
    return 3;
  }
  evaluate(bus);
  // The activation ends with this update: the Clocks go down and the buffers
  // they gated are spent.
  for (size_t i = 0; i < NODE_COUNT; i++) {
    bus->nodes[i].tx_clock = false;
    bus->nodes[i].tx_length = 0;
  }
  bus->evaluated = false;
  return 0;
}

int fmi3Terminate(void *instance) {
  Bus *bus = instance;
  bus->state = STATE_TERMINATED;
  return 0;
}

void fmi3FreeInstance(void *instance) { free(instance); }
