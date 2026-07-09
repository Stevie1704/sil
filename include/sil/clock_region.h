/* SiL virtual clock region — the fixed-layout time region shared between the
 * kernel (writer) and the clock shim / any preloaded process (reader).
 *
 * The region holds one frozen virtual time `t` and a realtime `epoch`, both in
 * nanoseconds. The shim maps this region and serves every clock read from it,
 * so consecutive reads within one step return the same value. This header is
 * the only contract between writer and reader; it knows nothing about the step
 * protocol, manifest, or kernel internals.
 */
#ifndef SIL_CLOCK_REGION_H
#define SIL_CLOCK_REGION_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Environment variable naming the memory-mapped region file to map at load. */
#define SIL_CLOCK_REGION_ENV "SIL_CLOCK_REGION"

/* Fixed POD layout. Little-endian nanoseconds, no padding between the two
 * 8-byte fields, matching the project's fixed-layout message convention. */
typedef struct sil_clock_region {
    uint64_t t;      /* virtual time since run start, ns (monotonic) */
    uint64_t epoch;  /* realtime epoch, ns since 1970 (added for realtime IDs) */
} sil_clock_region;

#ifdef __cplusplus
}
#endif

#endif /* SIL_CLOCK_REGION_H */
