/* Copies each fixture type into its imported SiL struct and prints what the
 * test needs to decode it through sil.schema (issue #253):
 *
 *   size <schema> <sizeof original> <sizeof SiL struct>
 *   bytes <schema> <SiL struct bytes in hex>
 *   member <schema> <field> <element index or -> <value>
 *
 * Each value is read through the original type, never through the import.
 * Floats print as exact hexadecimal; bool bytes are read with memcpy, because
 * a _Bool load of a byte other than 0 or 1 is undefined. */
#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "dwarf_layout.h"
#include "dwarf_messages.h"

#define INT(schema, field, index, expr) \
  printf("member %s %s %s %" PRId64 "\n", schema, field, index, (int64_t)(expr))
#define UINT(schema, field, index, expr) \
  printf("member %s %s %s %" PRIu64 "\n", schema, field, index, (uint64_t)(expr))
#define FLOAT(schema, field, index, expr) \
  printf("member %s %s %s %a\n", schema, field, index, (double)(expr))
#define BYTE(schema, field, index, lvalue)                       \
  do {                                                           \
    unsigned char byte_;                                         \
    memcpy(&byte_, &(lvalue), 1);                                \
    printf("member %s %s %s %u\n", schema, field, index, byte_); \
  } while (0)

static void fill(void *p, size_t n) {
  unsigned char *bytes = p;
  for (size_t i = 0; i < n; i++) bytes[i] = (unsigned char)(i + 1);
}

static void print_bytes(const char *schema, const void *p, size_t n) {
  const unsigned char *bytes = p;
  printf("bytes %s ", schema);
  for (size_t i = 0; i < n; i++) printf("%02x", bytes[i]);
  printf("\n");
}

static void input(void) {
  const char *s = "adas.Input";
  AdasInput v;
  adas_Input sil;
  fill(&v, sizeof v);
  memcpy(&sil, &v, sizeof v);
  printf("size %s %zu %zu\n", s, sizeof v, sizeof sil);
  print_bytes(s, &sil, sizeof sil);
  UINT(s, "seq", "-", v.seq);
  FLOAT(s, "ego_pose_x", "-", v.ego.pose.x);
  FLOAT(s, "ego_pose_y", "-", v.ego.pose.y);
  FLOAT(s, "ego_pose_yaw", "-", v.ego.pose.yaw);
  FLOAT(s, "ego_odometer", "-", v.ego.odometer);
  BYTE(s, "ego_valid", "-", v.ego.valid);
  for (int i = 0; i < 2; i++)
    for (int j = 0; j < 3; j++) {
      char index[8];
      snprintf(index, sizeof index, "%d", i * 3 + j);
      FLOAT(s, "grid", index, v.grid[i][j]);
    }
  for (int i = 0; i < 3; i++) {
    char name[32];
    const struct tracked_object *o = &v.objects[i];
    snprintf(name, sizeof name, "objects_%d_id", i);
    UINT(s, name, "-", o->id);
    snprintf(name, sizeof name, "objects_%d_cls", i);
    INT(s, name, "-", o->cls);
    snprintf(name, sizeof name, "objects_%d_lateral", i);
    INT(s, name, "-", o->lateral);
    snprintf(name, sizeof name, "objects_%d_lane", i);
    UINT(s, name, "-", o->lane);
    snprintf(name, sizeof name, "objects_%d_range", i);
    FLOAT(s, name, "-", o->range);
  }
  UINT(s, "gear", "-", v.gear);
  BYTE(s, "brake", "-", v.brake);
}

static void output(void) {
  const char *s = "adas.Output";
  struct AdasOutput v;
  adas_Output sil;
  fill(&v, sizeof v);
  memcpy(&sil, &v, sizeof v);
  printf("size %s %zu %zu\n", s, sizeof v, sizeof sil);
  print_bytes(s, &sil, sizeof sil);
  INT(s, "accel_cmd", "-", v.accel_cmd);
  UINT(s, "status", "-", v.status);
  INT(s, "offset", "-", v.offset);
  INT(s, "counters", "0", v.counters[0]);
  INT(s, "counters", "1", v.counters[1]);
}

int main(void) {
  input();
  output();
  return 0;
}
