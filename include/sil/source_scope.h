/* SiL source scopes: opt-in marks of logical C calls in a Native library.
 *
 *     #include <sil/source_scope.h>
 *
 *     static int confirms(const object *radar, const object *camera) {
 *       SIL_SOURCE_SCOPE(3, "confirms");
 *       if (!camera->valid) return 0;   // early return: a return event
 *       return near(radar, camera);
 *     }
 *
 * docs/proposals/native-source-scopes.md is the contract. This header is not
 * part of the Native participant ABI (sil/participant.h).
 *
 * SIL_SOURCE_SCOPE(id, name) declares one scope in the current block, once
 * per block. It begins at the declaration and ends when the block exits
 * through an ordinary C return or the end of the block. `id` is a constant
 * integer in [1, 65535]; `name` is a string literal of at most 63 bytes.
 * All sites of one ID must have the same name.
 *
 * Opt-out (the default): without SIL_SOURCE_SCOPES defined to 1, a scope is
 * a compile-time check only: no code, no data and no link dependency.
 *
 * Opt-in (SIL_SOURCE_SCOPES=1, GCC on an ELF target, qualified for Linux
 * x86-64/glibc with GCC 12): each use is one 256-byte site record in the
 * ELF section `sil_source_scopes` (the scope map) and a local guard with the
 * `cleanup` attribute. Link the library with -lsil_source_scope
 * (libsil_source_scope.so.1). The observer records scopes only in a
 * `sil profile --collector scopes` capture; elsewhere it is dormant.
 *
 * longjmp, thread cancellation and C++ exceptions through a scope skip its
 * return: the capture reports them as unbalanced and never repairs them.
 */
#ifndef SIL_SOURCE_SCOPE_H
#define SIL_SOURCE_SCOPE_H

#include <stdint.h>

#ifdef __cplusplus
#define SIL_SOURCE_SCOPE_ASSERT_(condition, message) \
  static_assert(condition, message)
#else
#define SIL_SOURCE_SCOPE_ASSERT_(condition, message) \
  _Static_assert(condition, message)
#endif

/* `"" name` accepts a string literal only. */
#define SIL_SOURCE_SCOPE_CHECK_(id, name)                                    \
  SIL_SOURCE_SCOPE_ASSERT_((id) >= 1 && (id) <= 65535,                       \
                           "a source scope ID is in [1, 65535]");            \
  SIL_SOURCE_SCOPE_ASSERT_(sizeof("" name) <= 64,                            \
                           "a source scope name has at most 63 bytes")

#if defined(SIL_SOURCE_SCOPES) && SIL_SOURCE_SCOPES == 1

#if !defined(__ELF__) || !defined(__GNUC__)
#error "SIL_SOURCE_SCOPES=1 needs GCC on an ELF target (Linux)"
#endif

#ifdef __cplusplus
extern "C" {
#endif

/* Version 1 of the site record: 4 + 2 + 2 + 4 + 64 + 180 = 256 bytes,
 * little-endian, zero-padded, no pointers. */
struct sil_source_scope_v1_site {
  char magic[4]; /* "SSC1" */
  uint16_t version;
  uint16_t id;
  uint32_t line;
  char name[64];
  char file[180];
};

typedef struct sil_source_scope_v1_guard {
  uint16_t id;
} sil_source_scope_v1_guard;

sil_source_scope_v1_guard sil_source_scope_v1_enter(uint16_t id);
void sil_source_scope_v1_return(const sil_source_scope_v1_guard *guard);

#ifdef __cplusplus
}
#endif

#define SIL_SOURCE_SCOPE(id, name)                                           \
  SIL_SOURCE_SCOPE_CHECK_(id, name);                                         \
  SIL_SOURCE_SCOPE_ASSERT_(sizeof(__FILE__) <= 180,                          \
                           "__FILE__ of a source scope has at most 179 "     \
                           "bytes");                                         \
  static const struct sil_source_scope_v1_site sil_source_scope_site_        \
      __attribute__((used, section("sil_source_scopes"))) = {               \
          {'S', 'S', 'C', '1'}, 1, (uint16_t)(id), __LINE__, name,          \
          __FILE__};                                                         \
  const sil_source_scope_v1_guard sil_source_scope_guard_                    \
      __attribute__((cleanup(sil_source_scope_v1_return))) =                 \
          sil_source_scope_v1_enter((uint16_t)(id))

#else

#define SIL_SOURCE_SCOPE(id, name) SIL_SOURCE_SCOPE_CHECK_(id, name)

#endif

#endif /* SIL_SOURCE_SCOPE_H */
