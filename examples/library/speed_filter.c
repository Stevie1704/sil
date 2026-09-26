/* speed_filter — see speed_filter.h.
 *
 * Build it as a plain shared library with the adopter's own toolchain:
 *
 *     cc -shared -fPIC -O2 -o speed_filter.so speed_filter.c
 *
 * Add -DSPEED_FILTER_DEFECT for the deliberately incorrect build: it steps
 * the filter with the forward-Euler gain period/time_constant instead of
 * period/(time_constant + period), which is the kind of regression a new
 * library build can carry. The example's Test participant catches it.
 */
#include "speed_filter.h"

#include <math.h>
#include <stdio.h>

static int initialized;
static double gain;
static speed_filter_result state;

int speed_filter_init(const speed_filter_config *config) {
  if (initialized) return SPEED_FILTER_ALREADY_INITIALIZED;
  if (!(isfinite(config->period_s) && config->period_s > 0))
    return SPEED_FILTER_BAD_PERIOD;
  if (!(isfinite(config->time_constant_s) && config->time_constant_s >= 0))
    return SPEED_FILTER_BAD_TIME_CONSTANT;
  if (!isfinite(config->initial_speed_mps))
    return SPEED_FILTER_BAD_INITIAL_SPEED;
#ifdef SPEED_FILTER_DEFECT
  gain = config->time_constant_s > 0
             ? config->period_s / config->time_constant_s
             : 1.0;
#else
  gain = config->period_s / (config->time_constant_s + config->period_s);
#endif
  state.filtered_speed_mps = config->initial_speed_mps;
  state.cycles = 0;
  initialized = 1;
  printf("speed_filter: init period=%g s time_constant=%g s\n",
         config->period_s,
         config->time_constant_s);
  fflush(stdout);
  return SPEED_FILTER_OK;
}

int speed_filter_step(double speed_mps) {
  if (!initialized) return SPEED_FILTER_NOT_INITIALIZED;
  if (!isfinite(speed_mps)) return SPEED_FILTER_BAD_INPUT;
  state.filtered_speed_mps += gain * (speed_mps - state.filtered_speed_mps);
  state.cycles += 1;
  printf("speed_filter: cycle %u\n", (unsigned)state.cycles);
  fflush(stdout);
  return SPEED_FILTER_OK;
}

void speed_filter_output(speed_filter_result *result) { *result = state; }

void speed_filter_terminate(void) {
  printf("speed_filter: terminate after %u cycles\n", (unsigned)state.cycles);
  fflush(stdout);
  initialized = 0;
}
