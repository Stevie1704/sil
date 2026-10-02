/* Synthetic interface types for sil-schema-import (issue #253).
 *
 * No project struct: each member exists to exercise one flattening rule.
 * The tests compile dwarf_layout.c, which defines one object of each type so
 * the compiler keeps the types in DWARF, and import from that object.
 * Keep each type below 127 bytes: the tests fill it with the distinct bytes
 * 1, 2, 3, ..., and then no float or double is a NaN.
 */
#ifndef DWARF_LAYOUT_H
#define DWARF_LAYOUT_H

#include <stdbool.h>
#include <stdint.h>

typedef double length_m;   /* typedef chain: odometer -> distance -> length_m */
typedef length_m distance;
typedef uint16_t object_id;

enum gear { GEAR_PARK = 0, GEAR_DRIVE = 3, GEAR_REVERSE = 4 };
enum offset_class { OFFSET_LEFT = -1, OFFSET_CENTER = 0, OFFSET_RIGHT = 1 };

struct pose {
  float x;
  float y;
  double yaw;
};

struct ego_state {
  struct pose pose;  /* third nesting level: AdasInput.ego.pose.x */
  distance odometer;
  bool valid;        /* followed by 7 bytes of trailing padding */
};

struct tracked_object {
  object_id id;
  char cls;          /* plain char: its signedness comes from DWARF */
  signed char lateral;
  unsigned char lane;
  float range;       /* after 3 bytes of inner padding */
};

typedef struct {
  uint8_t seq;                      /* followed by 7 bytes of padding */
  struct ego_state ego;
  float grid[2][3];                 /* two-dimensional primitive array */
  struct tracked_object objects[3]; /* array of structs, unrolled */
  enum gear gear;
  bool brake; /* _Bool in DWARF; followed by trailing padding */
} AdasInput;

struct AdasOutput {
  int16_t accel_cmd;  /* followed by 2 bytes of padding */
  uint32_t status;
  enum offset_class offset;
  int64_t counters[2];
};

#endif /* DWARF_LAYOUT_H */
