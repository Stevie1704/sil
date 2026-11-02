# Profile report contract

[Documentation index](README.md) · [Spec #313](https://github.com/Stevie1704/sil/issues/313) · [perf proof](../proofs/profiling/README.md)

`sil profile` captures one profile of one fresh Run. It writes a profile
directory. The summary of that directory is one versioned JSON document,
`{"sil_profile_report": 1, ...}`, in `profile.json`. This page defines the
command, the directory and the document.

Status: the CPU collector (Linux perf) is implemented (#315), and so is
the optional Activation correlation of its samples (#316). The heap
collector (Heaptrack) is implemented (#317): see
[Heap collector](#heap-collector). `sil profile render` presents a saved
profile offline (#318), with CPU flame graphs, an Activation timeline and
per-Task drill-downs (#339): see [Offline report](#offline-report).
`sil profile diff` compares two saved sets: see [Profile diff](#profile-diff). The
[#319 qualification](../proofs/profiling-qualification/README.md) qualified
the installed native and the documented container workflows on Linux
x86-64; `sil docs support` names the qualified tool versions.

Valid and invalid examples of every case are in
[tests/fixtures/profile/](../tests/fixtures/profile/).
`sil.profile_report` validates the document (`load_profile_report`,
`parse_profile_report`, `validate_profile_report`). A reader must reject an
unknown version.

## Command

```sh
sil profile MANIFEST --out DIR --collector cpu
    [-o RECORDING | --no-recording] [--provenance PATH]
    [--provenance-mode full|external]
    [--participant-timeout-ms N] [--max-protocol-line-bytes N]
    [--max-step-output-messages N] [--max-step-inline-payload-bytes N]
    [--frequency-hz N] [--stack-bytes N] [--max-capture-bytes N]
    [--symbolization-timeout-s N] [--modules-from PATH]...
    [--activations [--max-activation-bytes N]]

sil profile MANIFEST --out DIR --collector heap (--runner | --participant NAME)
    [-o RECORDING | --no-recording] [--provenance PATH]
    [--provenance-mode full|external]
    [--participant-timeout-ms N] [--max-protocol-line-bytes N]
    [--max-step-output-messages N] [--max-step-inline-payload-bytes N]
    [--max-capture-bytes N] [--symbolization-timeout-s N]
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--out DIR` | required | The profile directory. It must not exist; its parent must exist. |
| `--collector` | required | `cpu` or `heap`. |
| `-o`, `--no-recording`, `--provenance` | `DIR/run.mcap` | As `sil run`. A relative path resolves against the current directory. With `--no-recording` the provenance record is `DIR/run.provenance.json`; a failure report is `out.mcap.failure.json` in the current directory, as with `sil run`. |
| `--provenance-mode` | `full` | As `sil run` ([provenance mode](running.md#provenance-mode)). It controls the runner only. See [Provenance mode and profile integrity](#provenance-mode-and-profile-integrity). |
| the four guards | as `sil run` | Passed to the runner unchanged and recorded. |
| `--frequency-hz` | 999 | perf sampling frequency (`-F`). |
| `--stack-bytes` | 8192 | User stack bytes that perf copies per sample for DWARF unwinding. |
| `--max-capture-bytes` | 1073741824 (1 GiB); heap: 268435456 (256 MiB) | Limit for `perf.data`; heap: for `heap/heaptrack.data`. |
| `--symbolization-timeout-s` | 600 | Limit for each `perf script` pass; heap: for the interpreter to finish after the Run. |
| `--modules-from PATH` | none | A directory, an ELF file or a zip archive (an FMU) with copies of modules that are gone after the Run. Repeatable. |
| `--activations` | off | Also record the host interval of each Activation and join it with the samples ([Activation correlation](#activation-correlation)). |
| `--max-activation-bytes` | 67108864 (64 MiB) | Storage limit of the observation stream. Only with `--activations`. |

`--participant NAME` and `--runner` select the scope of the heap
collector. With `--collector cpu` they are a usage error. `--frequency-hz`,
`--stack-bytes`, `--modules-from`, `--activations` and
`--max-activation-bytes` are options of the CPU collector; with
`--collector heap` they are a usage error.

One command does one fresh Run with one collector. It does not repeat the
Run, retry, change the collector or the event, or change a host setting.

### What the command does

1. **Preflight.** It checks every output path. Each path must be new and
   must not be an alias of the Manifest, the Recording, the provenance
   record or the failure report. It finds `sil-run-diagnostics`,
   `sil-diagnostics-launcher` and `perf` on `PATH`. It probes perf with the
   exact event and options on `true`. A failed probe stops the command
   before any Participant starts.
2. **Run.** It starts

   ```sh
   perf record -e cpu-clock:u -F 999 -k monotonic \
       --call-graph dwarf,8192 --buildid-mmap -N -q \
       --control fifo:DIR/cpu/.control -o DIR/cpu/perf.data -- \
     sil-diagnostics-launcher --status-fd N --environment-fd M \
       sil-run-diagnostics MANIFEST \
       -o RECORDING --diagnostics-out DIR/run.diagnostics.json \
       --retain-run-directory DIR/cpu/.run-directory [guards] \
       [--activation-observations DIR/activation/observations.jsonl \
        --activation-observations-limit-bytes N]
   ```

   in a new session, in the current directory. perf changes the environment
   of the command it starts. perf 6.x sets `PERF_BUILDID_DIR` and `PREFIX`,
   empties `DEBUGINFOD_URLS` and puts its own directory first on `PATH`.
   Thus the command gives its own environment to the launcher on descriptor
   M, and the runner gets that environment unchanged. `-N` keeps perf from
   filling the build-ID cache of the user (`~/.debug`). Thus the profile
   uses no file outside `DIR`, except the host modules that it names. perf
   follows every descendant of the runner (`inherit`), so the scope is the
   runner and all its descendants. The Manifest bytes, the working directory, the Participant
   commands and their environment do not change.
3. **Limit.** While the Run executes, the command reads the size of
   `perf.data` and the free space of `DIR` every 100 ms. It sends `disable`
   to perf's control FIFO at the size limit, or when the free space falls
   below the reserve (see "Free-space reserve"). Sampling stops, and the
   Run continues. The file can be larger than the limit by the data of one
   interval.
4. **Modules.** At the end of the Run, the runner keeps its Run working
   directory as `DIR/cpu/.run-directory`. perf
   records modules with a build ID (`perf buildid-list`). When the file of
   such a module is gone or has another build ID, the command looks for a
   copy with the same build ID. It looks first in `.run-directory`, then in
   the `--modules-from` sources. It adds each copy to the build-ID cache
   `DIR/cpu/buildid-cache`, then removes `.run-directory` and the unpacked
   archives. A Participant that removes its own files at shutdown leaves no
   copy. The FMI importer removes its extracted FMU, so name the FMU archive
   with `--modules-from`. Without a copy, the samples of a module keep its
   path, build ID and module offsets, and have no function name.
5. **Export.** `perf script` writes the samples and the task, mmap and lost
   events (`-F comm,pid,tid,time,period,ip,sym,dso --ns --inline
   --show-task-events --show-mmap-events --show-lost-events`). A second
   pass gives the source line of each leaf frame
   (`-G -F ip,sym,srcline --no-inline`). The analysis commands use
   `perf --buildid-dir DIR/cpu/buildid-cache` and run without
   `DEBUGINFOD_URLS`, so no symbol is fetched from the network. Each pass
   runs alone (no parallel symbolization), for at most
   `--symbolization-timeout-s`, and its output is read up to the export
   limit: twice `--max-capture-bytes`. Above that limit the pass is
   stopped. For the sample pass that is `export_failed` (no
   `samples.json`; `perf.data` stays); for the source-line pass, the
   samples have no source lines. The command reads the sample pass line
   by line into its normalized form, so it does not keep that text. It
   keeps the text of the source-line pass, and the normalized form, in
   memory.
6. **Join.** With `--activations`, it joins the observation stream with
   the samples (below) and writes `activation/correlation.json`.
7. **Publish.** It writes `cpu/samples.json`. It writes `profile.json`
   through a temporary sibling and a hard link. Thus a reader never sees a
   partial summary, and nothing is replaced. It prints a summary on stdout:
   the statuses, the identities, the scope and method, the coverage and
   loss, the limitations, and then the top 10 rows with the remainder. A
   control character in a report value is printed as its escape
   (`\x1b`), so a name cannot change the terminal.

SIGINT, SIGTERM and SIGHUP go to the whole session. The runner records the
interruption and cleans up as without a profiler. perf stops sampling and
finalizes `perf.data`. perf ignores SIGHUP, so it cannot end before the Run.

### Free-space reserve

Both collectors keep a reserve of 8 MiB (8388608 bytes) of free space in
`DIR`. The reserve is for the diagnostics report, `cpu/samples.json` or
`heap/allocations.json`, and `profile.json`. When the free space of `DIR`
falls below the reserve, the collector stops its raw data. The Run
continues. The capture gets the reason `storage_reserve_reached`; it is
`partial` or `failed`, and the command exits 1 after a successful Run.
Thus a full disk does not stop the Run, and `profile.json` keeps the Run
outcome.

Before the Run, the command compares `--max-capture-bytes` with the free
space of `DIR`. When the limit plus the reserve is more than the free
space, it writes a warning on stderr. The capture then stops at the
reserve, not at the limit.

The reserve is not a quota. Another program that writes to the same file
system can still fill it. When `profile.json` cannot be written, the
command writes the error on stderr and exits 1, or with the Run's exit
code when the Run failed.

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
| The Run succeeded, capture `complete`, `--activations` correlation not `complete` | 1 | written |
| The Run succeeded, the summary cannot be written | 1 | not written |
| Hard crash or SIGKILL of the command | signal status | not written; raw files can remain |

Exit 127 for a missing `sil-run-diagnostics` or launcher is the convention
of `sil diagnostics run`: the shell code for "command not found".

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
| `activation/observations.jsonl` | With `--activations`: the runner's observation stream (below). Authoritative. |
| `activation/correlation.json` | With `--activations`, when the join is proven: the class of each sample and each Activation's interval and counts (below). |
| `cpu/perf-script.stderr.txt`, `cpu/perf-srcline.stderr.txt`, `cpu/perf-buildid-cache.stderr.txt` | The last 64 KiB of each export's output on stderr; for the cache, of each `perf buildid-cache -a`. A copy that perf did not add is not captured. |
| `heap/...` | With `--collector heap` in place of `cpu/`: see [Heap files](#heap-files). |

Raw stack copies can hold any data that was on a stack, also secrets. Share
`perf.data` only as you would share a core dump. `samples.json` and
`profile.json` hold no stack bytes, no environment variable and no Message
payload. A copy of `profile.json` and `cpu/samples.json` is sufficient for an
offline report; with `--activations`, also `activation/correlation.json`
([Offline report](#offline-report)).
Re-symbolization needs `perf.data` and the modules. The captured modules
are in `cpu/buildid-cache`. The other modules stay on the host, and only
their path and build ID identify them.

## Conventions

The conventions of the [diagnostics contracts](diagnostics-report.md#conventions)
apply. Member names have units. There is no NaN or infinity. A null has a
reason in an `unavailable` map. There is no Message payload or environment
variable.

- A value that can be above 2^53 is a string. Absolute timestamps
  (`*_ns` that are host `CLOCK_MONOTONIC` values) are decimal strings.
  Addresses are `0x` hexadecimal strings. Times relative to
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
| `unverifiable_debug_file` | The module has no recorded build ID, so no debug data can be matched to it |
| `symbolization_timeout` | A `perf script` pass took longer than the limit |
| `inlined_frames_failed` | `perf script --inline` failed (perf 6.1 can end on its `addr2line` helper). The sample pass ran again with `--no-inline`, so an inlined function is part of its caller's frame |
| `export_failed` | A `perf script` pass failed, its output did not decode, or it was larger than the export limit. Or `cpu/samples.json` could not be written |
| `no_samples` | The capture has no sample to describe |
| `not_requested` | The option or collector was not requested |
| `reserved` | Reserved for a later ticket; not measured by this version |

Reasons of `activation_correlation` (#316):

| Reason | Meaning |
| --- | --- |
| `observations_missing` | The runner wrote no observation stream (the Run did not start) |
| `observations_corrupt` | The stream has no valid header, or a line that does not decode in a stream that reports no write failure |
| `observations_truncated` | The storage limit stopped the stream; later records are dropped |
| `observations_write_failed` | A write to the stream failed; later records are dropped |
| `observations_dropped` | A record could not be made (no memory) |
| `observations_unterminated` | The stream has no end line: the runner did not end normally |
| `clock_mismatch` | The stream or the capture is not host `CLOCK_MONOTONIC`, or an observation lies outside the runner's lifetime as perf saw it |
| `runner_not_observed` | The capture has no runner process with the PID that the stream names |
| `pid_namespace_mismatch` | The runner and the command are in different PID namespaces |
| `pid_namespace_unverified` | The PID namespace of the runner or of the command is unknown (not Linux) |
| `no_samples_file` | The capture has samples, but `cpu/samples.json` was not written |
| `process_unmapped` | A Process participant's spawn has no unique perf process |

## `sil_profile_report` v1

| Member | Content |
| --- | --- |
| `producer` | `name` (`sil profile`), `sil_version` |
| `identity` | `manifest_path` as given, `manifest_hash`, `sil_version`, `source_repository`, `source_revision` (from the diagnostics report; null with `no_diagnostics_report`) |
| `host` | `os`, `kernel_release`, `architecture`, `perf_event_paranoid` (null when not readable) |
| `configuration` | `collector`, `recording` (null for `--no-recording`), `provenance_mode`, `participant_timeout_ms` and the three guards (null when not given), `cwd` |
| `run` | The Run outcome, below |
| `capture` | The collector variant: `kind` `cpu` or `heap` |
| `activation_correlation` | `kind` `not_requested` or `activation` |
| `artifacts` | List of `{role, path, bytes, sha256}`: every file the directory holds that `profile.json` relies on; `path` relative to `DIR` |
| `limitations` | Fixed statements that apply to every value, below |

### Provenance mode and profile integrity

Provenance hashing is part of the measured runner work, so a capture records
its effective mode in `configuration.provenance_mode`. `sil profile render`
shows it, and `sil profile diff` warns when two sets differ.

| `provenance_mode` | Meaning | Shown as |
| --- | --- | --- |
| `full`, `external` | The effective mode of this capture | The value |
| Not present | A set from before the option. Those Runs used `full`. | `full (report predates the option)`; the diff compares it as `full` |
| null | The mode cannot be established | `unknown` |

Report version 1 stays valid, because the member is optional. A reader never
infers `external` from a missing digest.

`external` omits runner provenance hashing only. The profile tool still
calculates and verifies each digest in `artifacts`, and `sil profile render`
still rejects changed normalized evidence. A Recording in the profile directory
is an artifact, so the profile tool hashes it after the Run, also in `external`
mode. A `sha256` in `artifacts` is a digest of the profile tool. It is not a
digest of the runner, and the side-car still shows `omitted`. So `external` does
not make a profile command free of hash work. A Recording outside the profile
directory is not an artifact, as before.

#### The artifact inventory

The profile tool builds `artifacts` once, after normalization and after the
optional Activation correlation. Thus it reads and hashes each file once per
capture. It keeps no digest between commands.

#### No Recording digest handoff

The profile tool does not reuse the digest of the runner (#323, section 6). It
hashes the Recording itself, with one read in bounded memory, in all modes. The
ticket allows this result if a trustworthy handoff cannot be established.

A handoff must show four facts. The profile tool can show three of them:

| Fact | Available | Why |
| --- | --- | --- |
| This invocation | Yes | The profile tool starts the runner and selects the side-car path. |
| The exact output | Yes | The profile tool selects the Recording path. |
| Successful finalization | Yes | The side-car and the exit code give it. |
| The file is unchanged until profile finalization | **No** | A Recording stays writable after the Run while `perf` and the analysis run. Only path, size and modification time could be compared, and these do not prove content identity. |

An entry in `artifacts` is the integrity claim of the profile set. If the profile
tool copied a digest, a change between the runner close and profile finalization
would give a claim about bytes that are not in the file. The cost is small
compared to the work that it protects: one read of the Recording, in a command
that also exports and normalizes a capture.

| Case | `recording` in the side-car | `sha256` of the `recording` artifact |
| --- | --- | --- |
| `full`, file unchanged | `calculated` | Calculated apart, and equal |
| `external` | `omitted` | Calculated by the profile tool |
| Failed Run with a partial Recording | The digest of the partial file | Calculated by the profile tool, of the same partial file |
| `--no-recording`, or a Recording outside the directory | Not related | No artifact |

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
| `reasons` | Subset of `lost_samples`, `capture_limit_reached`, `storage_reserve_reached`, `interrupted`, `backend_failed`, `export_failed`, `run_not_started` |
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
| `samples_file` | `cpu/samples.json`, or null. A `complete` or `partial` capture always has the file. When the file cannot be written, the capture is `failed` with `export_failed` |
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
| `samples_with_caller` | Samples with at least two physical frames: the unwinder found a caller |
| `lost_samples` | Samples that perf reports lost (`PERF_RECORD_LOST`, `PERF_RECORD_LOST_SAMPLES`); null when a lost record has no count |
| `lost_records` | The number of lost records |

`symbolization`:

| Member | Content |
| --- | --- |
| `function_names` | `samples_with_name`: leaf frames with a function name |
| `call_frame_unwinding` | `method` (`dwarf`), `samples_with_caller` |
| `source_lines` | `status` (`available`, `partial`, `unavailable`), `scope` (`leaf_frames`), `samples_with_line` (null for `unavailable`), `unavailable` |
| `inlined_frames` | `status` `available`: the export uses `--inline`. `samples_with_inlined_frames` counts the samples with at least one inlined frame. `unavailable` with `inlined_frames_failed` when the inline export failed and the samples come from a `--no-inline` export. `unavailable` when there is no export. A version 1 report of an earlier version can have `not_requested` |

An inlined function is its own frame: it has the address and the module of
its physical frame and the mark `inlined`. In a stack, the inlined frames
come before their physical frame, innermost first. perf can mark every function of an address as inlined; then the module is the one mapped at that address. A `self` row gives the
sample to the innermost frame. An `inclusive` row counts a function once per
sample. So no total counts a sample twice. `samples_with_caller` counts
addresses only: an inlined function is not a caller.

Function names and DWARF call frames are separate capabilities. A module
can unwind and have no names (a stripped library). A module can also have
names and no source lines.

A `processes` row:

| Member | Content |
| --- | --- |
| `id`, `pid` | Identity and kernel PID |
| `parent` | Identity of the parent, or null when the parent is outside the capture |
| `role` | `launcher` (perf's workload), `runner` (the launcher's child), `descendant` |
| `commands` | The command names (`comm`, at most 15 bytes each) in order; an `exec` adds one |
| `execs` | Number of `exec` events. An `exec` keeps the process identity and its Activation mapping |
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
| `material` | `host_file`: the file at `path` has the build ID. Without a recorded build ID, it is the file at `path`, and nothing verifies that it is the module that ran. `captured_run_directory` or `captured_modules_from`: `cpu/buildid-cache` has a copy with the build ID. The copy is from the retained Run working directory or from a `--modules-from` source. `unavailable`: no file with the build ID. |
| `debug` | `status` (`embedded`, `separate_matching`, `separate_mismatched`, `not_found`, `unverifiable`, `module_unavailable`) and `path` of the matching debug file or null. A module without a recorded build ID is always `unverifiable`: its debug data gives no source line |
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

### `capture`, kind `heap`

| Member | Content |
| --- | --- |
| `kind` | `heap` |
| `status` | `complete`, `partial`, `interrupted`, `failed`, `not_started`, `unavailable` |
| `reasons` | Subset of `capture_limit_reached`, `storage_reserve_reached`, `data_truncated`, `data_may_be_incomplete`, `interrupted`, `backend_failed`, `export_failed`, `no_data`, `run_not_started`, `unsupported_scope`, `reserved`. This version never writes `unsupported_scope` or `reserved`; they are valid in documents of other versions |
| `backend` | `name` (`heaptrack`), `version` (`heaptrack --version`, or null), `preload` and `interpreter` (the paths used), `interpreter_exit_code` (null when the command stopped it) |
| `scope` | `kind` `runner`; or `kind` `process_participant` with `participant`; or `kind` `unsupported` with `requested` and `reason` |
| `launch` | `method` (`preload_exec_adapter`), `adapter` (`heap/launch-adapter.sh`), `wrapped` (`runner` or `process_participant`) |
| `limits` | `max_capture_bytes`, `capture_bytes`, `limit_reached`, `interpreter_timeout_s` |
| `activation_correlation` | Always `{"status": "unavailable", "reason": "no_validated_clock_mapping"}` |
| `raw` | `heap/heaptrack.data`, or null |
| `analysis` | Below, or null |
| `unavailable` | Reasons for null members |

`complete` and `partial` have an analysis. The other statuses can have one
when the data decodes. `backend`, `launch`, `limits` and
`activation_correlation` came with #317; a reserved heap capture of #315
(status `unavailable`, reason `reserved`) has none of them and stays valid.
The scope `unsupported` is a valid record of a request that a version cannot
capture. This version does not write it: it rejects such a request before
the Run (exit 2).

Heap reasons:

| Reason | Meaning |
| --- | --- |
| `capture_limit_reached` | The data reached `--max-capture-bytes`; later data is dropped |
| `storage_reserve_reached` | The free space of `DIR` fell below the reserve; later data is dropped |
| `data_truncated` | The last record of the data is cut, or the data file could not be written (a full disk). The rest of the data is dropped, and the Run goes on. |
| `data_may_be_incomplete` | Heaptrack writes its last data at normal process exit. The runner ended by a signal (runner scope), or the Run did not succeed and the runner stopped the participant (Process scope) |
| `backend_failed` | The interpreter failed, or did not end within its time limit |
| `export_failed` | The data is not Heaptrack's interpreted file version 3, a record is malformed, or its stacks do not resolve. Or `heap/allocations.json` could not be written |
| `no_data` | Heaptrack wrote no data: the scope process did not start |

`analysis`:

| Member | Content |
| --- | --- |
| `file_version` | 3 |
| `heaptrack_version` | The version in the data file, for example `1.5.0` |
| `traced_executable` | The command line that Heaptrack recorded for the traced process; it shows which process the data is from |
| `allocations_file` | `heap/allocations.json`, or null. A `complete` or `partial` capture always has the file |
| `totals` | Below |
| `coverage` | `allocations_unknown_module`, `allocations_unknown_symbol` (the innermost frame has a module and no function name), `allocations_with_caller` (two or more frames), `undecodable_records`, `truncated` |
| `modules` | `id`, `path`, `build_id` (or null), `debug`, `allocations_leaf` (allocations whose innermost frame is in the module), `allocations_unknown_symbol`, `unavailable`. `debug` is null with `not_reported_by_backend`: Heaptrack does not report which debug file it used. A report of an earlier version can omit `debug` |
| `hotspots` | `limit` (50), `self`, `self_remainder`, `inclusive` (below) |

A `hotspots` row: `module` (identity or null), `symbol` (or null),
`allocations`, `allocated_bytes`, `temporary_allocations`,
`outstanding_allocations`, `outstanding_bytes`, `source_lines` (at most 5
`{file, line, allocations}` of the innermost frame). `self` keeps the union of
the top `limit` rows by `allocated_bytes`, `allocations`,
`temporary_allocations` and `outstanding_bytes`, ordered by
`allocated_bytes`. `self` plus `self_remainder` equals `totals` for each of
the five metrics. `inclusive` keeps rows the same way; inclusive values
overlap, so they do not add up.

See [Heap collector](#heap-collector) for the metrics and their boundaries.

### `activation_correlation`

- `{"kind": "not_requested"}`: the command without `--activations`.
- kind `activation`: `status` (`complete`, `partial`, `unavailable`,
  `failed`), `reasons`, `clock` (`host_monotonic`, or null when the join is
  not proven), `observations` (`activation/observations.jsonl`, or null),
  `summary` (below, or null) and `unavailable`.

`complete` and `partial` have a summary; `unavailable` and `failed` have
none, and then no sample is related to an Activation. `partial` names what is
missing: a cut stream or an unmapped process. Sampling and observation have
separate completeness: a `complete` correlation of a `partial` capture
relates only the samples that perf kept.

See [Activation correlation](#activation-correlation) for the join.

### `limitations`

The heap capture always lists `heap_tracks_allocation_calls_only`,
`outstanding_is_not_a_leak_verdict`, `allocation_origin_is_not_ownership`,
`activation_correlation_unavailable` and `host_build_only`
([Heap collector](#heap-collector)).

The CPU capture always lists:

- `cpu_samples_estimate_on_cpu_time`: weights are sampled estimates, not
  exact durations or call counts.
- `user_space_only`: `:u` samples no kernel code.
- `scope_is_runner_and_descendants`: other processes are not sampled.
- `host_build_only`: the profile describes this host build, not a target ECU
  or a worst-case execution time.

## Heap collector

`--collector heap` captures the allocations of one process with
[Heaptrack](https://github.com/KDE/heaptrack). The
[#314 proof](../proofs/profiling/README.md) qualified Heaptrack 1.4 and 1.5
on Linux x86-64/glibc for the runner and for an unshimmed Process
participant.

### Scope

One command captures one scope:

- `--runner`: the runner process. Every Native participant runs in it, so
  their allocations are in one capture, together with the runner's own work
  (routing, Recording, setup and shutdown). A Native participant cannot be a
  scope by name: `--participant` with a Native participant is a usage error
  (exit 2). Use `--runner` and read the sites by module and function.
- `--participant NAME`: one Process participant. An FMU group is one Process
  participant, so it is one scope.

These selections are usage errors (exit 2) before the Run: an unknown name, a
Native or Replay participant, and a Process participant with `shim: true`.
Heaptrack in a shimmed participant is not qualified (#314). It can stop the
participant. It removes the shim from the children of the participant. Its
timer writes data without a bound. Issue
[#330](https://github.com/Stevie1704/sil/issues/330) investigates it. The
Clock shim of the other participants is not affected. In the runner scope,
the runner sets the shim of a shimmed participant itself, after Heaptrack.

Heaptrack removes `LD_PRELOAD` and `DUMP_HEAPTRACK_OUTPUT` from the process
it traces when it loads. Thus no child or sibling of the scope process is
traced, and the allocations of a child are not in the capture. Heaptrack
does not trace the copy of a process between `fork` and `exec`. Heaptrack
removes all of `LD_PRELOAD`. Thus the command stops in preflight (exit 1)
when `LD_PRELOAD` is set in its environment. If not, the processes of the
Run would start with another environment.

### What the command does

1. **Preflight.** It checks the output paths as for the CPU collector. It
   checks the scope against the Manifest (exit 2). Then it checks the host
   (exit 1). The host must be Linux and must have `heaptrack` on `PATH`. It
   must have the preload library and the interpreter, from the
   `LIB_REL_PATH` and `LIBEXEC_REL_PATH` lines of the `heaptrack` script.
   Last, a probe: `true` with the preload library must write data.
2. **Adapter.** It writes `DIR/heap/launch-adapter.sh`. The adapter sets
   `DUMP_HEAPTRACK_OUTPUT` to the FIFO `DIR/heap/.fifo`. It puts the preload
   library before any `LD_PRELOAD`. `sh` sets `PWD` when it starts, so the
   adapter sets `PWD` back to the value of the command, or removes it. Then
   it execs its arguments. The process keeps its PID, pipes, working
   directory, arguments and all other environment. The runner executes the
   adapter from `DIR/heap`, so `DIR` must be on a file system that allows
   execution. On a `noexec` mount, the launcher (runner scope) or the runner
   (Process scope) cannot start it, and no Run takes place.
3. **Interpreter.** It starts `heaptrack_interpret < DIR/heap/.fifo` in a
   session of its own, outside the Run, without `DEBUGINFOD_URLS`. Its output
   goes to `DIR/heap/heaptrack.data` up to `--max-capture-bytes`, and while
   the free space of `DIR` is above the reserve. The command reads and drops
   later data, so the traced process never waits for it. The
   last 64 KiB of its stderr go to `DIR/heap/interpret.stderr.txt`. It
   resolves symbols while the Run executes, so a module in the Run working
   directory (an extracted FMU) is named before it is gone.
4. **Run.** For the runner scope:

   ```sh
   sil-diagnostics-launcher --status-fd N DIR/heap/launch-adapter.sh \
     sil-run-diagnostics MANIFEST -o RECORDING \
       --diagnostics-out DIR/run.diagnostics.json [guards] \
       --retain-run-directory DIR/heap/.run-directory
   ```

   For a Process participant, the runner is not wrapped. It gets
   `--launch-wrapper NAME DIR/heap/launch-adapter.sh`. The diagnostics
   runner then starts that participant as `ADAPTER COMMAND...` instead of
   `COMMAND...`. It does this in the child that it forked, after its own
   setup of the process group, the pipes, the working directory and the
   environment. The
   option is in `sil-run-diagnostics` only; `sil-run` rejects it. The
   Manifest bytes, its hash, the Native ABI and the Step protocol do not
   change.
5. **Data end.** When the Run ends, the command opens and closes the FIFO
   for writing. Thus the interpreter reads the end of the data, also when
   its scope process did not start. It waits for the interpreter at most
   `--symbolization-timeout-s`, and stops it after that.
6. **Normalize.** It reads `heaptrack.data` (below) and writes
   `DIR/heap/allocations.json`. It reads the build ID of each module file:
   at its path, or in the retained Run working directory. Then it removes
   the retained directory.
7. **Publish.** It writes `profile.json` last, as for the CPU collector, and
   prints a summary.

SIGINT, SIGTERM and SIGHUP go to the launcher, which forwards them to the
runner. The runner records the interruption and cleans up as without a
profiler. The interpreter is not in the Run's session: it ends when the data
ends. A signal while the command waits for the interpreter stops the
interpreter.

The exit codes are those of the CPU collector, with these additions. A scope
that cannot be captured is exit 2. These are exit 1 before any Participant
starts: a host without Heaptrack, a failed probe, a host that is not Linux,
and a set `LD_PRELOAD`. After a successful Run, a capture that is not `complete`
gives exit 1.

### Heap files

| Path | Content |
| --- | --- |
| `heap/heaptrack.data` | The raw evidence: Heaptrack's interpreted data file, format version 3, uncompressed. `heaptrack_print` and `heaptrack_gui` read it. It holds allocation stacks, sizes, module paths and the traced command line; no memory content. |
| `heap/allocations.json` | Normalized allocations: the offline input for reports (below). |
| `heap/launch-adapter.sh` | The adapter of this capture. |
| `heap/interpret.stderr.txt` | The last 64 KiB of the interpreter's stderr. |

A copy of `profile.json` and `heap/allocations.json` is sufficient for an
offline report ([Offline report](#offline-report)). It needs neither
Heaptrack nor the profiled binaries.

### Metrics and their boundaries

The data covers the whole life of the scope process under Heaptrack. It
starts at the load of the preload library and ends at the last data that
Heaptrack wrote. For
the runner scope that is before the Manifest loads to after the Recording
closes, so it includes setup, shutdown and Recording work. Each value counts
each allocation event once.

| Metric | Meaning |
| --- | --- |
| `allocations` | Calls of the allocation functions that Heaptrack interposes (`malloc`, `calloc`, `realloc`, `posix_memalign`, `aligned_alloc` and the others in its preload library). A `realloc` that moves is one free and one allocation. C++ `new` counts through `malloc`. |
| `allocated_bytes` | The requested sizes of these allocations, summed. It is not allocator overhead and not live memory. |
| `frees` | Frees of tracked allocations. |
| `temporary_allocations` | Frees that directly follow the allocation of the same allocation info (the same stack and size), with no other allocation or free between. This is Heaptrack's definition on its data file. `heaptrack_print` also counts a free of the first allocation info after any free; `heaptrack_interpret`'s own statistics compare addresses and can be lower. |
| `outstanding_allocations`, `outstanding_bytes` | Allocations not freed at the end of the data: a snapshot at capture end. They are not a leak verdict: the process can keep memory on purpose until it exits. |
| `peak_tracked_bytes` | The largest sum of outstanding requested bytes at any record of the data. This is tracked heap: not RSS, not stack, not mapped pages, not allocator overhead. |

A `self` row gives each allocation to its innermost frame. That is the
first frame that Heaptrack records: the function that called the
allocation function. For C++ `new`, it can be `operator new` in the C++
library. An inlined function is its own frame. An `inclusive` row counts
each function in the stack of an allocation once per allocation. This is
also true when the function is in the stack more than one time (recursion). Inclusive values overlap and
are never totals.

### Coverage and limits

- Only the interposed allocation functions are seen. A custom pool shows
  as its few backing allocations, for example an arena allocator, a static
  buffer pool or `pymalloc` of Python. The logical allocations inside the
  pool are not seen.
  A test with real Heaptrack shows this: an arena that takes one block
  from `malloc` and gives out 160 objects shows as one allocation.
  Direct `mmap`, `brk` and stack memory are not tracked.
- An allocation site is where memory was allocated. It is not the owner of
  that memory now.
- Symbols: the interpreter finds a separate debug file through its build ID
  or `.gnu_debuglink`, as perf does. It refuses a debug file with another
  build ID. Then the allocations stay in the evidence with an unknown
  function. A library without debug information gives names for its
  exported functions only.
- Module identity: Heaptrack records module paths, no build IDs. The build
  ID in `modules` is that of the file after the Run. A module that its
  participant removed itself (the FMI importer removes its extracted FMU)
  has no build ID (`module_unavailable`); its names were resolved live.
- There are no memory totals per Native participant or per FMU instance, and
  no allocation history per Activation or Step. Activation correlation is
  unavailable: Heaptrack's timestamps are milliseconds since its own start,
  with no validated mapping to host time.

### Normalized allocations: `heap/allocations.json`

`{"sil_profile_heap_allocations": 1, ...}`:

| Member | Content |
| --- | --- |
| `frames` | List of `{module, symbol, address, source}`; `source` is `{file, line}` or null. An address with inlined functions gives one frame per function, innermost first. |
| `stacks` | List of frame index lists, innermost first |
| `allocations` | One `[stack, size_bytes, allocations, temporary_allocations, outstanding_allocations]` per Heaptrack allocation info (one stack and one size), in data order |

Rows refer to the module identities of `profile.json`.

## Activation correlation

`--activations` makes `sil-run-diagnostics` stream observation records
(only that build has the hooks; `sil-run` rejects the option). The command
joins them with the samples after the Run. The observations never change
the Run: not its Recording, its exit code, its stdout or a scheduling
decision.

### The join

The join is made only when it is proven:

- **Clock.** The runner reads `std::chrono::steady_clock`, which is
  `CLOCK_MONOTONIC` on Linux, and perf records with `-k monotonic`. The
  join checks that both state `host_monotonic` and that every observation
  lies inside the runner's lifetime in the capture. The Clock shim does not
  change these clocks: the runner is never shimmed, and perf takes its
  timestamps in the kernel. The intervals of a Process Step come from the
  runner, never from the shimmed participant.
- **PID namespace.** The runner records the identity of its PID namespace
  (`/proc/self/ns/pid`), the command compares it with its own. perf runs in
  the command's namespace.
- **Process identity.** A Process participant is the one perf process that
  the runner forked with the spawn record's PID between the spawn and the
  reap. A reused PID is a different perf identity, so it does not match. Its
  descendants belong to it through their parent chain; an `exec` does not
  change the identity.

Intervals are half open, `[begin_ns, end_ns)`. Each sample is in exactly one
class:

| Class | Sample | Meaning |
| --- | --- | --- |
| `execution_context` | runner thread, inside a Native activation or a Process Step | The code ran in that Activation, inclusive: routing and Recording work that the Task called is part of it. For a Process Step it is the runner's own side of the Step exchange, inclusive. It is never the work of the participant's process. |
| `window_associated` | a Process participant's process tree, inside one of its Steps | It ran while the runner waited for that Step. It is not proven to be the Step's own work. |
| `lifecycle` | runner thread, in a lifecycle phase, outside every Activation | Preflight, setup, Slot scheduling, shutdown, Recording close. |
| `replay` | runner thread, in a Replay publication Slot | Replay work, not a Task. |
| `unattributed` | everything else, with a reason | See below. |

Unattributed reasons:

| Reason | Meaning |
| --- | --- |
| `process_outside_step` | The process of a Process participant between its Steps: start-up, shutdown or asynchronous work |
| `runner_other_thread` | A runner thread that runs no Activation, for example a thread of a Native participant |
| `runner_outside_intervals` | The runner thread before the first phase or after the last phase |
| `unmapped_process` | A descendant of the runner that is not in the process tree of a mapped participant |
| `launcher` | The launcher process |
| `observations_incomplete` | A sample of the runner thread or a Run descendant at or after the last kept record of a cut stream. Or, in a cut stream, a runner-thread sample outside every kept interval: the runner writes a record at its end, so a dropped interval can begin earlier |

No identity comes from a symbol: a Native library that two Participants
share is told apart by the runner's interval only. An FMU group is one
Process participant; no sample is related to one FMU instance. No class is
an exclusive, causal or ownership attribution, and a sample weight is not an
Activation's duration. An Activation can have no sample: it is listed with
`samples` 0, which is not evidence that it used no CPU time.

### Observation stream: `activation/observations.jsonl`

One JSON object per line. Host times (`*_ns` except `virtual_time_ns`) are
decimal strings of host `CLOCK_MONOTONIC`.

| `kind` | Members |
| --- | --- |
| header (first line) | `sil_activation_observations` (1), `clock` (`host_monotonic`), `runner_pid`, `runner_tid`, `limit_bytes`, `pid_namespace` (`{device, inode}` or null) |
| `activation` | `participant`, `task`, `task_kind` (`native_task`, `process_step`), `index` (zero-based per Task), `virtual_time_ns`, `begin_ns`, `end_ns`, `outcome` (`completed`, `failed`, `interrupted`), `tid` |
| `replay` | As `activation`, without `task` and `task_kind`: one Replay publication Slot |
| `phase` | `phase` (`preflight`, `setup`, `scheduled_execution`, `shutdown`, `recording_finalization`), `begin_ns`, `end_ns` |
| `spawn` | `participant`, `pid`, `at_ns` (just before the fork) |
| `reap` | `participant`, `pid`, `reaped`, `at_ns` (after the reap) |
| `end` (last line) | `records`, `dropped`, `truncated`, `write_failed`, `bytes` |

A record is written when its interval closes, so the stream is in order of
end time. The runner keeps records in a 64 KiB buffer and writes it when it
is full and at the end of the Run. The file holds at most `limit_bytes` of
lines before the end line. Policy: the first record that does not fit, or
the first failed write, stops the stream; every later record is only
counted in `dropped`. The runner does not retry, and it never waits for a
reader: the file is a new regular file. The end line is the one line past
the limit. A stream without it is from a runner that did not end normally:
its last Activation can be missing.

### Correlation file: `activation/correlation.json`

`{"sil_profile_activation_correlation": 1, ...}`:

| Member | Content |
| --- | --- |
| `activations` | Every kept activation record in stream order: `participant`, `task`, `kind`, `index`, `virtual_time_ns`, `begin_ns`, `end_ns`, `outcome`, `samples`, `weight_ns` (of both `execution_context` and `window_associated`) |
| `replays` | Every kept Replay record, with `samples`, `weight_ns` |
| `phases` | `phase`, `begin_ns`, `end_ns` |
| `samples` | One `[class, ref]` per row of `cpu/samples.json`, in the same order. `ref` is the index in `activations` (`execution_context`, `window_associated`), in `replays` (`replay`), the phase name (`lifecycle`) or the reason (`unattributed`) |

### `summary`

| Member | Content |
| --- | --- |
| `interval` | `half_open` |
| `correlation_file` | `activation/correlation.json` |
| `stream` | `records`, `dropped` (null without an end line), `truncated`, `write_failed`, `terminated`, `limit_bytes`, `undecodable_lines` |
| `intervals` | Kept intervals: `activations` (Native), `process_steps`, `replay`, `phases` |
| `processes` | Per spawn: `participant`, `pid`, `process` (perf identity or null), `mapping` (`verified`, `not_observed`, `ambiguous`) |
| `samples` | `total` (equals `capture.totals`), one `{samples, weight_ns}` per class; `unattributed` also has `reasons` |
| `tasks` | Per Task: `participant`, `task`, `kind`, `activations`, `completed`, `failed`, `interrupted`, `with_samples`, `without_samples`, `execution_context`, `window_associated` |
| `lifecycle` | Per kept phase: `phase`, `samples`, `weight_ns` |

The classes add up to `total`, the reasons to `unattributed`, the Tasks to
their classes, and the phases to `lifecycle`.

## Offline report

```sh
sil profile render DIR/profile.json [--html OUTPUT.html]
```

The command reads `profile.json` and the normalized files that it names. It
prints the terminal summary of `sil profile` again and, with `--html`, writes
one standalone HTML file. It executes nothing: no Run, no profiled binary, no
perf and no Heaptrack. It never opens `perf.data`, `heaptrack.data`, a module
or a source file.

The first argument `render` selects this command, and `diff` selects the
comparison command below. To capture a profile of a Manifest file with one
of these names, write it as `./render` or `./diff`.

### Portable artifact set

Copy these files with their paths relative to `profile.json`:

| Profile | Files |
| --- | --- |
| CPU | `profile.json`, `cpu/samples.json` |
| CPU with `--activations` | also `activation/correlation.json` |
| Heap | `profile.json`, `heap/allocations.json` |

A file is needed only when `profile.json` names it (`capture.samples_file`,
`activation_correlation.summary.correlation_file`,
`capture.analysis.allocations_file`). A capture without such a file (for
example a Run that did not start) renders from `profile.json` alone. The raw
files (`perf.data`, `heaptrack.data`), the build-ID cache, the Recording and
the diagnostics report are not needed; keep them for re-analysis.

The command checks the set before it writes anything. Each named file must:

- exist, with the size and SHA-256 that `artifacts` records;
- have version 1;
- refer only to identities of `profile.json`;
- add up to its totals: sample count and weight, allocation count and
  bytes (including temporary and outstanding metrics), and one class per
  sample. Correlated samples must lie inside their referenced half-open
  host intervals; interval and Task counters must agree with those samples.

Each file of the set has a render limit of 268435456 bytes (256 MiB). A
larger file is refused before it is read (exit 2). The command holds each
file and its decoded document in memory, so its memory use grows with the
files. A capture within the default limits gives files far below 256 MiB.
The [#319 qualification](../proofs/profiling-qualification/README.md)
records the sizes of named workloads.

### Exit codes

| Situation | Exit code |
| --- | --- |
| The summary was printed and the HTML report, if requested, was written | 0 |
| The HTML report cannot be written, for example because the disk is full | 1 |
| Invalid usage; the output path exists (also the input itself) or its directory does not exist | 2 |
| `profile.json` or a named file is missing, corrupt, of another version, of another profile or inconsistent | 2 |

The command checks the output path first. It reads and renders nothing when
the path is not usable. An existing file is never replaced.

### Example

```console
$ sil profile render prof/profile.json --html prof.html
profile: prof/profile.json
run: succeeded, exit code 0
capture: cpu, complete
sampling: perf cpu-clock:u at 999 Hz, DWARF call graph (8192 bytes), the runner and all its descendants
samples: 10, weight 10.010 ms of sampled on-CPU time (an estimate, not a duration); lost 0
processes: 4 observed, 3 with samples
symbols: function names for 9 of 10 samples; source lines for 8 (leaf frames)
activations: complete
  of 10 samples: 5 in an Activation's execution context (runner thread), 3 in a Process Step window (associated, not proven to be Step work), 1 lifecycle, 0 Replay, 1 unattributed
  2 Activations have no sample: that is not evidence that they used no CPU time
top self weight (share of all sample weight):
   30.0%  silproof_native_spin_even (libsilproof_native.so)
   20.0%  silproof_fmu_spin (EgoMotion.so)
   ...
HTML report: prof.html
```

### The HTML report

The page opens from `file://` with no other file, no network request and no
server. Its policy allows only its own inline style and script. Sections, in
order:

1. **Status.** The Run outcome, the capture outcome and the Activation
   correlation, each with its reasons, and the first failure of the Run.
   They are independent: a capture problem never replaces the Run's exit
   code.
2. **What was measured.** Manifest and build identity, host, collector and
   version, event, sampling, weight unit, clock, call graph, scope and
   limits. Heap: scope, launch, preload library, interpreter and data file
   version.
3. **Coverage and loss** (CPU). The denominators (samples, sampled weight),
   lost samples, unknown modules and functions, unwinding and source lines.
   Zero samples is stated as no evidence of zero CPU use.
4. **Limitations.** The `limitations` of the report, as text.
5. **Results.** CPU:
   - functions by self weight: the top 50 of `profile.json`, with the
     remainder and the total in the table footer;
   - functions by inclusive weight: the top 50, and the number of
     functions that are not listed. Inclusive rows overlap, so this table
     has no remainder row and no total;
   - the top-down caller tree and flame graph (icicle) of `cpu/samples.json`;
   - the Activation evidence: samples by class, unattributed reasons,
     Tasks, lifecycle phases, participant-to-process mapping, the
     observation stream and the first 500 Activations, also those with no
     sample;
   - an Activation timeline, with Native activations, Process Steps, Replay
     Slots and lifecycle phases in separate Participant/Task lanes;
   - per-Task drill-downs with Activation counts, zero-sample Activations,
     self/inclusive hotspots and a caller tree for each sample class;
   - processes, threads and modules.

   Heap:
   - totals with their boundaries;
   - allocation sites by bytes, churn (temporary allocations) and
     retention (outstanding bytes at capture end), each the top 25 with
     the remainder and the total;
   - sites by inclusive bytes, with the number of functions that are not
     listed and no remainder row (the rows overlap);
   - the allocation caller tree and the modules.

Bounds: the caller tree shows a node only with at least 0.5% of the weight
of the tree. It shows at most 20 children per node, 64 levels and 1500
nodes. The other
nodes of a level are one visible remainder line with their count and weight,
so each level still adds up. Recursion gives deeper nodes; no sample counts
twice in one node.

The flame graph uses the same node limits, with an additional visible box
for a remainder or a stack ending (at most 4501 boxes including the root).
An ending may be a cut-off stack: retained evidence cannot distinguish an
unwinding limit from a true root. Unknown functions and empty stacks retain
their weight. Each sample contributes its weight once to each caller-path
node, including inlined frames. Width is sampled weight; the total and its
denominator are printed above the graph. Click or focus a frame and press
Enter/Space to zoom; reset returns to the whole selection. Search highlights
visible matching function names and reports its bounded scope. Long names
are shortened inside boxes; hover or focus gives the full name.

The timeline shows at most 500 intervals and lanes belonging to them, with
the omitted interval count and sample counters visible below it. The axis
uses host CLOCK_MONOTONIC; its table and accessible labels give exact decimal
host and Virtual time values. Geometry scales integer differences, never
absolute floating-point timestamps. Intervals are half-open. Sixteen bins
inside each interval mark sample density (sample count, not duration), with
exact counts, weight and class values in the table. Zero-sample intervals
remain visible and explicitly supply no evidence of zero CPU use.
The selection summary and each interval's accessible label also report the
currently filtered sample count and weight; geometry and density retain the
saved capture context.

A partial observation stream carries a prominent gap notice: blank space
may contain unobserved work. Samples classified `observations_incomplete`
mark a shaded host-time extent; this is the extent of those samples, not a
claim about the exact gap boundaries, which the saved correlation does not
retain. An unavailable correlation gets an explanation instead of an empty
timeline. Selecting an interval filters the explorer's tables, caller tree
and flame graph to that interval's correlated samples. The selection prints
each class separately. Lifecycle samples reference a phase name, so selecting
a lifecycle interval selects the phase's samples.

Links from the Task table and timeline open the Task's drill-down. At most
50 Tasks are expanded; the remainder lists Activation count and sample count
and weight separately for `execution_context` and `window_associated`.
Each Task class has its own denominator, top 50 self and inclusive functions,
a self remainder, and a caller tree limited to 100 nodes and 32 levels.
At most 500 zero-sample Activations are listed per Task; omitted counts stay
visible. The two classes are never added into a Task cost. Class links open
the filtered flame graph. All these tables and caller trees work without
JavaScript; interactive zoom, search and sample selection require it.

**Without JavaScript**, every status, every table and the caller tree of
all samples are complete. **With JavaScript**, a CPU page adds filters by
process and thread. When the correlation is `complete` or `partial`, it
also adds filters by class, Participant/Task and Virtual time. The filters
recompute the caller tree and the self and inclusive rankings of the
selection, and the flame graph. The rankings show the top 50, the remainder and the selection
total. The filters show only the matching Activations. Participant, Task and Virtual time select the samples that the
correlation relates to an Activation: `execution_context` and
`window_associated`. Virtual time is the Activation's, as the half-open range
[from, to), in nanoseconds; the page compares it as an exact integer
(BigInt), also above 2^53. When the correlation is `not_requested`,
`unavailable` or `failed`, the page offers no Activation filter and shows
no Activation table: it says why. A heap profile has no Activation view:
its correlation is always unavailable. The filters are not offered for a
capture with more than 200000 distinct (stack, thread, class) rows. The
static tables and tree still show all samples.

The 200000-row limit also disables interactive flame zoom/search and timeline
selection; the static graphs, tables and explicit remainders remain available.

The keyboard reaches every control. The tree nodes are `<details>` elements
(Tab, then Enter). The filters are native form controls. The tables sort
and filter as in the [diagnostics report](running.md#html-reports). The page
follows the light or dark system theme; the Theme button overrides it.

**Content.** The page holds names, paths, identities, counts and weights of
the set. It holds no stack bytes, no source text, no environment variable
and no Message payload. All report text is escaped. The filter data is one
inert `application/json` block. In it, `<`, `>`, `&` and every character
outside ASCII (also U+2028 and U+2029) are `\u` escapes. Thus no name can
end the block or become markup. A source line is text; only an absolute path is
also a link, a `file:` URL that the page builds from the path. No report
value becomes a URL. "Download JSON" saves `profile.json` byte for byte.

## Profile diff

```sh
sil profile diff BASE/profile.json NEW/profile.json [--html OUTPUT.html]
```

Compare two portable artifact sets of the same collector, offline, using the
same validation and file-size limits as `render`. Both need their normalized
CPU samples or heap allocations; no raw capture, tool or binary is opened.
The command prints a terminal summary and optionally writes a standalone
HTML report, including usable tables without JavaScript, keyboard sorting,
and the same dark/light theme controls as the profile report.

Both sets' Manifest hash, SiL version, source revision, host, tool version,
sampling settings, call graph, scope and provenance mode are shown. Differences produce a
prominent warning; unknown identity values stay unknown. Run and capture
statuses and reasons remain visible even when a partial set is compared.

CPU: compare self and inclusive sampled-weight shares per function, with
both sample-count and weight denominators and changes in percentage points.
Doubling every sample leaves the shares unchanged. A zero-weight denominator
has an unavailable share and change, never an invented zero percentage.
Functions that appear or disappear have zero weight on the absent side of a
nonzero denominator. Function identity is the exact module path and symbol,
independent of local module IDs; a changed module path appears as a different
function. Unknown functions remain explicit. Inclusive functions count once
per sample, even in a recursive or inlined stack, and their shares overlap.
Per-Task comparisons are available only when both sets have complete
Activation correlation. Each Task's `execution_context` and
`window_associated` classes have separate rankings and denominators.

Heap: compare allocations, allocated bytes, temporary allocations,
outstanding allocations and outstanding bytes per leaf site (module path
and symbol). Each metric shows BASE, NEW and its signed change. Outstanding
allocations at capture end are not a leak verdict.

Rankings show at most 100 functions/sites; the self remainder gives omitted
weight on both sides (heap: every metric), and inclusive rows are explicitly
non-additive. At most 50 Tasks are expanded. Global rankings include all
samples, also the Tasks that are not expanded. The diff compares one Run
per set: it supplies no significance verdict, and small CPU share changes
may be sampling noise. Weights remain estimates, never function durations
or call counts.

Exit codes: `0` means the summary and requested HTML were produced; `1`
means an output write failed; `2` means invalid usage, missing/invalid or
unsupported evidence, different collectors, an output collision, or a
missing output directory. Output paths are checked first and existing files
are never replaced. The HTML's JSON download holds both input summaries.

## Normalized samples: `cpu/samples.json`

`{"sil_profile_cpu_samples": 1, ...}`:

| Member | Content |
| --- | --- |
| `time_origin_ns` | As in `profile.json` |
| `frames` | List of `{module, symbol, address, module_offset, source}`; `module_offset` is the file offset (`address - start + file_offset`), `source` is `{file, line}` or null. An inlined frame also has `inlined` true |
| `stacks` | List of frame index lists, leaf first |
| `samples` | List of `[offset_ns, process, thread, weight_ns, stack]` in capture order |

Rows refer to the identities of `profile.json`. The file is the offline
input for reports and for the Activation join; it does not reconstruct
chronology from sorted values.
