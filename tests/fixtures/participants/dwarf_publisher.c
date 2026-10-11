/* Native participant for the sil schema import round trip (issue #253).
 *
 * It fills the project type AdasInput through its own members, copies it with
 * memcpy into the imported SiL struct and publishes that on "adas.input".
 * There is no conversion code: the imported layout is the compiled layout.
 * The tests compile it against the header that silschema generates from the
 * imported Schemas. */
#include <stdlib.h>
#include <string.h>

#include <sil/participant.h>

#include "dwarf_layout.h"
#include "dwarf_messages.h"

typedef struct {
  const sil_api_v1 *api;
  uint8_t step;
} publisher;

static void tick(void *user, uint64_t now_ns) {
  (void)now_ns;
  publisher *p = user;
  AdasInput v;
  memset(&v, 0, sizeof v); /* deterministic padding bytes */
  v.seq = p->step;
  v.ego.pose.x = 1.5f + (float)p->step;
  v.ego.pose.y = -2.25f;
  v.ego.pose.yaw = 0.125;
  v.ego.odometer = 100.0 + p->step;
  v.ego.valid = true;
  for (int i = 0; i < 2; i++)
    for (int j = 0; j < 3; j++) v.grid[i][j] = (float)(i * 10 + j);
  for (int i = 0; i < 3; i++) {
    v.objects[i].id = (object_id)(100 + i);
    v.objects[i].cls = (char)('a' + i);
    v.objects[i].lateral = (signed char)(-1 - i);
    v.objects[i].lane = (unsigned char)i;
    v.objects[i].range = 10.0f * (float)(i + 1);
  }
  v.gear = GEAR_DRIVE;
  v.brake = p->step % 2 == 1;
  p->step++;

  adas_Input message;
  memcpy(&message, &v, sizeof message);
  if (p->api->publish(p->api->ctx, "adas.input", &message, sizeof message) !=
      SIL_OK)
    p->api->fail(p->api->ctx, "publish failed");
}

int sil_participant_init(const sil_api_v1 *api, const char *name,
                         const char *config_json) {
  (void)name;
  (void)config_json;
  publisher *p = calloc(1, sizeof *p); /* owned by the Run */
  if (p == NULL) return SIL_ERR;
  p->api = api;
  return api->register_task(api->ctx, "tick", 10000000, 0, 0, tick, p);
}
