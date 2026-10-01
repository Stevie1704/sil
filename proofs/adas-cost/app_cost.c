/* The ADAS reference application's own computation, timed without SiL.
 *
 * measure.py fills one adas_cost_activation per activation of the declared
 * workload from the exact inputs the Runs replay, and calls adas_cost_run.
 * It initializes a fresh instance, then times only the application's calls:
 * every receive and one advance per activation, as sil_adapter.c makes
 * them. No Message conversion, routing, Step protocol or Recording is inside
 * the timed interval. The outputs are kept, so measure.py can require that
 * this computation is the one the Runs published.
 */
#define _POSIX_C_SOURCE 199309L

#include <stddef.h>
#include <stdint.h>
#include <time.h>

#include "adas_reference.h"

typedef struct adas_cost_activation {
  uint64_t t_ns;
  uint32_t has_radar;
  uint32_t has_camera;
  uint32_t has_ego;
  adas_ref_object_list radar;
  adas_ref_object_list camera;
  adas_ref_ego ego;
} adas_cost_activation;

static uint64_t now_ns(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

/* Runs n activations on a fresh instance. Returns ADAS_REF_OK and the
 * elapsed monotonic time of the calls in *elapsed_ns, or the first rejected
 * call's status with its cause in *fault. */
adas_ref_status adas_cost_run(const adas_ref_config *config,
                              const adas_cost_activation *activations,
                              size_t n, adas_ref_output *outputs,
                              uint64_t *elapsed_ns, adas_ref_fault *fault) {
  adas_ref_instance instance;
  adas_ref_status status = adas_ref_init(&instance, config, fault);
  if (status != ADAS_REF_OK) return status;
  uint64_t start = now_ns();
  for (size_t i = 0; i < n; i++) {
    const adas_cost_activation *a = &activations[i];
    if (a->has_radar &&
        (status = adas_ref_receive_radar(&instance, a->t_ns, &a->radar,
                                         fault)) != ADAS_REF_OK)
      return status;
    if (a->has_camera &&
        (status = adas_ref_receive_camera(&instance, a->t_ns, &a->camera,
                                          fault)) != ADAS_REF_OK)
      return status;
    if (a->has_ego &&
        (status = adas_ref_receive_ego(&instance, a->t_ns, &a->ego, fault)) !=
            ADAS_REF_OK)
      return status;
    if ((status = adas_ref_advance(&instance, a->t_ns, &outputs[i], fault)) !=
        ADAS_REF_OK)
      return status;
  }
  *elapsed_ns = now_ns() - start;
  return ADAS_REF_OK;
}
