/* gap_monitor — see gap_monitor.h.
 *
 * Build it as a plain shared library with the adopter's own toolchain:
 *
 *     cc -shared -fPIC -O2 -o gap_monitor.so gap_monitor.c
 */
#include "gap_monitor.h"

#include <math.h>
#include <stdio.h>

static int initialized;
static double warning_gap_s;
static gap_monitor_ego ego;
static gap_monitor_radar radar;
static gap_monitor_gap gap;
static gap_monitor_report report;
static double window_min_s;
static uint32_t window_warnings;
static uint32_t window_cycles;

int gap_monitor_init(const gap_monitor_config *config) {
  if (initialized) return GAP_MONITOR_ALREADY_INITIALIZED;
  if (!(isfinite(config->period_s) && config->period_s > 0))
    return GAP_MONITOR_BAD_PERIOD;
  if (!(isfinite(config->warning_gap_s) && config->warning_gap_s >= 0))
    return GAP_MONITOR_BAD_WARNING_GAP;
  warning_gap_s = config->warning_gap_s;
  gap = (gap_monitor_gap){0};
  report = (gap_monitor_report){0};
  window_warnings = 0;
  window_cycles = 0;
  initialized = 1;
  printf("gap_monitor: init period=%g s warning_gap=%g s\n", config->period_s,
         config->warning_gap_s);
  fflush(stdout);
  return GAP_MONITOR_OK;
}

int gap_monitor_set_ego(const gap_monitor_ego *value) {
  if (!initialized) return GAP_MONITOR_NOT_INITIALIZED;
  ego = *value;
  return GAP_MONITOR_OK;
}

int gap_monitor_set_radar(const gap_monitor_radar *value) {
  if (!initialized) return GAP_MONITOR_NOT_INITIALIZED;
  if (!(isfinite(value->range_m) && value->range_m >= 0))
    return GAP_MONITOR_BAD_RANGE;
  radar = *value;
  return GAP_MONITOR_OK;
}

int gap_monitor_track(void) {
  if (!initialized) return GAP_MONITOR_NOT_INITIALIZED;
  if (!(isfinite(ego.speed_mps) && ego.speed_mps > 0))
    return GAP_MONITOR_BAD_SPEED;
  gap.time_gap_s = radar.range_m / ego.speed_mps;
  gap.object_id = radar.object_id;
  gap.track_cycles += 1;
  if (window_cycles == 0 || gap.time_gap_s < window_min_s)
    window_min_s = gap.time_gap_s;
  if (gap.time_gap_s < warning_gap_s) window_warnings += 1;
  window_cycles += 1;
  printf("gap_monitor: track %u\n", (unsigned)gap.track_cycles);
  fflush(stdout);
  return GAP_MONITOR_OK;
}

int gap_monitor_report_cycle(void) {
  if (!initialized) return GAP_MONITOR_NOT_INITIALIZED;
  report.min_time_gap_s = window_cycles ? window_min_s : gap.time_gap_s;
  report.warnings = window_warnings;
  report.report_cycles += 1;
  window_warnings = 0;
  window_cycles = 0;
  printf("gap_monitor: report %u\n", (unsigned)report.report_cycles);
  fflush(stdout);
  return GAP_MONITOR_OK;
}

void gap_monitor_gap_output(gap_monitor_gap *result) { *result = gap; }

void gap_monitor_report_output(gap_monitor_report *result) {
  *result = report;
}

void gap_monitor_terminate(void) {
  printf("gap_monitor: terminate after %u track and %u report cycles\n",
         (unsigned)gap.track_cycles, (unsigned)report.report_cycles);
  fflush(stdout);
  initialized = 0;
}
