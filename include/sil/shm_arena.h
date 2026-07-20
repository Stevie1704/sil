/* SiL shared-memory channel arena — the fixed-layout region a single channel's
 * payload crosses the kernel↔process boundary through when the channel declares
 * transport "shm". One arena per (participant, channel) direction; the kernel
 * and the process participant map the same file MAP_SHARED.
 *
 * The step protocol is fully sequential (request/response, no concurrency), so
 * a single-slot arena is sufficient: the writer stamps `len` and the payload
 * before signalling over the pipe, and the reader consumes it before the next
 * step line. `seq` monotonically counts writes, so a reader can assert it saw a
 * fresh payload rather than a stale one. This header knows nothing about the
 * step protocol, manifest, or kernel internals — it is the only contract.
 */
#ifndef SIL_SHM_ARENA_H
#define SIL_SHM_ARENA_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Fixed POD header, immediately followed by `capacity` payload bytes in the
 * same mapping. Little-endian, no padding — matching the project's fixed-layout
 * message convention. */
typedef struct sil_shm_arena {
    uint64_t seq;       /* incremented on every write (fresh-payload marker)  */
    uint64_t len;       /* valid payload length in bytes (<= capacity)        */
    /* uint8_t payload[capacity] follows immediately */
} sil_shm_arena;

#ifdef __cplusplus
}
#endif

#endif /* SIL_SHM_ARENA_H */
