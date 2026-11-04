# Contract: opt-in Native C source scopes (#360)

Status: **accepted contract, not implemented.** This document resolves
[#360](https://github.com/Stevie1704/sil/issues/360). It is the contract for
separate implementation tickets (see [Decision and tickets](#decision-and-tickets)).
No command, header, library or file format in this document is supported
until those tickets ship it. The ownership and compatibility decision is
[ADR 0007](../adr/0007-source-owned-native-scope-observer.md).

The [#320 proof](../../proofs/function-tracing/README.md) is the evidence.
This contract does not repeat that proof. It replaces the
[proof's follow-up proposal](../../proofs/function-tracing/proposed-spec.md).

## What a user gets

A source owner marks selected logical C calls in a Native library with a
**source scope**. One opt-in `sil profile` Run captures the entry and return
of each scope, and joins complete calls to the Native Task Activations of
the same Run. For each Activation the user can see:

- the exact count of each scope's calls,
- their entry order and nesting,
- the inclusive elapsed host time of each call, in the instrumented build.

Sampled stacks cannot give these values. A sample weight is an estimate,
and a sampled caller is not a call count. The named first user is the ADAS
reference. Its number of `confirms` calls grows with the number of objects.
For one and eight objects, `check_object`, `confirmed`, `confirms` and
`select_lead` have (2, 1, 1, 1) and (16, 8, 36, 1) calls. #320 counted these
calls exactly, also when the compiler inlined the four functions.

## Scope of the contract

| In scope | Out of scope |
| --- | --- |
| One source-owned Native C library per capture | Process participants, FMUs, prebuilt libraries without source |
| Synchronous calls on the runner thread inside a Native Task Activation | Calls on worker threads: they stay unattributed |
| Ordinary C returns, including early returns | `longjmp`, thread cancellation, C++ exceptions through a scope: detected as unbalanced, never repaired |
| Linux x86-64/glibc, GCC 12 (qualified by #320) | Other platforms and compilers until they are qualified |
| Inclusive elapsed host `CLOCK_MONOTONIC` time | CPU time, instruction trace, target ECU timing, WCET |
| Scopes that the source owner writes and maintains | Automatic or regular-expression rewriting of user code, XRay, compiler-inserted instrumentation |

## Source API

An optional, separately installed header `include/sil/source_scope.h` and
an observer library `libsil_source_scope.so.1`. Neither is part of
`sil_api_v1`. A Native library that does not include the header is not
affected.

```c
#include <sil/source_scope.h>

static int confirms(const object *radar, const object *camera) {
  SIL_SOURCE_SCOPE(3, "confirms");
  if (!camera->valid) return 0;        /* early return: a return event */
  return near(radar, camera);
}
```

- `SIL_SOURCE_SCOPE(id, name)` declares one scope in the current block. The
  scope begins at the declaration and ends when the block exits through an
  ordinary C return or the end of the block. Use it once per block.
- `id` is a constant integer in [1, 65535]. 0 is reserved. The source owner
  chooses the IDs and keeps them stable across the builds that they compare.
- `name` is a string literal of at most 63 bytes. `__FILE__` must be at
  most 183 bytes. A longer value is a compile error.
- One scope ID can have more than one site, for example in two blocks or
  in a header that more than one translation unit includes. All sites of an
  ID must have the same name. Sites with the same ID and different names
  are a conflicting duplicate.
- Without the macro `SIL_SOURCE_SCOPES` defined to 1, the macro expands to
  nothing: no code, no data and no link dependency. This is the **opt-out**
  and the default.

With `SIL_SOURCE_SCOPES=1`, each use expands to:

1. One 256-byte record in the ELF section `sil_source_scopes`:
   `{u32 magic "SSC1", u16 version 1, u16 id, u32 line, char name[64],
   char file[184]}` (little-endian, zero-padded, no pointers and no
   relocations). This is the **scope map**. It is in the bytes of the
   instrumented library, so it cannot belong to a different build.
2. A local guard with the GCC `cleanup` attribute. Its initializer calls
   `sil_source_scope_v1_enter(id)`; its cleanup calls
   `sil_source_scope_v1_return(&guard)`.

The instrumented library links `libsil_source_scope.so.1` (`DT_NEEDED`).
The observer exports only `sil_source_scope_v1_*` symbols. A future
incompatible observer uses a new soname and a new symbol prefix.

### Observer states

| State | When | Cost in a scope |
| --- | --- | --- |
| dormant | No capture session, or the process is not the session's runner (for example, a Process participant that loads the library) | One relaxed atomic load and one branch. No clock read, no event, no file. |
| active | `sil profile --collector scopes` started this runner | One direct `clock_gettime(CLOCK_MONOTONIC)` system call, one try-lock and one 16-byte store |
| stopped | The buffer is full, or finalization began | One relaxed atomic load and a loss counter increment |

The observer becomes active only when the environment variable
`SIL_SOURCE_SCOPE_SESSION` is set and its `pid` value equals `getpid()`.
`sil-diagnostics-launcher` adds its own PID to that value before it execs
`sil-run-diagnostics`, so only the runner itself matches. A child process
inherits the variable but has another PID and stays dormant. At the first
enter, the active observer maps its buffer once (`mmap`). If that fails, it
stops and records `observer_unavailable`.

A scope never allocates, does I/O, retries or waits. A contended try-lock
or a full buffer increments `dropped_contended` or `dropped_full`. There is
no overwrite.

### Ordinary runs

- `sil-run` and the kernel sources contain no `sil_source_scope` symbol,
  hook or branch. ADR 0004 applies without change.
- An instrumented library under `sil run`, `sil-run`, `sil diagnostics` or
  another `sil profile` collector is dormant. It writes no file. It is still
  a different build: its code, inlining and timing can differ from the
  uninstrumented build.

## Capture command

```sh
sil profile MANIFEST --out DIR --collector scopes --participant NAME
    [--original-library PATH] [--max-scope-records N]
    [-o RECORDING | --no-recording] [--provenance PATH]
    [--provenance-mode full|external]
    [--participant-timeout-ms N] [--max-protocol-line-bytes N]
    [--max-step-output-messages N] [--max-step-inline-payload-bytes N]
    [--max-activation-bytes N]
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--collector scopes` | required | Source-scope capture. It needs no perf. |
| `--participant NAME` | required | The Native participant whose library has the `sil_source_scopes` section. |
| `--original-library PATH` | none | The uninstrumented build of the same library. Its SHA-256 and build ID are recorded for comparison. It is never loaded. |
| `--max-scope-records N` | 65536 | Observer buffer, in 16-byte records. Range [1, 262144]: at most 4 MiB. |
| `--max-activation-bytes N` | 67108864 | As `--activations` of the CPU collector. The scopes collector always records Activation observations. |
| other options | as the CPU collector | Passed to the runner unchanged and recorded. |

`--frequency-hz`, `--stack-bytes`, `--modules-from`, `--activations`,
`--runner` and `--max-capture-bytes` are usage errors with
`--collector scopes`.

### Lifecycle

1. **Preflight.** The output paths are checked as for the other
   collectors. `NAME` must be a Native participant. Its library must be a
   readable ELF file with a GNU build ID, a valid `sil_source_scopes`
   section and `libsil_source_scope.so.1` in `DT_NEEDED`. The command
   extracts the scope map in memory and records the library's path, build
   ID and SHA-256. A failure of these checks exits 2. The host must be
   Linux x86-64/glibc; on another host the command exits 1, as a host
   preflight failure of the other collectors. No preflight failure makes
   `DIR`. After preflight, the command makes `DIR` and writes
   `DIR/scopes/map.json` first. A failure to write it exits 1 and leaves
   `DIR` as found, without a `scopes.json`.
2. **Session.** The command makes a fresh capture ID (UUID version 4). It
   sets `SIL_SOURCE_SCOPE_SESSION` with the capture ID, the capacity and the
   two output paths. The launcher adds `pid`.
3. **Run.** It starts the launcher and `sil-run-diagnostics` with
   `--activation-observations`, as the CPU collector does with
   `--activations`. The Manifest bytes, Participant commands and working
   directory do not change. The environment of the runner, and of each
   process it starts, gets one more variable: `SIL_SOURCE_SCOPE_SESSION`.
   Only the runner's PID matches it.
4. **Finalization.** The observer writes when the instrumented library is
   unloaded, or at process exit if that is first. It writes
   `scopes/events.bin` first and `scopes/events.json` last. Each is a fresh
   file (`O_CREAT | O_EXCL | O_NOFOLLOW`), written with at most one
   `write` call per buffer, never replaced.
5. **Supervision.** When the observation stream has its `end` line and the
   runner has not exited 30 s later, the command sends SIGTERM to the
   session and SIGKILL 5 s later. The capture is then `failed` with
   `finalization_timeout`.
6. **Decode.** It checks identities, then writes `scopes/calls.json` and,
   last, `scopes.json` through a temporary sibling and a hard link. It
   prints the summary.

SIGINT, SIGTERM and SIGHUP go to the whole session, as for the CPU
collector. A runner that ends by signal or SIGKILL leaves no
`events.json`. The capture is then `failed` with `raw_incomplete`.

## Files

| Path | Contract | Written by |
| --- | --- | --- |
| `scopes.json` | `sil_source_scope_report` v1. Written last. | command |
| `run.diagnostics.json` | `sil_run_diagnostics` v1, unchanged | runner |
| `activation/observations.jsonl` | `sil_activation_observations` 1, unchanged ([contract](../profile-report.md#observation-stream-activationobservationsjsonl)) | runner |
| `scopes/map.json` | `sil_source_scope_map` v1 | command, preflight |
| `scopes/events.bin` | raw records, version 1 | observer |
| `scopes/events.json` | `sil_source_scope_raw` v1 | observer |
| `scopes/calls.json` | `sil_source_scope_calls` v1 | command |
| `run.mcap` | the Recording, unchanged | runner |

The command writes no `profile.json`. The `sil_profile_report` v1 contract,
its capture kinds and its field meanings do not change.

Host times are decimal strings of host `CLOCK_MONOTONIC` nanoseconds, as in
the observation stream. `virtual_time_ns` is an integer.

### `scopes/events.bin`

A 64-byte header, then `records` records. All integers are little-endian.

| Offset | Size | Field |
| ---: | ---: | --- |
| 0 | 8 | magic `SILSCOPE` |
| 8 | 2 | version, 1 |
| 10 | 2 | record size, 16 |
| 12 | 4 | flags, 0 |
| 16 | 16 | capture ID (UUID bytes) |
| 32 | 4 | PID |
| 36 | 4 | reserved, 0 |
| 40 | 8 | capacity in records |
| 48 | 16 | reserved, 0 |

Record: `u64 monotonic_ns, u32 tid, u16 scope_id, u8 edge, u8 reserved`.
Edge 0 is entry; edge 1 is return. Records are in append order. Times are
in order per thread, not across threads.

### `scopes/events.json`: `sil_source_scope_raw` v1

| Member | Content |
| --- | --- |
| `sil_source_scope_raw` | 1 |
| `observer` | `name` (`libsil_source_scope`), `version` (1) |
| `capture_id`, `pid`, `pid_namespace` | The session and the runner. `pid_namespace` is `{device, inode}`. |
| `clock` | `host_monotonic` |
| `clock_bracket_ns` | Three times: a direct system call, libc `clock_gettime(CLOCK_MONOTONIC)`, a direct system call. Read once at activation. |
| `clock_errors` | Failed clock reads; their events are dropped |
| `library` | `build_id`, `path` of the library that registered the scopes (read from its loaded program headers) |
| `capacity_records`, `records` | Buffer size and kept records |
| `dropped_full`, `dropped_contended` | Lost events |
| `bytes_written`, `write_status` | Size of `events.bin`; `complete` or `short_write` |

### `scopes/map.json`: `sil_source_scope_map` v1

`library` (`path`, `build_id`, `sha256`), `original` (`path`, `build_id`,
`sha256` of `--original-library`, or null) and `scopes`: one `{id, name,
sites}` per scope ID, sorted by `id`. `sites` lists each distinct `{file,
line}` of that ID, sorted by file, then line.

### `scopes/calls.json`: `sil_source_scope_calls` v1

| Member | Content |
| --- | --- |
| `sil_source_scope_calls`, `capture_id`, `participant` | Version 1, the session, the selected participant |
| `status`, `problems` | `complete` with `[]`, or `partial` with the capture problems below |
| `totals` | `entries`, `returns`, `paired_calls`, `attributed_calls`, `withheld_calls`, `unattributed` |
| `activations` | The record of every kept Native Task Activation of the participant, in stream order (below) |
| `unattributed` | `reasons` (count per reason), `rows` (at most 1,000, in entry order), `rows_remainder` |

An activation has the observation members (`participant`, `task`, `index`,
`virtual_time_ns`, `begin_ns`, `end_ns`, `outcome`) and `status`, `reason`,
`withheld_calls`, `calls`, `counts`.

- `exact`: `reason` is null. `calls` lists every contained call in entry
  order: `scope`, `depth` (0 at the outermost scope in the Activation),
  `parent` (index in `calls`, or null), `entry_ns`, `return_ns`,
  `elapsed_ns`. `counts` maps each scope ID to its number of calls.
- `unavailable`: `calls` is `[]`, `counts` is null and `withheld_calls`
  counts the paired calls that it contains. `reason` is the first that
  applies of `observations_incomplete`, `events_lost`, `not_completed`,
  `unbalanced_scopes` and `crossing_call`.

An unattributed row has `scope`, `tid`, `entry_ns`, `return_ns` and
`reason`. An unpaired entry has `return_ns` null; an unpaired return has
`entry_ns` null.

The totals reconcile: `attributed_calls + withheld_calls` plus the paired
unattributed rows is `paired_calls`. `entries - paired_calls` rows have no
return and `returns - paired_calls` rows have no entry.

### `scopes.json`: `sil_source_scope_report` v1

| Member | Content |
| --- | --- |
| `producer` | `name` (`sil profile`), `sil_version` |
| `configuration` | Manifest path and SHA-256, participant, capacity, guards, provenance mode |
| `run` | As `sil_profile_report` v1 `run`: exit code, signal, first failure. Independent of the capture. |
| `capture` | `status` (`complete`, `partial`, `failed`, `interrupted`), `reasons`, `capture_id`, loss counters, `records` |
| `identities` | `instrumented` and `original` library identities, observer version, compiler identity from the library's `.comment` section |
| `artifacts` | Path, size and SHA-256 of every file in the table above |
| `summary` | Per Task: Activations, exact, unavailable by reason; per scope: total attributed calls, and min/median/max calls per exact Activation |
| `limitations` | The fixed list in [Presentation](#presentation) |

## Join and pairing

Pairing is per thread, with one stack. An entry pushes. A return pops when
the top of the stack has the same scope ID. When it does not, the frames
above the nearest frame with the same ID are `missing_return`, and that
frame pairs. With no such frame, the return is `missing_entry`. Entries
left at the end are `missing_return`. Recursion is nesting of the same ID.
No return is made up for a missing one, and a missing return has no
duration.

A paired call belongs to Activation A when all of these are true:

- Its thread is the runner thread: `runner_tid`, and A's `tid`.
- A is an Activation of the selected participant, of kind `native_task`.
- `begin_ns <= entry_ns < end_ns` and `begin_ns <= return_ns < end_ns`.
 Intervals are half open: an event at the
next Activation's `begin_ns` belongs to that Activation.

| Call | Result |
| --- | --- |
| Inside a completed Activation; no problem in that Activation | Listed in `calls` |
| Inside a `failed` or `interrupted` Activation | Activation `not_completed`, call withheld |
| Inside an Activation that has an unpaired event | Activation `unbalanced_scopes`, call withheld; the unpaired event is a row |
| Entry and return in different intervals, or one outside every interval | Row `crossing_interval`; the Activations with an endpoint are `crossing_call` |
| On another thread | Row `worker_thread` |
| Runner thread, inside an Activation or Process Step of another participant | Row `other_participant` |
| Runner thread, outside every Activation and Process Step (setup, shutdown, library constructors) | Row `outside_activations` |
| After the last kept record of a cut observation stream | Row `observations_incomplete`; later Activations are missing |

Any lost event (`dropped_full`, `dropped_contended`, `clock_errors`) makes
every Activation `events_lost` and the capture `partial`. A cut
observation stream makes the capture `partial` with
`observations_incomplete`. Without loss, counts and order are exact for an
`exact` Activation.

### Identity checks

These checks fail the decode before any file is written. The capture is
`failed` with the reason in parentheses.

- `events.bin` header and `events.json` agree on version, capture ID, PID
  and capacity; the file size is `64 + 16 × records`; every record has a
  valid edge, reserved 0 and a scope ID in the map (`raw_invalid`).
- `events.json` exists and `write_status` is `complete`
  (`raw_incomplete`).
- The capture ID is the session's, and PID and PID namespace equal the
  observation header's `runner_pid` and `pid_namespace`
  (`capture_mismatch`).
- `library.build_id` in `events.json` equals the map's build ID, and the
  library file at the map's path has the same SHA-256 after the Run as at
  preflight (`identity_mismatch`). A path alone never identifies a build.
- `clock` is `host_monotonic` in both streams, the bracket is in
  non-decreasing order and `clock_errors` is known (`clock_unverified`).
- The scope map has no conflicting duplicates and at most 4,096 sites
  (`map_invalid`; normally a preflight error).
- In `sil profile render`: each file in `artifacts` has its recorded size
  and SHA-256, and a known contract version, before any output (exit 2).

## Run and capture outcomes

The Run outcome and the capture outcome are separate. A capture failure never
replaces a Run failure.

| Situation | Exit code | `scopes.json` |
| --- | --- | --- |
| Invalid option, output path exists or is an alias, participant not Native, library not instrumented | 2 | not written, `DIR` not made |
| Not Linux x86-64/glibc (host preflight) | 1 | not written, `DIR` not made |
| `sil-run-diagnostics` or the launcher is not on `PATH` | 127 | not written |
| The Run failed (exit code N) | N | written, capture status as found |
| The runner ended by signal N | 128+N | written, capture `failed` (`raw_incomplete`) |
| The Run succeeded, capture `complete` | 0 | written |
| The Run succeeded, capture `partial`, `failed` or `interrupted` | 1 | written |
| The Run succeeded, `scopes.json` cannot be written | 1 | not written |
| Hard crash or SIGKILL of the command | signal status | not written; raw files can remain |

Capture reasons: `events_lost`, `observations_incomplete`,
`unbalanced_scopes` (`partial`); `observer_unavailable`, `raw_incomplete`,
`raw_invalid`, `capture_mismatch`, `identity_mismatch`, `clock_unverified`,
`map_invalid`, `normalized_limit`, `storage_reserve_reached`,
`finalization_timeout` (`failed`); `interrupted`. A capture with zero
scope events is `complete`. It does not prove that no scope ran in a
dormant process.

## Resource and failure budgets

| Resource | Budget | At the limit |
| --- | --- | --- |
| Observer buffer | 64 B + 16 B × `--max-scope-records`; 1 MiB by default, 4 MiB at most; mapped once | `dropped_full`; the Run continues |
| Work in a scope | Dormant: 1 atomic load. Active: 1 clock system call, 1 try-lock, 1 store. No allocation, I/O, retry or wait | `dropped_contended` on contention |
| Scope map | At most 4,096 sites of 256 B (1 MiB section); `map.json` at most 2 MiB | preflight exit 2 |
| `events.json` | At most 4 KiB | — |
| Observation stream | `--max-activation-bytes`, 64 MiB by default, existing policy | `observations_incomplete` |
| `calls.json` | At most 16 MiB (the per-file input limit of the offline commands) | capture `failed`, `normalized_limit`; raw files stay |
| Unattributed rows | 1,000, then counts only | `rows_remainder` |
| `scopes.json` | At most 1 MiB | — |
| Free space | The existing 8 MiB reserve of `DIR`, checked every 100 ms | The command cannot stop the observer; it records `storage_reserve_reached` when the reserve is gone before finalization |
| Finalization | Two regular-file writes after the observation `end` line; 30 s, then SIGTERM, 5 s, SIGKILL | `finalization_timeout` |
| Offline inputs | 16 MiB per file, as all `sil profile` offline commands | exit 2 |

Disk growth of `DIR` excluding the Recording and `run.diagnostics.json`
is at most 4 MiB raw +
the observation limit + 16 MiB normalized + 3 MiB maps and summaries.
Regular-file I/O can wait on the file system. The observer makes no hard
real-time or uninterruptible-I/O promise. No reader or pipe can apply
backpressure to an Activation.

## Presentation

```sh
sil profile render DIR/scopes.json [--html OUTPUT.html]
    [--participant NAME] [--task NAME] [--activation-index N]
    [--from T] [--to T]
```

`sil profile render` selects the contract by the document's top-level
version member. It reads `scopes.json` and the files that its `artifacts`
list names, and verifies them first ([Identity checks](#identity-checks)). The selection options have the same rules as for
`profile.json` ([Activation outliers](../profile-report.md#activation-outliers-and-offline-selection)).
`sil profile diff` rejects `scopes.json` with exit 2.

- Terminal: statuses, identities, loss, per-Task exact and unavailable
  counts, and the per-scope count table. With an exact Activation selected:
  the calls in entry order, indented by depth, at most 500 rows with a
  remainder.
- HTML: the same views, offline, with the existing bounds, keyboard
  operation, themes and escaping. An unavailable Activation shows its
  reason and withheld count, never partial counts.

Every view states these limitations:

- Durations are inclusive elapsed host time of the **instrumented** build.
  They include observer cost, waiting and preemption. Nested durations
  overlap and must not be added. No observer cost is subtracted.
- The instrumented build can inline and optimize differently from the
  original build. Both identities are shown; durations do not transfer to
  the original build.
- Counts are exact only for `exact` Activations of a capture without loss.
- No CPU time, instruction trace, target ECU timing or WCET.

## Evidence reused from #320

The [retained #320 evidence](../../proofs/function-tracing/evidence/README.md)
from [Linux run 39466725936](https://github.com/Stevie1704/sil/actions/runs/39466725936)
already shows:

| Property | #320 result |
| --- | --- |
| Exact counts and entry order | (2, 1, 1, 1) and (16, 8, 36, 1) in all 40 joined Activations of each capture; 2,640 events, no loss |
| Inlined logical calls | All four functions inlined by GCC 12 with and without scopes; their scopes still recorded |
| Half-open join | Every joined event inside an actual runner observation; same PID, TID and clock |
| Loss | A seven-record buffer kept 112 bytes, counted 2,633 dropped events and gave no exact attribution |
| Recording equality | All 19 Runs had the same Recording SHA-256 |
| Perturbation | Median Activation 1,951.5 → 5,645.5 ns (one object), 7,160.5 → 50,795.5 ns (eight objects) |

These results do not need to be repeated. They used a proof-only observer
and copied sources, so the implementation must show the same counts and
order with the supported header and observer.

## Remaining qualification matrix

Each row needs Linux x86-64/glibc evidence (GitHub Actions `ubuntu-24.04`,
pinned GCC 12) unless it says offline. Expected values are independent of
the implementation: worked examples or a counted fixture.

| ID | Case | Expected | Ticket |
| --- | --- | --- | --- |
| Q1 | ADAS one/eight objects with the supported header | #320's counts and order | API/lifecycle |
| Q2 | Recursive scope, depth 3 | 3 nested calls, depths 0–2 | API/lifecycle, decoding |
| Q3 | Early return from a nested scope | A return event for each path | API/lifecycle |
| Q4 | Two Native Tasks at the same Virtual time | Calls go to their own Activation | API/lifecycle |
| Q5 | `longjmp` out of two nested scopes | `unbalanced_scopes`; 2 `missing_return` rows | API/lifecycle |
| Q6 | C++ exception through a scope (C++ caller of the C library) | Unbalanced; no synthesized return | API/lifecycle |
| Q7 | Scope on a worker thread | `worker_thread` rows; Activation exact | API/lifecycle |
| Q8 | Failed and interrupted Activations | `not_completed`, calls withheld | API/lifecycle |
| Q9 | Buffer of 7 records; forced contention | `events_lost`, `partial`, equal Recording | API/lifecycle |
| Q10 | `mmap` failure | `observer_unavailable`; Run outcome unchanged | API/lifecycle |
| Q11 | Rebuilt library at the same path; changed bytes after preflight | `identity_mismatch` before output | decoding |
| Q12 | Duplicate ID with another name; missing section; missing `DT_NEEDED` | exit 2 before `DIR` | decoding |
| Q13 | Output file exists; `events.json` missing; short write | `raw_incomplete` or `raw_invalid`; nothing replaced | API/lifecycle |
| Q14 | SIGKILL of the runner; SIGINT of the command | 137 / `interrupted`; Run outcome retained | API/lifecycle |
| Q15 | Finalization blocked beyond 30 s | `finalization_timeout`; Run outcome from the diagnostics report | API/lifecycle |
| Q16 | Free-space reserve reached | `storage_reserve_reached`; Run continues | API/lifecycle |
| Q17 | Instrumented library under `sil run`, `sil-run`, a CPU profile and as a Process participant | Dormant: no file; equal Recordings and exit codes | API/lifecycle |
| Q18 | `sil-run` binary and kernel sources | No `sil_source_scope` symbol or include. The source check already runs offline in `tests/test_source_scope_contract.py`; the binary check is pending | API/lifecycle |
| Q19 | Build without `SIL_SOURCE_SCOPES` | Same bytes as without the header calls; no `DT_NEEDED` | API/lifecycle |
| Q20 | Original and instrumented builds | Both identities retained; inline decisions of both builds retained | API/lifecycle |
| Q21 | Ordinary versus capture Runs, generous Response deadlines, 3 repeats | Byte-identical Recordings and equal outcomes; elapsed and storage retained without a threshold | API/lifecycle |
| Q22 | Fixtures in `tests/fixtures/source_scopes/` (offline) | The decoder output equals each `expected.json` | decoding |
| Q23 | Malicious names, large integers, 1,000+ unattributed rows, maximum `calls.json` (offline) | Escaped, bounded, remainder shown | decoding, presentation |
| Q24 | Render and HTML of all fixture cases (offline) | Exact and unavailable Activations, limitations shown | presentation |

The fixtures in [`tests/fixtures/source_scopes/`](../../tests/fixtures/source_scopes/)
are worked examples of the decoder's input and output. They cover nesting,
an early return, the half-open boundary, recursion, every unattributed
reason except `missing_entry` and `observations_incomplete`, the
`not_completed`, `unbalanced_scopes`, `crossing_call` and `events_lost`
Activations, and an identity mismatch.
`tests/test_source_scope_contract.py` keeps them consistent with this
contract.

## Decision and tickets

**Build, gated.** The named benefit is exact per-Activation call counts and
order for source-owned Native C code, which sampling cannot give. The
constraints above stay: one source-owned Native C library, synchronous
runner-thread calls, Linux x86-64/glibc, GCC 12, instrumented-build
durations only.

The work is split into three tickets against this contract:

- [#372](https://github.com/Stevie1704/sil/issues/372) API and capture lifecycle: header, observer, launcher session,
  `--collector scopes`, raw files, supervision and Q1–Q10, Q13–Q21.
- [#371](https://github.com/Stevie1704/sil/issues/371) Decoding: map extraction, identity checks, pairing, join,
  `calls.json`, `scopes.json` and Q2, Q11, Q12, Q22, Q23.
- [#373](https://github.com/Stevie1704/sil/issues/373) Presentation: `sil profile render` for `scopes.json` and Q23, Q24.

Decoding has no dependency. API/lifecycle uses the decoder's map
extraction. Presentation needs decoding. Nothing ships until its ticket's
matrix rows pass. A change to this contract needs a review of ADR 0007.
