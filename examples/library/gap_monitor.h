/* gap_monitor — the example library of the port binding in examples/library/.
 *
 * It stands in for an adopter's ADAS library with several interfaces: two
 * input structs set by their own calls, two cyclic runnables at different
 * rates, and one output struct per runnable. Like speed_filter, it has no SiL
 * header, keeps its state in globals and writes progress to stdout.
 *
 * The `track` runnable computes the time gap to the radar object from the
 * latest inputs. The `report` runnable summarizes the `track` cycles since
 * the previous report: the smallest time gap and how many were below the
 * warning threshold. A report without a `track` cycle since the previous
 * report gives the latest time gap and no warning.
 */
#ifndef GAP_MONITOR_H
#define GAP_MONITOR_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define GAP_MONITOR_OK 0
#define GAP_MONITOR_BAD_PERIOD (-1)
#define GAP_MONITOR_BAD_WARNING_GAP (-2)
#define GAP_MONITOR_ALREADY_INITIALIZED (-3)
#define GAP_MONITOR_NOT_INITIALIZED (-4)
#define GAP_MONITOR_BAD_SPEED (-5)
#define GAP_MONITOR_BAD_RANGE (-6)

typedef struct gap_monitor_config {
  double period_s;      /* > 0: the base cycle the runnables are scheduled on */
  double warning_gap_s; /* >= 0: a time gap below it counts as a warning */
} gap_monitor_config;

typedef struct gap_monitor_ego {
  double speed_mps; /* > 0 when `track` runs */
} gap_monitor_ego;

typedef struct gap_monitor_radar {
  double range_m; /* >= 0 */
  uint32_t object_id;
} gap_monitor_radar;

typedef struct gap_monitor_gap {
  double time_gap_s;
  uint32_t object_id;
  uint32_t track_cycles; /* `track` cycles since gap_monitor_init */
} gap_monitor_gap;

typedef struct gap_monitor_report {
  double min_time_gap_s;
  uint32_t warnings;
  uint32_t report_cycles; /* `report` cycles since gap_monitor_init */
} gap_monitor_report;

/* Starts one lifecycle. Returns GAP_MONITOR_OK or a negative error code; a
 * second init without gap_monitor_terminate is refused. */
int gap_monitor_init(const gap_monitor_config *config);

/* Set the input the next runnable reads. */
int gap_monitor_set_ego(const gap_monitor_ego *ego);
int gap_monitor_set_radar(const gap_monitor_radar *radar);

/* The 10 ms runnable: one time gap from the current inputs. */
int gap_monitor_track(void);

/* The 30 ms runnable: one summary of the `track` cycles since the last. */
int gap_monitor_report_cycle(void);

/* The result of the latest cycle of each runnable. */
void gap_monitor_gap_output(gap_monitor_gap *gap);
void gap_monitor_report_output(gap_monitor_report *report);

/* Ends the lifecycle; a later gap_monitor_init starts from scratch. */
void gap_monitor_terminate(void);

#ifdef __cplusplus
}
#endif

#endif /* GAP_MONITOR_H */
