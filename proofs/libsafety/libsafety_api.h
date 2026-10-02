/* opendbc's safety library: its C API, its packet and the replay policy.
 *
 * Shared by native_adapter.c (the Native participant) and library_cost.c
 * (the computation timing), so both make the same calls in the same order.
 * The declarations are those of opendbc's own libsafety harness at the
 * pinned commit. Header-only: every function is static.
 */
#ifndef LIBSAFETY_API_H
#define LIBSAFETY_API_H

#include <dlfcn.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "libsafety_messages.h"

#define TIMER_MODULUS 0xFFFFFFFFull
#define TICK_MARGIN_NS 1000000000ull
/* opendbc's packed CANPacket_t: a 40-bit header, a checksum byte, 64 data
 * bytes. Classic CAN only: a length of 0 to 8 bytes is its own DLC. */
#define PACKET_BYTES 70
#define HEADER_BYTES 5
#define MAX_CLASSIC_LENGTH 8
#define EXTENDED_FROM 0x800u
/* CANPacket_t holds a bitfield of unsigned int, so the library reads it with
 * 4-byte alignment; UBSan in the pinned build stops at a misaligned one. */
#define PACKET_ALIGNMENT 4

typedef struct can_packet {
  _Alignas(PACKET_ALIGNMENT) uint8_t bytes[PACKET_BYTES];
} can_packet;

typedef struct library {
  int (*set_safety_hooks)(uint16_t mode, uint16_t param);
  void (*set_alternative_experience)(int mode);
  void (*set_timer)(uint32_t t);
  void (*safety_tick)(void);
  bool (*safety_config_valid)(void);
  int (*safety_fwd_hook)(int bus, int address);
  bool (*safety_rx_hook)(void *packet);
  bool (*safety_tx_hook)(void *packet);
  bool (*get_controls_allowed)(void);
  bool (*get_gas_pressed_prev)(void);
  bool (*get_brake_pressed_prev)(void);
  bool (*get_cruise_engaged_prev)(void);
  bool (*get_vehicle_moving)(void);
  bool (*get_acc_main_on)(void);
  float (*get_vehicle_speed_min)(void);
  float (*get_vehicle_speed_max)(void);
} library;

#define RESOLVE(name)                                                    \
  if (!(*(void **)&lib->name = dlsym(handle, #name))) {                 \
    snprintf(error, size, "library '%s' does not export '%s'", path,    \
             #name);                                                     \
    return false;                                                        \
  }

/* Loads the library and resolves every symbol before anything runs. */
static bool bind(const char *path, library *lib, char *error, size_t size) {
  void *handle = dlopen(path, RTLD_NOW | RTLD_LOCAL);
  if (!handle) {
    snprintf(error, size, "cannot load library '%s': %s", path, dlerror());
    return false;
  }
  RESOLVE(set_safety_hooks)
  RESOLVE(set_alternative_experience)
  RESOLVE(set_timer)
  RESOLVE(safety_tick)
  RESOLVE(safety_config_valid)
  RESOLVE(safety_fwd_hook)
  RESOLVE(safety_rx_hook)
  RESOLVE(safety_tx_hook)
  RESOLVE(get_controls_allowed)
  RESOLVE(get_gas_pressed_prev)
  RESOLVE(get_brake_pressed_prev)
  RESOLVE(get_cruise_engaged_prev)
  RESOLVE(get_vehicle_moving)
  RESOLVE(get_acc_main_on)
  RESOLVE(get_vehicle_speed_min)
  RESOLVE(get_vehicle_speed_max)
  return true;
}

/* The packed CANPacket_t upstream's make_CANPacket builds. Header bits,
 * least significant first: fd 1, bus 3, data_len_code 4, rejected 1,
 * returned 1, extended 1, addr 29. The checksum byte stays 0. */
static void packet(const can_TimedFrame *frame, int bus, can_packet *packet) {
  uint8_t *out = packet->bytes;
  uint64_t header = ((uint64_t)(bus & 0x7) << 1) |
                    ((uint64_t)frame->length << 4) |
                    ((uint64_t)frame->address << 11);
  if (frame->address >= EXTENDED_FROM) header |= 1u << 10;
  memset(out, 0, PACKET_BYTES);
  for (int i = 0; i < HEADER_BYTES; i++) out[i] = (uint8_t)(header >> (8 * i));
  const uint8_t data[MAX_CLASSIC_LENGTH] = {frame->d0, frame->d1, frame->d2,
                                            frame->d3, frame->d4, frame->d5,
                                            frame->d6, frame->d7};
  memcpy(out + HEADER_BYTES + 1, data, frame->length);
}

/* Upstream replay's time policy for one recorded segment. */
typedef struct event_policy {
  uint64_t timer_origin_ns;
  uint64_t timer_unit_ns;
  uint64_t first_event_ns;
  uint64_t last_event_ns;
} event_policy;

/* set_timer and, when warm, safety_tick: the start of one upstream event. */
static void start_event(const library *lib, const event_policy *policy,
                        uint64_t event_ns) {
  lib->set_timer((uint32_t)((policy->timer_origin_ns + event_ns) /
                            policy->timer_unit_ns % TIMER_MODULUS));
  if (event_ns > policy->first_event_ns + TICK_MARGIN_NS &&
      event_ns + TICK_MARGIN_NS < policy->last_event_ns)
    lib->safety_tick();
}

/* The library's state after an event, into the observation. */
static void read_state(const library *lib, libsafety_NativeState *out) {
  out->config_valid = lib->safety_config_valid();
  out->controls_allowed = lib->get_controls_allowed();
  out->gas_pressed_prev = lib->get_gas_pressed_prev();
  out->brake_pressed_prev = lib->get_brake_pressed_prev();
  out->cruise_engaged_prev = lib->get_cruise_engaged_prev();
  out->vehicle_moving = lib->get_vehicle_moving();
  out->acc_main_on = lib->get_acc_main_on();
  out->vehicle_speed_min = lib->get_vehicle_speed_min();
  out->vehicle_speed_max = lib->get_vehicle_speed_max();
}

#endif /* LIBSAFETY_API_H */
