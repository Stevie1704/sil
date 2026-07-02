/* SiL native participant contract — stable C ABI.
 *
 * A native participant is a shared library exporting sil_participant_init.
 * The kernel passes a function table; the participant registers periodic
 * tasks and channel subscriptions during init. All callbacks run on the
 * kernel thread, strictly sequentially, under stepped virtual time.
 */
#ifndef SIL_PARTICIPANT_H
#define SIL_PARTICIPANT_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define SIL_ABI_VERSION 1

#define SIL_OK 0
#define SIL_ERR (-1)

typedef void (*sil_task_fn)(void *user, uint64_t now_ns);

typedef struct sil_api_v1 {
  uint32_t abi_version; /* SIL_ABI_VERSION */
  void *ctx;            /* kernel context; pass to every call below */

  /* Registration — valid only during sil_participant_init. */
  int (*register_task)(void *ctx, const char *name, uint64_t period_ns,
                       uint64_t offset_ns, int32_t priority, sil_task_fn fn,
                       void *user);
  int (*subscribe)(void *ctx, const char *channel);

  /* Data plane — valid only inside a task callback. */
  int (*publish)(void *ctx, const char *channel, const void *data, size_t len);
  /* Takes the next visible message on a subscribed channel.
   * Returns 1 and sets data/len (kernel-owned, valid until the next take
   * or end of the task callback), 0 if none visible, SIL_ERR on error. */
  int (*take)(void *ctx, const char *channel, const void **data, size_t *len);

  uint64_t (*now_ns)(void *ctx);
  /* Abort the whole run with a diagnostic; the runner exits non-zero. */
  void (*fail)(void *ctx, const char *reason);
} sil_api_v1;

/* Entry point exported by the participant library.
 * config_json is the participant's "config" object from the manifest.
 * Return SIL_OK, or SIL_ERR to abort startup. */
int sil_participant_init(const sil_api_v1 *api, const char *name,
                         const char *config_json);

typedef int (*sil_participant_init_fn)(const sil_api_v1 *, const char *,
                                       const char *);

#ifdef __cplusplus
}
#endif

#endif /* SIL_PARTICIPANT_H */
