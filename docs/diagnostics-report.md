# Diagnostics report contracts

[Documentation index](README.md) · [Decision](adr/0004-opt-in-run-diagnostics-runner.md)

Two versioned JSON documents carry Run measurements:

- `{"sil_run_diagnostics": 1, ...}`: one Run, written by `sil-run-diagnostics`.
  `sil run --diagnostics` starts it and prints a summary
  ([docs/running.md](running.md#measure-a-run)).
- `{"sil_benchmark_report": 1, ...}`: repeated Runs, written by
  `sil benchmark` (#303), with every Run report embedded.

Valid examples of every case are in
[tests/fixtures/diagnostics/](../tests/fixtures/diagnostics/).
`sil.diagnostics_report` validates both documents (`load_report`,
`parse_report`, `validate_run_report`, `validate_benchmark_report`). The
validator checks the JSON type of every member below that a consumer reads
(`sil.diagnostics_shape`), so a report it accepts is one the summaries and the
HTML report can present. A reader must reject an unknown version. `sil diagnostics render` presents either
document as a standalone HTML page
([docs/running.md](running.md#html-reports)).

## Conventions

- Units are in member names: `_ns` is nanoseconds (an integer unless it is a
  mean or a standard deviation), `_bytes` is bytes, `_per_s` is per second.
- No member is NaN or infinite.
- **Null with a reason.** An object that can hold an unavailable value has an
  `unavailable` map. Each null member of that object is named in the map with
  a reason string, and the map names no member that has a value. An unavailable
  value is null, never zero.
- Execution counts and identities, which a repeated Run reproduces, are in
  `execution`. Times, samples, CPU and memory, which vary per Run, are in
  `observational`.
- No Message payload and no environment variable is in a report.

Reasons in v1:

| Reason | Meaning |
| --- | --- |
| `not_reached` | The Run ended before this phase |
| `no_recording` | `--no-recording`, so there is no Recording to finalize |
| `no_completed_activations` | No completed activation to describe |
| `insufficient_samples` | Fewer completed samples than the percentile needs |
| `run_not_successful` | The factor is defined only for a successful full Run |
| `no_scheduled_execution` | The scheduled-execution time is missing or zero |
| `no_slot_opened` | The Run opened no Slot |
| `manifest_not_loaded` | The Manifest was not loaded |
| `not_reported_by_os` | The host does not report this value |
| `not_measured` | The collector has no measurement for this value |
| `unsupported_platform` | The collector cannot measure resources on this host |
| `not_started` | The Process participant was never started, so it has no resource record |
| `incomplete_scopes` | A total is missing because one of the scopes it adds is missing |
| `no_successful_measured_runs` | Benchmark: no measured Run succeeded, so a statistic has no sample |
| `unavailable_in_a_measured_run` | Benchmark: a successful measured Run has no value for this metric |
| `no_run_report` | Benchmark: no Run wrote a report to take this identity or host value from |

Collector version 1 also used `not_collected` for every resource value. A
later collector version can add reasons. A reader shows an unknown reason as
it is.

## Run report: `sil_run_diagnostics` v1

| Member | Content |
| --- | --- |
| `collector` | `name`, `collector_version` (2), `clock` (`host_monotonic`), `slowest_retained` (20), `p95_minimum_samples` (20), `p99_minimum_samples` (100) |
| `identity` | `manifest_path` as given, `manifest_hash`, `sil_version`, `source_repository`, `source_revision` |
| `host` | `os`, `os_release`, `architecture`, `logical_cpu_count`, `cpu_description` |
| `configuration` | `measurement_mode` (`diagnostics`), `recording`, `provenance_mode` (`full` or `external`), `participant_timeout_ms` (null when not given), the three Step-protocol size guards |
| `run` | `exit_code`, `status` (`succeeded`, `run_failure`, `configuration_error`, `interrupted`), `failure` (null on success; else `message`, `category` as in the failure report, `participant`, `virtual_time_ns`) |
| `completeness` | `run_complete` (exit code 0) and `reasons` (the status of a failed Run) |
| `collection_errors` | List of `{scope, message}` for measurements the collector lost |
| `execution` | `duration_ns`, `last_virtual_time_ns` (the last Slot opened) and `participants` |
| `observational` | Times, statistics and resource records, below |
| `artifacts` | `recording` (null without a Recording), `provenance`, `failure_report` (null on success) |

Provenance hashing is runner work, so the mode changes what the times mean. A
report without `provenance_mode` predates the option. Those Runs used `full`,
and a reader shows this as `full (report predates the option)`.

`execution.participants` lists every Participant in Manifest (name) order:
`participant`, `kind` (`native`, `process`, `replay`), `tasks` and `replay`.

- A Task row has `task`, `kind` (`native_task` or `process_step`),
  `period_ns`, `offset_ns`, and the counts `attempted`, `completed`, `failed`,
  `interrupted`. `attempted` is the sum of the other three. A Process
  participant has one Task, `step`.
- `failed_activation` is null, or the first failed or interrupted activation:
  zero-based `activation_index`, `virtual_time_ns`, `outcome` (`failed` or
  `interrupted`). It is not in any distribution.
- A Replay participant has no Task. Its `replay` object counts publication
  Slots (a Slot where at least one Message was due) with the same counts, and
  `publications`: Messages published, including any an Interceptor
  suppressed. Replay work is never a Step or an activation.

### Observational members

| Member | Content |
| --- | --- |
| `run_wall_time` | `wall_ns` and `boundary`: from the runner's measurement start, before the Manifest is loaded, through Participant cleanup and Recording close. Python startup and report writing are outside it. |
| `phases` | `preflight_ns`, `setup_ns`, `scheduled_execution_ns`, `shutdown_ns`, `recording_finalization_ns` and `unattributed_ns`. The intervals do not overlap; with `unattributed_ns` they add up to `wall_ns`. |
| `tasks` | One row per Task: `participant`, `task`, `kind`, `timing`, and `failed_activation` (`activation_index`, `wall_ns`) or null |
| `replay` | One row per Replay participant: `participant`, `timing`, `failed_activation` |
| `throughput` | `completed_native_activations`, `completed_process_steps`, `native_activations_per_s`, `process_steps_per_s`, and their `denominator`, `scheduled_execution_ns` |
| `virtual_time_speed` | `scheduled_factor` (Duration / scheduled-execution time) and `end_to_end_factor` (Duration / Run wall time); null for a failed or partial Run |
| `resources` | `runner` and `process_participants` resource records, `cpu_total`; `native_participants` is `shared_with_runner` |

Phases: preflight is Manifest load, preparation and Recording open; setup is
Engine construction and Participant init or spawn; scheduled execution is the
Slot loop; shutdown is Process shutdown, Run directory removal and Engine
teardown; Recording finalization is Recording close.

Do not add activation times to phase times: routing and recording called from
inside a Task are inside both.

### Timing

A `timing` object describes the **completed** activations of one Task or the
completed publication Slots of one Replay participant:

| Member | Content |
| --- | --- |
| `scope` | `inclusive_native_activation`, `inclusive_process_step` or `inclusive_replay_publication_slot` |
| `completed`, `sum_ns`, `min_ns`, `max_ns` | Exact |
| `mean_ns`, `stddev_ns` | `mean_ns` is `sum_ns / completed`, from the exact integer sum, rounded once to the nearest double; `stddev_ns` is the population standard deviation from running moments (Welford) |
| `p50_ns`, `p95_ns`, `p99_ns` | Nearest rank, rank = ceil(p × n / 100), over the retained samples |
| `quantiles` | `method` (`nearest_rank`), `approximate`, `sample_count` (all completed), `retained_count`, `reservoir` (`algorithm` `algorithm_r`, `generator` `splitmix64`, `algorithm_version` 1, `capacity` 8192, `seed`) |
| `samples_ns` | The retained samples, ascending |
| `slowest` | At most 20 slowest completed activations: `activation_index`, `virtual_time_ns`, `wall_ns`; slowest first, equal times in activation-index order |

- A native activation is timed from before the call into the Task to its
  return. That includes routing and recording the Task calls synchronously.
- A Process Step is timed from preparing the Step inputs through response
  handling and output publication. That includes codec work, IPC, OS waiting,
  the Participant and routing/recording. It is not isolated algorithm time.
- Up to 8,192 completed samples the percentiles are exact. Above that, the
  reservoir keeps a uniform sample, `approximate` is true, and the percentiles
  are estimates. The reservoir uses its own seeded generator, never one a
  Participant can share.
- `p95_ns` needs 20 completed samples and `p99_ns` 100. These minimums do not
  make a tail statistically reliable. No outlier is removed, and a maximum is
  not a worst-case execution time.
- The collector reads the runner's monotonic clock, which the Participant Clock
  shim does not interpose.

### Resource records

A resource record has `scope` (`runner_process` or `process_participant`,
with `participant`), `collector`, `interval`, `includes_waited_descendants`,
`user_cpu_ns`, `system_cpu_ns`, `peak_rss_bytes`, `peak_rss_kind` and
`limitations`. CPU times are nanoseconds; peak resident memory is bytes on
every host (Linux reports KiB, macOS bytes).

| Scope | `collector` | `interval` | `includes_waited_descendants` | `peak_rss_kind` |
| --- | --- | --- | --- | --- |
| `runner_process` | `getrusage_self` | `runner_start_to_run_end`: from the runner's process start to the end of Run wall time | `false` | `process_peak_rss` |
| `process_participant` | `wait4_child` | `spawn_to_reap`: from fork to the reap of the child | `true` | `largest_process_peak_rss` |

- The runner record is the runner's own usage, all threads, without any
  child. Native participants run in the runner, so they have no record of
  their own; their cost is in the runner record.
- A Process participant record is the child's usage plus that of the
  descendants it waited for. A grouped FMU Importer is one Process
  participant and one record.
- A failed or short-lived Participant keeps its record: the child is reaped,
  and so measured, on every cleanup path. A Run that ends by SIGKILL writes no
  report.
- `peak_rss_bytes` is a high-water mark of one process: for a Process
  participant, of the largest process among the child and its waited
  descendants. It is not the memory the process tree held at one time.

`cpu_total` (`scope` `runner_and_process_participants`, `user_cpu_ns`,
`system_cpu_ns`) adds the runner and every Process participant record. These
scopes do not overlap: the runner's self usage excludes the children it
reaped, and each child is reaped once. One missing scope makes the total
null with `incomplete_scopes`. CPU time can exceed wall time for
multithreaded software. **No memory total exists.** Peaks of different
processes need not occur at the same time, so a sum of them is memory no
process tree used; the validator rejects any other member of `resources`.

`limitations` names the OS accounting limits of a measured record:

| Code | Meaning |
| --- | --- |
| `peak_includes_parent_rss_at_spawn` | Linux: a high-water mark survives exec, so the peak is at least the parent's resident size when it forked. For the runner, start it through `sil-diagnostics-launcher` so that parent is small; for a Process participant, the parent is the runner. |
| `descendants_counted_only_when_waited` | A descendant the child did not wait for (one that outlived it or was reparented) is in no record. |

An unmeasured record is null in every value with one reason, and its
`limitations` list is empty.

### Launcher

`sil-diagnostics-launcher [--status-fd N] RUNNER [ARG...]` runs
`RUNNER ARG...` as its child and ends as it ended: with its exit code, or by
the same signal. It forwards SIGINT, SIGTERM and SIGHUP to the child and
changes nothing else in its environment. Its own failures are one line
beginning `sil-diagnostics-launcher: ` and exit 2 (usage), 1 (fork or wait
failed) or 127 (RUNNER cannot be executed). The line goes to stderr, or to
the open file descriptor N with `--status-fd`; RUNNER does not inherit N, so
no Run output can pose as a launcher failure. A supervisor that has a large
resident size, such as a Python process, starts `sil-run-diagnostics` through
it; `sil run` and `sil benchmark` pass `--status-fd`.

With `--environment-fd M` (after `--status-fd`, when both are given), RUNNER
gets the environment that the open file descriptor M holds, NUL-terminated
`NAME=VALUE` entries to its end, instead of the launcher's own. RUNNER does
not inherit M. An unreadable descriptor, or one with more than 16 MiB or
without a final NUL, is exit 2. `sil profile --collector cpu` passes its own
environment this way, because perf changes the environment of the command it
starts.

## Benchmark report: `sil_benchmark_report` v1

[benchmark-complete.json](../tests/fixtures/diagnostics/benchmark-complete.json)
is the shape `sil benchmark` (#303) writes.

| Member | Content |
| --- | --- |
| `generator` | `name` (`sil benchmark`), `sil_version` |
| `measurement_mode` | `diagnostics`: every Run uses the diagnostics runner |
| `identity` | `manifest_path` (absolute), `manifest_hash`, `sil_version`, `source_revision`, `diagnostics_collector_version`, `unavailable`; from the first Run report, null with `no_run_report` when no Run wrote one |
| `host` | As in the Run report; null with `no_run_report` when no Run wrote one |
| `policy` | `repeats`, `warmup`, `recording`, `execution` (`serial_fresh_processes`), `participant_timeout_ms` and the three size guards: the values in the first Run report's `configuration`, else the requested ones (null = runner default) |
| `status` | `complete`, `exit_code` (the command's exit code for the first failed Run, as `sil run --diagnostics` gives it: the Run's code, `1` for a missing or invalid report, `128+N` for a signal; `128+N` for a signal between Runs; else 0), `stopped_reason` (null, `warmup_failed`, `run_failed`, `report_failed` for a Run that exited 0 without a valid report, or `interrupted`) |
| `counts` | `requested`, `attempted`, `completed`, `failed`, and the same four with the prefix `warmups_` |
| `runs` | Every attempted warm-up and measured Run, in order |
| `aggregates` | `scope` (`successful_measured_runs`), `sample_count` and `metrics` |

A `runs` entry has `label` (`warmup-001`, `run-001`, ...), `role` (`warmup` or
`measured`), `index`, `directory`, `run_exit_code` (null if the process ended
by a signal), `process_outcome` (`exit_code`, `signal`), `report_status`
(`present` or `missing`), `report_path`, and `report`: the complete embedded
Run report, or null when missing. The summary therefore renders without the
sibling directories.

An aggregate metric has `metric`, `unit`, `source` (the Run report member it
reads), `per_run` (`label` and `value` for each successful measured Run), and
`min`, `median`, `max`, `mean`, `stddev` (population) with an `unavailable`
map. The median of an even count is the mean of the two middle values.
Aggregates use only successful measured Runs: warm-ups and failed Runs never
enter them, and `sample_count` is their number. With no such Run every
statistic is null with `no_successful_measured_runs`. Activations are never
pooled across Runs; per-Run distributions stay in the embedded reports.

A Run is successful when its process exited 0 and it wrote a valid report.
`counts.completed` counts successful Runs, `counts.failed` the others. A
`per_run` value is null when that Run report has no value; its reason is in
the Run report at `source`. Then every statistic of the metric is null with
`unavailable_in_a_measured_run`, so no statistic uses fewer Runs than
`sample_count`.

The metrics, in this order:

| `metric` | `unit` | `source` |
| --- | --- | --- |
| `run_wall_ns` | `ns` | `observational.run_wall_time.wall_ns` |
| `preflight_ns`, `setup_ns`, `scheduled_execution_ns`, `shutdown_ns`, `recording_finalization_ns`, `unattributed_ns` | `ns` | `observational.phases.<metric>` |
| `scheduled_factor`, `end_to_end_factor` | `factor` | `observational.virtual_time_speed.<metric>` |
| `native_activations_per_s`, `process_steps_per_s` | `1/s` | `observational.throughput.<metric>` |
| `runner_user_cpu_ns`, `runner_system_cpu_ns`, `runner_peak_rss_bytes` | `ns`, `bytes` | `observational.resources.runner.*` |
| `cpu_total_user_cpu_ns`, `cpu_total_system_cpu_ns` | `ns` | `observational.resources.cpu_total.*` |
| `process_user_cpu_ns`, `process_system_cpu_ns`, `process_peak_rss_bytes` | `ns`, `bytes` | `observational.resources.process_participants[participant].*`; one metric each per Process participant of any successful measured Run, in name order, with a `participant` member; a Run without that record has a null value |

The throughput denominator of each Run is `scheduled_execution_ns`, which is
also a metric. No memory metric adds peaks of different processes.
