/* FMU of the profiling proof (issue #314): the EgoMotion example FMU with a
 * hot function and an allocation site in each fmi3DoStep.
 *
 * It reuses examples/fmu-replay/ego_motion.c unchanged: that file's
 * fmi3DoStep gets another name at include time, and the fmi3DoStep below
 * calls it after the hot work. The archive keeps the example's
 * modelDescription.xml, so the FMI Importer extracts and loads it from the
 * Run working directory like any FMU. The kernel removes that directory at
 * Run end.
 */
#include "silproof.h"

#define fmi3DoStep silproof_ego_motion_do_step
#include "ego_motion.c"
#undef fmi3DoStep

#define SILPROOF_FMU_ITERATIONS 3000000ULL
#define SILPROOF_FMU_ALLOCS 4
#define SILPROOF_FMU_BLOCK 4096

static uint64_t g_fmu_step;
static volatile uint64_t g_fmu_sink;

SILPROOF_HOT static uint64_t silproof_fmu_spin(uint64_t seed,
                                               uint64_t iterations) {
  SILPROOF_SPIN_BODY(seed, iterations, 0x6A09ULL, 0xFu);
}

SILPROOF_HOT static void silproof_fmu_alloc_site(void) {
  for (int i = 0; i < SILPROOF_FMU_ALLOCS; i++) {
    void *block = malloc(SILPROOF_FMU_BLOCK);
    if (!block) return;
    silproof_touch(block, SILPROOF_FMU_BLOCK);
    free(block);
  }
}

int fmi3DoStep(void *instance, double communication_point, double step_size,
               bool no_set_fmu_state_prior_to_current_point,
               bool *event_handling_needed, bool *terminate_simulation,
               bool *early_return, double *last_successful_time) {
  uint64_t step = g_fmu_step++;
  uint64_t begin = silproof_host_ns();
  g_fmu_sink = silproof_fmu_spin(step, SILPROOF_FMU_ITERATIONS);
  uint64_t end = silproof_host_ns();
  silproof_interval("fmu", step, "silproof_fmu_spin", begin, end);
  silproof_fmu_alloc_site();
  return silproof_ego_motion_do_step(
      instance, communication_point, step_size,
      no_set_fmu_state_prior_to_current_point, event_handling_needed,
      terminate_simulation, early_return, last_successful_time);
}
