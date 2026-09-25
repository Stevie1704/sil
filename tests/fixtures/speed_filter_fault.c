/* examples/library/speed_filter.c with one fault, for the adapter's failure
 * tests (issue #184). The example library is included unchanged; only the
 * symbol a variant breaks is renamed out of the way and, for a cycle fault,
 * replaced by a wrapper.
 *
 * SPEED_FILTER_CRASH_AT_CYCLE=n  dereferences NULL in cycle n
 * SPEED_FILTER_HANG_AT_CYCLE=n   never returns from cycle n
 * SPEED_FILTER_OMIT_TERMINATE    does not export speed_filter_terminate
 */
#include <unistd.h>

#define speed_filter_step speed_filter_step_nominal
#ifdef SPEED_FILTER_OMIT_TERMINATE
#define speed_filter_terminate speed_filter_terminate_omitted
#endif
#include "speed_filter.c"
#undef speed_filter_step

int speed_filter_step(double speed_mps) {
#ifdef SPEED_FILTER_CRASH_AT_CYCLE
  if (state.cycles + 1 == SPEED_FILTER_CRASH_AT_CYCLE) *(volatile int *)0 = 0;
#endif
#ifdef SPEED_FILTER_HANG_AT_CYCLE
  if (state.cycles + 1 == SPEED_FILTER_HANG_AT_CYCLE)
    for (;;) pause();
#endif
  return speed_filter_step_nominal(speed_mps);
}
