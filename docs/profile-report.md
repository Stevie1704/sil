# Profile report contract

[Documentation index](README.md) · [Spec #313](https://github.com/Stevie1704/sil/issues/313) · [perf proof](../proofs/profiling/README.md)

`sil profile` captures one profile of one fresh Run. It writes a profile
directory. The summary of that directory is one versioned JSON document,
`{"sil_profile_report": 1, ...}`, in `profile.json`. This page defines the
command, the directory and the document.

Status: the CPU collector (Linux perf) is implemented (#315). The heap
collector (#317) and Activation correlation (#316) are reserved: this
contract fixes their discriminated shapes, and the fixtures show them, but
no command produces them yet. The offline HTML view is #318.

Valid and invalid examples of every case are in
[tests/fixtures/profile/](../tests/fixtures/profile/).
`sil.profile_report` validates the document (`load_profile_report`,
`parse_profile_report`, `validate_profile_report`). A reader must reject an
unknown version.

## Command

```sh
sil profile MANIFEST --out DIR --collector cpu
    [-o RECORDING | --no-recording] [--provenance PATH]
    [--participant-timeout-ms N] [--max-protocol-line-bytes N]
    [--max-step-output-messages N] [--max-step-inline-payload-bytes N]
    [--frequency-hz N] [--stack-bytes N] [--max-capture-bytes N]
    [--symbolization-timeout-s N] [--modules-from PATH]...
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--out DIR` | required | The profile directory. It must not exist; its parent must exist. |
| `--collector` | required | `cpu`. `heap` is reserved for #317 and is rejected (exit 2). |
| `-o`, `--no-recording`, `--provenance` | `DIR/run.mcap` | As `sil run`. A relative path resolves against the current directory. With `--no-recording` the provenance record is `DIR/run.provenance.json`; a failure report is `out.mcap.failure.json` in the current directory, as with `sil run`. |
| the four guards | as `sil run` | Passed to the runner unchanged and recorded. |
| `--frequency-hz` | 999 | perf sampling frequency (`-F`). |
| `--stack-bytes` | 8192 | User stack bytes that perf copies per sample for DWARF unwinding. |
| `--max-capture-bytes` | 1073741824 (1 GiB) | Limit for `perf.data`. |
| `--symbolization-timeout-s` | 600 | Limit for each `perf script` pass. |
| `--modules-from PATH` | none | A directory, an ELF file or a zip archive (an FMU) with copies of modules that are gone after the Run. Repeatable. |

`--participant NAME` and `--runner` are reserved for the heap collector.
With `--collector cpu` they are a usage error.

One command does one fresh Run with one collector. It does not repeat the
Run, retry, change the collector or the event, or change a host setting.

### What the command does

1. **Preflight.** It checks every output path (new, no alias of the
   Manifest, the Recording, the provenance record or the failure report),
   finds `sil-run-diagnostics`, `sil-diagnostics-launcher` and `perf` on
   `PATH`, and probes perf with the exact event and options on `true`. A
   failed probe stops the command before any Participant starts.
2. **Run.** It starts

   ```sh
   perf record -e cpu-clock:u -F 999 -k monotonic \
       --call-graph dwarf,8192 --buildid-mmap -N -q \
       --control fifo:DIR/cpu/.control -o DIR/cpu/perf.data -- \
     sil-diagnostics-launcher --status-fd N sil-run-diagnostics MANIFEST \
       -o RECORDING --diagnostics-out DIR/run.diagnostics.json \
       --retain-run-directory DIR/cpu/.run-directory [guards]
   ```

   in a new session, in the current directory, with the environment of the
   command unchanged. `-N` keeps perf from filling the user's build-ID
   cache (`~/.debug`): the profile depends on no file outside `DIR` except
   the host modules it names. perf follows every descendant of
   the runner (`inherit`), so the scope is the runner and all its
   descendants. The Manifest bytes, the working directory, the Participant
   commands and their environment do not change.
3. **Limit.** While the Run executes, the command reads the size of
   `perf.data` every 100 ms. At the limit it sends `disable` to perf's
   control FIFO: sampling stops, the Run continues. The file can be larger
   than the limit by the data of one interval.
4. **Modules.** When the Run ends, the runner moves its Run working
   directory to `DIR/cpu/.run-directory` instead of removing it. For every
   module that perf recorded with a build ID (`perf buildid-list`), and
   whose file is gone or has another build ID, the command looks for a copy
   with the same build ID: first in `.run-directory`, then in the
   `--modules-from` sources. It adds each copy to the build-ID cache
   `DIR/cpu/buildid-cache`, then removes `.run-directory` and the unpacked
   archives. A Participant that removes its own files at shutdown leaves no
   copy: the FMI importer removes its extracted FMU, so name the FMU archive
   with `--modules-from`. Without a copy, the samples of a module keep its
   path, build ID and module offsets, and have no function name.
5. **Export.** `perf script` writes the samples, the task, mmap and lost
   events (`-F comm,pid,tid,time,period,ip,sym,dso --ns --no-inline
   --show-task-events --show-mmap-events --show-lost-events`), and a
   second pass gives the source line of each leaf frame (`-G -F
   ip,sym,srcline`). The analysis commands use
   `perf --buildid-dir DIR/cpu/buildid-cache` and run without
   `DEBUGINFOD_URLS`, so no symbol is fetched from the network.
6. **Publish.** It writes `cpu/samples.json`, then `profile.json` through
   a temporary sibling and a hard link, so a reader never sees a partial
   summary and nothing is replaced. It prints a summary on stdout.

SIGINT, SIGTERM and SIGHUP go to the whole session: the runner records the
interruption and cleans up as without a profiler; perf stops sampling and
finalizes `perf.data`. perf ignores SIGHUP, so it cannot end before the Run.

### Exit codes

The Run outcome and the capture outcome are separate. A capture failure never
replaces a Run failure.

| Situation | Exit code | `profile.json` |
| --- | --- | --- |
| Invalid option, output path exists or is an alias | 2 | not written, `DIR` not made |
| `perf`, the event or a permission is not available (preflight) | 1 | not written, `DIR` not made |
| `sil-run-diagnostics` or the launcher is not on `PATH` | 127 | not written |
| The Run failed (exit code N) | N | written |
| The runner ended by signal N | 128+N | written |
| The Run succeeded, capture `complete` | 0 | written |
| The Run succeeded, capture `partial`, `failed` or `interrupted` | 1 | written |
| The Run succeeded, the summary cannot be written | 1 | not written |
| Hard crash or SIGKILL of the command | signal status | not written; raw files can remain |

Unknown symbols or source lines alone do not fail a capture. Zero samples
is a `complete` capture; it is not evidence that no CPU time was used.

## Profile directory

| Path | Content |
| --- | --- |
| `profile.json` | The `sil_profile_report` v1 summary. Written last. |
| `run.diagnostics.json` | The `sil_run_diagnostics` v1 report of the Run ([contract](diagnostics-report.md)). |
| `run.mcap`, `run.mcap.provenance.json` | The Recording and its provenance record, when not moved by `-o`, `--no-recording` or `--provenance`. |
| `cpu/perf.data` | Raw perf capture: authoritative. It contains copies of user stacks: process memory. |
| `cpu/buildid-cache/` | perf build-ID cache with the captured modules. |
| `cpu/samples.json` | Normalized samples: the offline input for reports (below). |
| `cpu/perf-script.stderr.txt`, `cpu/perf-srcline.stderr.txt`, `cpu/perf-buildid-cache.stderr.txt` | The last 64 KiB of each export's output on stderr; for the cache, of each `perf buildid-cache -a`. A copy that perf did not add is not captured. |

Raw stack copies can hold any data that was on a stack, also secrets. Share
`perf.data` only as you would share a core dump. `samples.json` and
`profile.json` hold no stack bytes, no environment variable and no Message
payload. A copy of `profile.json` and `cpu/samples.json` is sufficient for an
offline report. Re-symbolization needs `perf.data` and the modules: the
captured ones are in `cpu/buildid-cache`, the others stay on the host and
are identified by path and build ID only.

## Conventions

The conventions of the [diagnostics contracts](diagnostics-report.md#conventions)
apply: units in member names, no NaN or infinity, null with a reason in an
`unavailable` map, no Message payload or environment variable.

- A value that can be above 2^53 is a string: absolute timestamps
  (`*_ns` that are host `CLOCK_MONOTONIC` values) are decimal strings,
  addresses are `0x` hexadecimal strings. Times relative to
  `time_origin_ns` (`*_offset_ns`) are integers.
- Identities are local to one document: processes `p1`, `p2`, ...; threads
  `t1`, ...; modules `m1`, .... A PID or TID is not an identity, because the
  kernel can reuse it. A new `fork` of a PID after its `exit` is a new
  process.
- Lists are in a stable order: processes and threads in order of first
  appearance, modules by path then build ID, hotspots by weight then name.
- A `kind` member selects a variant. A reader must reject an unknown kind.
- A `status` with a `reasons` list: `reasons` is empty only for `complete`.

Reasons in v1, in addition to the diagnostics reasons:

| Reason | Meaning |
| --- | --- |
| `run_not_started` | The Run did not start, so there is nothing to capture |
| `no_diagnostics_report` | The runner wrote no valid diagnostics report |
| `not_reported_by_backend` | perf did not report this value |
| `no_build_id` | The module has no recorded build ID |
| `module_unavailable` | No file with the recorded build ID was available |
| `debug_file_mismatched` | A debug file was found with another build ID; it is not used |
| `debug_file_not_found` | No debug file with the module's build ID was found |
| `unverifiable_debug_file` | The module has no build ID, so a separate debug file cannot be matched |
| `symbolization_timeout` | A `perf script` pass took longer than the limit |
| `export_failed` | A `perf script` pass failed or its output did not decode |
| `no_samples` | The capture has no sample to describe |
| `not_requested` | The option or collector was not requested |
| `reserved` | Reserved for a later ticket; not measured by this version |

## `sil_profile_report` v1

| Member | Content |
| --- | --- |
| `producer` | `name` (`sil profile`), `sil_version` |
| `identity` | `manifest_path` as given, `manifest_hash`, `sil_version`, `source_repository`, `source_revision` (from the diagnostics report; null with `no_diagnostics_report`) |
| `host` | `os`, `kernel_release`, `architecture`, `perf_event_paranoid` (null when not readable) |
| `configuration` | `collector`, `recording` (null for `--no-recording`), `participant_timeout_ms` and the three guards (null when not given), `cwd` |
| `run` | The Run outcome, below |
| `capture` | The collector variant: `kind` `cpu` or `heap` |
| `activation_correlation` | `kind` `not_requested` or `activation` |
| `artifacts` | List of `{role, path, bytes, sha256}`: every file the directory holds that `profile.json` relies on; `path` relative to `DIR` |
| `limitations` | Fixed statements that apply to every value, below |

### `run`

| Member | Content |
| --- | --- |
| `status` | `succeeded`, `run_failure`, `configuration_error`, `interrupted` (from the diagnostics report), `ended_by_signal`, `not_started` |
| `exit_code` | The Run's exit code; null when it ended by signal or did not start |
| `signal` | The signal that ended the runner, or null |
| `failure` | null, or the diagnostics report's `run.failure` (`message`, `category`) |
| `diagnostics_report` | `run.diagnostics.json`, or null |
| `supervisor_signal` | The first signal the command received, or null |

`exit_code` is an integer for the four statuses of the diagnostics report,
and null for `ended_by_signal` and `not_started`. `signal` is set only for
`ended_by_signal`. `not_started`: the launcher reported that it could not
execute the runner, or perf could not start its workload.

### `capture`, kind `cpu`

| Member | Content |
| --- | --- |
| `kind` | `cpu` |
| `status` | `complete`, `partial`, `interrupted`, `failed`, `not_started` |
| `reasons` | Subset of `lost_samples`, `capture_limit_reached`, `interrupted`, `backend_failed`, `export_failed`, `run_not_started` |
| `backend` | `name` (`perf`), `version` (`perf --version`), `command` (the `perf record` argument list), `exit_code`, `signal` |
| `sampling` | `event` (`cpu-clock:u`), `event_type` (`software`), `privilege` (`user`), `mode` (`frequency`), `frequency_hz`, `weight` (`period`), `weight_unit` (`cpu_clock_ns`), `clock` (`host_monotonic`) |
| `call_graph` | `method` (`dwarf`), `stack_bytes` |
| `scope` | `kind` (`runner_and_descendants`), `inherit` (true), `pid_namespace` (`collector`: perf and the Run share the PID namespace, so PIDs need no translation) |
| `limits` | `max_capture_bytes`, `capture_bytes`, `limit_reached`, `symbolization_timeout_s` |
| `time_origin_ns` | Host `CLOCK_MONOTONIC` of the first record (decimal string), or null |
| `totals` | Below |
| `symbolization` | Below |
| `processes`, `threads`, `modules` | Below |
| `hotspots` | Below |
| `samples_file` | `cpu/samples.json`, or null |
| `unavailable` | Reasons for null members |

**Weights.** Each sample has the weight `period`: the `cpu-clock` time in
nanoseconds since the previous sample of that thread, as perf reports it.
A weight estimates on-CPU user time. It is not an exact duration, not a call
count, and it does not explain wall time: waiting, IPC and scheduler delay
have no samples.

`totals`:

| Member | Content |
| --- | --- |
| `samples`, `weight_ns` | All samples and the sum of their weights: the denominators of every share |
| `samples_unknown_module`, `samples_unknown_symbol` | Samples whose leaf frame has no module, or a module and no function name |
| `samples_with_caller` | Samples with at least two frames: the unwinder found a caller |
| `lost_samples` | Samples that perf reports lost (`PERF_RECORD_LOST`, `PERF_RECORD_LOST_SAMPLES`); null when a lost record has no count |
| `lost_records` | The number of lost records |

`symbolization`:

| Member | Content |
| --- | --- |
| `function_names` | `samples_with_name`: leaf frames with a function name |
| `call_frame_unwinding` | `method` (`dwarf`), `samples_with_caller` |
| `source_lines` | `status` (`available`, `partial`, `unavailable`), `scope` (`leaf_frames`), `samples_with_line` (null for `unavailable`), `unavailable` |
| `inlined_frames` | `status` `not_requested`: the export uses `--no-inline`, so an inlined function is part of its caller's frame and no sample counts twice |

Function names and DWARF call frames are separate capabilities: a module can
unwind and have no names (a stripped library), or have names and no source
lines.

A `processes` row:

| Member | Content |
| --- | --- |
| `id`, `pid` | Identity and kernel PID |
| `parent` | Identity of the parent, or null when the parent is outside the capture |
| `role` | `launcher` (perf's workload), `runner` (the launcher's child), `descendant` |
| `commands` | The command names (`comm`, at most 15 bytes each) in order; an `exec` adds one |
| `execs` | Number of `exec` events |
| `start_offset_ns`, `end_offset_ns` | First and last record of the process, relative to `time_origin_ns` |
| `forked`, `exited` | Whether the capture has the fork and the exit event |
| `samples`, `weight_ns` | Samples of all its threads |
| `threads` | Number of thread identities |

Every descendant that perf saw is listed, also one with no sample. A process
with `samples` 0 was observed and had no sample; that is not evidence of no
CPU use. Processes that started before the capture or outside the runner's
descendants are not observed.

A `threads` row: `id`, `process`, `tid`, `commands`, `samples`, `weight_ns`.

A `modules` row:

| Member | Content |
| --- | --- |
| `id`, `path` | Identity and the path perf recorded |
| `build_id` | GNU build ID (hexadecimal), or null |
| `material` | `host_file` (the file at `path` has the build ID), `captured_run_directory` or `captured_modules_from` (a copy with the build ID, from the retained Run working directory or a `--modules-from` source, is in `cpu/buildid-cache`), `unavailable` |
| `debug` | `status` (`embedded`, `separate_matching`, `separate_mismatched`, `not_found`, `unverifiable`, `module_unavailable`) and `path` of the matching debug file or null |
| `mappings` | Executable mappings: `{process, start, size, file_offset}`, addresses as hexadecimal strings |
| `samples_leaf`, `samples_unknown_symbol` | Leaf-frame samples in the module, and those without a function name |
| `unavailable` | Reasons for null members |

A source line is reported only for a module with debug status `embedded` or
`separate_matching`. A mismatched debug file is never used, so it cannot give
a false source location; its samples stay in the module with an unknown
function.

`hotspots`:

| Member | Content |
| --- | --- |
| `limit` | Rows kept per list (50) |
| `self` | Rows by self weight: the function is the leaf frame |
| `self_remainder` | `samples`, `weight_ns` of the functions not in `self`; `self` plus the remainder equals `totals` |
| `inclusive` | Rows by inclusive weight: the function is anywhere in the stack, counted once per sample |

A row: `module` (identity or null), `symbol` (or null: an unknown function
in that module), `self_samples`, `self_weight_ns`, `inclusive_samples`,
`inclusive_weight_ns`, `source_lines` (at most 5 `{file, line, samples}` of
leaf frames, most samples first). Inclusive values overlap, so they do not
add up.

### `capture`, kind `heap` (reserved, #317)

| Member | Content |
| --- | --- |
| `kind` | `heap` |
| `status` | `complete`, `partial`, `interrupted`, `failed`, `not_started`, `unavailable` |
| `reasons` | List of reasons |
| `backend` | `name` (`heaptrack`), `version` (or null) |
| `scope` | `kind` `runner`; or `kind` `process_participant` with `participant`; or `kind` `unsupported` with `requested` and `reason` |
| `raw` | Path of the raw Heaptrack file, or null |
| `analysis` | Reserved: null in v1 |
| `unavailable` | Reasons for null members |

The scope `unsupported` records a request that this version cannot capture,
for example a Native participant (it is not a process) or a scope that is not
qualified. The launch mechanics of a Process scope are not fixed here.
#317 defines `analysis`; if its members are not a compatible addition, the
version changes.

### `activation_correlation`

- `{"kind": "not_requested"}`: the CPU collector of #315 always writes this.
- kind `activation` (reserved, #316): `status` (`complete`, `partial`,
  `unavailable`, `failed`), `reasons`, `clock` (`host_monotonic` or null),
  `observations` (sidecar path or null), `summary` (null in v1) and
  `unavailable`. #316 defines `summary` and the sidecar.

### `limitations`

The CPU capture always lists:

- `cpu_samples_estimate_on_cpu_time`: weights are sampled estimates, not
  exact durations or call counts.
- `user_space_only`: `:u` samples no kernel code.
- `scope_is_runner_and_descendants`: other processes are not sampled.
- `host_build_only`: the profile describes this host build, not a target ECU
  or a worst-case execution time.

## Normalized samples: `cpu/samples.json`

`{"sil_profile_cpu_samples": 1, ...}`:

| Member | Content |
| --- | --- |
| `time_origin_ns` | As in `profile.json` |
| `frames` | List of `{module, symbol, address, module_offset, source}`; `module_offset` is the file offset (`address - start + file_offset`), `source` is `{file, line}` or null |
| `stacks` | List of frame index lists, leaf first |
| `samples` | List of `[offset_ns, process, thread, weight_ns, stack]` in capture order |

Rows refer to the identities of `profile.json`. The file is the offline
input for reports and for the Activation join of #316; it does not
reconstruct chronology from sorted values.
