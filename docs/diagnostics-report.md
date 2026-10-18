# Diagnostics report contracts

[Documentation index](README.md) · [Decision](adr/0004-opt-in-run-diagnostics-runner.md)

Two versioned JSON documents carry Run measurements:

- `{"sil_run_diagnostics": 1, ...}`: one Run, written by `sil-run-diagnostics`.
- `{"sil_benchmark_report": 1, ...}`: repeated Runs, written by
  `sil benchmark` (#303), with every Run report embedded.

Valid examples of every case are in
[tests/fixtures/diagnostics/](../tests/fixtures/diagnostics/).
`sil.diagnostics_report` validates both documents (`load_report`,
`parse_report`, `validate_run_report`, `validate_benchmark_report`). A reader
must reject an unknown version.

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
| `not_collected` | This collector version does not collect the value (resource records until #301) |

A later collector version can add reasons, for example
`unsupported_platform`. A reader shows an unknown reason as it is.

## Run report: `sil_run_diagnostics` v1

| Member | Content |
| --- | --- |
| `collector` | `name`, `collector_version` (1), `clock` (`host_monotonic`), `slowest_retained` (20), `p95_minimum_samples` (20), `p99_minimum_samples` (100) |
| `identity` | `manifest_path` as given, `manifest_hash`, `sil_version`, `source_repository`, `source_revision` |
| `host` | `os`, `os_release`, `architecture`, `logical_cpu_count`, `cpu_description` |
| `configuration` | `measurement_mode` (`diagnostics`), `recording`, `participant_timeout_ms` (null when not given), the three Step-protocol size guards |
| `run` | `exit_code`, `status` (`succeeded`, `run_failure`, `configuration_error`, `interrupted`), `failure` (null on success; else `message`, `category` as in the failure report, `participant`, `virtual_time_ns`) |
| `completeness` | `run_complete` (exit code 0) and `reasons` (the status of a failed Run) |
| `collection_errors` | List of `{scope, message}` for measurements the collector lost |
| `execution` | `duration_ns`, `last_virtual_time_ns` (the last Slot opened) and `participants` |
| `observational` | Times, statistics and resource records, below |
| `artifacts` | `recording` (null without a Recording), `provenance`, `failure_report` (null on success) |

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
| `resources` | `runner` and `process_participants` resource records; `native_participants` is `shared_with_runner` |

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
| `mean_ns`, `stddev_ns` | Exact running moments; population standard deviation |
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
`user_cpu_ns`, `system_cpu_ns`, `peak_rss_bytes` and `peak_rss_kind`.
Collector version 1 reports every value as null with `not_collected`; #301
fills them in. Native participants share the runner process, so they have no
record of their own. A peak resident value is a largest-process high-water
mark; a reader must not add peaks to make a total.

## Benchmark report: `sil_benchmark_report` v1

[benchmark-complete.json](../tests/fixtures/diagnostics/benchmark-complete.json)
is the shape `sil benchmark` (#303) writes.

| Member | Content |
| --- | --- |
| `generator` | `name` (`sil benchmark`), `sil_version` |
| `measurement_mode` | `diagnostics`: every Run uses the diagnostics runner |
| `identity` | `manifest_path`, `manifest_hash`, `sil_version`, `source_revision`, `diagnostics_collector_version` |
| `host` | As in the Run report |
| `policy` | `repeats`, `warmup`, `recording`, `execution` (`serial_fresh_processes`), `participant_timeout_ms` and the three size guards |
| `status` | `complete`, `exit_code` (the first Run failure, else 0), `stopped_reason` (null, `warmup_failed`, `run_failed` or `interrupted`) |
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
