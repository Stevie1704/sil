/* speed_filter — the example library of examples/library/.
 *
 * It stands in for an adopter's existing library: its own C interface, its
 * own lifecycle, no SiL header and no sil_participant_init. A first-order
 * low-pass filter over a speed signal, stepped at a fixed period.
 *
 * Like much existing code, it keeps its state in globals and writes progress
 * to stdout. Both are fine in a Process participant: each process has its own
 * globals, and the adapter moves stdout off the Step protocol's descriptor.
 */
#ifndef SPEED_FILTER_H
#define SPEED_FILTER_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define SPEED_FILTER_OK 0
#define SPEED_FILTER_BAD_PERIOD (-1)
#define SPEED_FILTER_BAD_TIME_CONSTANT (-2)
#define SPEED_FILTER_BAD_INITIAL_SPEED (-3)
#define SPEED_FILTER_ALREADY_INITIALIZED (-4)
#define SPEED_FILTER_NOT_INITIALIZED (-5)
#define SPEED_FILTER_BAD_INPUT (-6)

typedef struct speed_filter_config {
  double period_s;          /* > 0: the fixed cycle the filter is stepped at */
  double time_constant_s;   /* >= 0: 0 passes the input through */
  double initial_speed_mps; /* the filter state before the first cycle */
} speed_filter_config;

typedef struct speed_filter_result {
  double filtered_speed_mps;
  uint32_t cycles; /* cycles since speed_filter_init */
} speed_filter_result;

/* Starts one lifecycle. Returns SPEED_FILTER_OK or a negative error code;
 * a second init without speed_filter_terminate is refused. */
int speed_filter_init(const speed_filter_config *config);

/* Runs one cycle of period_s with the current input speed. */
int speed_filter_step(double speed_mps);

/* The result of the latest cycle, or the initial state before the first. */
void speed_filter_output(speed_filter_result *result);

/* Ends the lifecycle; a later speed_filter_init starts from scratch. */
void speed_filter_terminate(void);

#ifdef __cplusplus
}
#endif

#endif /* SPEED_FILTER_H */
