/* SiL channel arena — the fixed-layout shared-memory region a single channel's
 * payloads cross the kernel↔process boundary through when the channel declares
 * transport "shm". The kernel and process participant map the same file
 * MAP_SHARED.
 *
 * The step protocol is fully sequential (request/response, no concurrency), so
 * no slot needs locking: the writer stamps `len` and the payload before
 * signalling over the pipe, and the reader consumes every slot before the
 * next response boundary. Each slot is one `sil_arena` header followed by one
 * schema-sized payload; repeating that original single-slot layout keeps slot
 * zero compatible with protocol-1 participants. `seq` monotonically counts
 * writes across the Arena, so the header in each slot proves that slot was
 * written for the Message naming it. This header knows nothing about the Step
 * protocol, Manifest, or kernel internals — it is the only binary contract.
 */
#ifndef SIL_ARENA_H
#define SIL_ARENA_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Fixed POD header, immediately followed by `capacity` payload bytes for this
 * slot. Multi-slot Arenas repeat this header+payload stride. Little-endian, no
 * padding — matching the project's fixed-layout message convention. */
typedef struct sil_arena {
    uint64_t seq;       /* incremented on every write (fresh-payload marker)  */
    uint64_t len;       /* valid payload length in bytes (<= capacity)        */
    /* uint8_t payload[capacity] follows immediately */
} sil_arena;

#ifdef __cplusplus
}
#endif

#endif /* SIL_ARENA_H */
