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

/* Fixed POD header of one slot. The Schema-sized payload follows it
 * immediately, then trailing padding up to `shm_capacity` bytes. The kernel
 * sets `shm_capacity` in the init line to the Schema `byte_size` rounded up to
 * a multiple of 8. The slot stride is `sizeof(sil_arena) + shm_capacity`, so
 * every header is 8-byte aligned. The header is little-endian and has no
 * padding. This matches the project's fixed-layout message convention. */
typedef struct sil_arena {
    uint64_t seq;       /* incremented on every write (fresh-payload marker)  */
    uint64_t len;       /* payload length; equals the Schema byte_size        */
    /* uint8_t payload[byte_size] follows, then padding to shm_capacity */
} sil_arena;

#ifdef __cplusplus
}
#endif

#endif /* SIL_ARENA_H */
