/* SiL virtual clock region — the fixed-layout time region shared between the
 * kernel (writer) and the clock shim / any preloaded process (reader).
 *
 * The region holds one frozen virtual time `t`, a realtime `epoch` (both in
 * nanoseconds) and the participant's sleep policy. The shim maps this region
 * and serves every clock read from it, so consecutive reads within one step
 * return the same value. This header is the only contract between writer and
 * reader; it knows nothing about the step protocol, manifest, or kernel
 * internals.
 */
#ifndef SIL_CLOCK_REGION_H
#define SIL_CLOCK_REGION_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Environment variable naming the memory-mapped region file to map at load. */
#define SIL_CLOCK_REGION_ENV "SIL_CLOCK_REGION"

/* What the sleep family does while virtual time is frozen (issue #52).
 *
 * Virtual time cannot advance inside a step: the kernel writes `t`, sends the
 * step line, and waits for the response, so a sleep that blocked until `t`
 * moved would deadlock against the kernel that would have to move it. Neither
 * policy can therefore actually sleep; they differ in what they tell the
 * caller. */
typedef enum sil_sleep_policy {
    /* Return success as if the full duration had elapsed. The recorded
     * pre-#52 behavior, and what an absent Manifest `sleep` field selects. */
    SIL_SLEEP_IMMEDIATE = 0,
    /* Fail with ENOSYS so a retry loop ends instead of spinning the CPU for
     * the rest of the step. The Python builder's default. */
    SIL_SLEEP_REJECT = 1
} sil_sleep_policy;

/* Fixed POD layout. Little-endian, no padding between the 8-byte fields,
 * matching the project's fixed-layout message convention. `sleep_policy` holds
 * a sil_sleep_policy value; it is written once at setup, never per step. An
 * unrecognized value reads as SIL_SLEEP_IMMEDIATE, so a reader can never fail
 * closed on a region a newer writer extended. */
typedef struct sil_clock_region {
    uint64_t t;             /* virtual time since run start, ns (monotonic) */
    uint64_t epoch;         /* realtime epoch, ns since 1970 (realtime IDs) */
    uint64_t sleep_policy;  /* sil_sleep_policy */
} sil_clock_region;

#ifdef __cplusplus
}
#endif

#endif /* SIL_CLOCK_REGION_H */
