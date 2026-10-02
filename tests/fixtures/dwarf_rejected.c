/* Types that sil-schema-import must reject (issue #253): each type holds one
 * construct that a flat Schema cannot mirror, below a nested member so the
 * diagnostic has to name the member path. */
#include <stdint.h>

struct inner_union {
  union {
    float f;
    uint32_t u;
  } value;
};

struct WithUnion {
  uint8_t seq;
  struct inner_union payload;
};

struct WithBitField {
  struct {
    uint32_t mode : 3;
    uint32_t rest : 29;
  } flags;
};

struct WithPointer {
  struct {
    const float *samples;
  } buffer;
};

struct WithFlexibleArray {
  uint32_t count;
  float samples[];
};

/* a_b.c and a.b_c both flatten to a_b_c. */
struct WithCollision {
  struct {
    float c;
  } a_b;
  struct {
    float b_c;
  } a;
};

struct WithReservedName {
  struct {
    uint8_t len_t;
  } header;
};

struct WithLongDouble {
  long double precise;
};

struct WithUnion dwarf_rejected_union;
struct WithBitField dwarf_rejected_bit_field;
struct WithPointer dwarf_rejected_pointer;
struct WithFlexibleArray dwarf_rejected_flexible;
struct WithCollision dwarf_rejected_collision;
struct WithReservedName dwarf_rejected_reserved;
struct WithLongDouble dwarf_rejected_long_double;
