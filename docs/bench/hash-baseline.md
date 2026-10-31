# Hashing cost baseline

This baseline shows the cost of file hashing in a Release build, for #323. It
gives the procedure, the workloads and one initial capture at the revision
before the hashing changes. It is evidence, not a threshold: no test or gate
reads a duration from it.

## Retained captures

| Capture | Revision | Load | Tables | Raw |
| --- | --- | --- | --- | --- |
| Initial | `600fa429dfcca50b11eb5db9b6735d6d52bcea89` | Not quiet. Other coding agents built and tested on this machine (1-minute load average 8 to 13). | [results](hash-baseline-600fa429dfcc-results.md) | [JSON](hash-baseline-600fa429dfcc.json) |
| #328 | See [recording-digest.md](recording-digest.md) | Not quiet | Linked there | Linked there |
| #326 before | `55c4d3990f6e42339c6a2f1195a73a3d9f657fa7` (feat/323 tip, no provenance cache) | Not quiet. 1-minute load average 6.9 at 09:33. Three repetitions. | [results](hash-baseline-dedup-before-55c4d3990f6e-results.md) | [JSON](hash-baseline-dedup-before-55c4d3990f6e.json) |
| #326 after | `fdece5b17cf54178160f937872bba9b0339c8577` (per-preparation provenance cache) | Not quiet. 1-minute load average 6.9 at 09:33. Three repetitions. | [results](hash-baseline-dedup-after-fdece5b-results.md) | [JSON](hash-baseline-dedup-after-fdece5b.json) |

The initial capture shows the method and the order of magnitude. Do not use it
to compare small differences. #329 repeats the capture on a quiet machine.

The #326 pair uses one backend, so it isolates repeated-input reuse from the
backend change. The provenance-input hash operations of
`native-shared-library` fall from 33 to 2, for the same 8 MiB library. Those of
`process-shared-inputs` fall from 25 to 4. The median preparation time falls
from 177.9 ms to 10.8 ms, and from 184.3 ms to 28.0 ms. Both captures ran on a
loaded machine. The end-to-end medians are lower after the change, but three
repetitions on a loaded machine do not establish a speedup. The operation
counts are the deterministic evidence.

The machine is an Apple M1 Pro with macOS on arm64. SUPPORT.md names Linux
x86-64 as the supported machine class, so this capture does not describe that
class. Do not compare numbers from different machines.

## Procedure

Run this command from the repository root. It needs no other input:

```sh
.venv/bin/python tools/hash_baseline.py \
  --revision 600fa429dfcca50b11eb5db9b6735d6d52bcea89 \
  --machine "<a name for the machine>" \
  --load-condition "<what else runs on the machine>" \
  --label <name of the evidence files>
```

`make bench-hash ARGS='...'` runs the same tool.

| Option | Use |
| --- | --- |
| `--revision COMMIT` | Build this commit and measure it. The tool makes a separate clone and a Release build in `--cache-dir`. It does not touch your checkout, its branches or its worktree list, and it ignores uncommitted edits. It uses a completed build of the commit again. |
| `--build-dir DIR` | Measure an existing Release build. The tool refuses other build types. The evidence states if the source tree has uncommitted changes. |
| `--runner-arg ARG` | Give an argument to both runners. Use this to measure a runner option. The workloads do not need any new option. |
| `--backend TEXT` | State the SHA-256 backend in the evidence. |
| `--workload NAME` | Measure only this workload. You can repeat this option. |
| `--repetitions N` | Set the number of repetitions for each workload. The default is 5. |
| `--scale X` | Multiply artifact sizes and Recording Durations. Use a small value only for a test of the tool. |
| `--out-dir`, `--label` | Set where the tool writes `hash-baseline-<label>.json` and `hash-baseline-<label>-results.md`. |

On a macOS machine with the 27.0 SDK, set `SDKROOT` first. See
[the known traps](../agents/traps.md).

The Native workloads load `toy_producer.silp` and `toy_multitask.silp` from the
selected build. The Process workload uses `echo.py` and the Python package of
the selected source. The Manifests use only keys that exist at the pinned
revision.

## Workloads

| Workload | Shape | Why it is here |
| --- | --- | --- |
| `native-shared-library` | 32 Native Participants share one 8 MiB library. | Repeated hashes of one input (#326). |
| `native-distinct-artifacts` | 4 Native Participants, each with its own 128 MiB library. | Hash throughput with no repeated input (#325). |
| `native-recording-heavy` | 1 Native Participant with 2 Tasks, 1,000 s Duration, 1.2 million Messages. | The Recording digest (#328). |
| `process-shared-inputs` | 8 Python Process participants share an interpreter, a script and one 32 MiB file argument. | Repeated inputs of Process participants (#326). |
| `replay-verification` | Replay of a 100 s Recording. | Replay verification, which stays on in all modes (#327). |

The tool appends bytes to a copy of the toy library to set its size. The loader
does not read these bytes. The expected work comes from the real files: the raw
evidence gives the size of each artifact and of the Recording.

## Method

Each repetition runs two series, one after the other:

| Series | Runner | What it gives |
| --- | --- | --- |
| Ordinary | `sil-run`, with no observer | The end-to-end time of an ordinary Run. |
| Attributed | `sil-run-diagnostics`, with `tools/hash_trace.c` loaded | The phases and the hash work. |

### Timing boundaries

| Value | Start and end |
| --- | --- |
| End to end | From before the tool starts the runner to the reap of the runner. |
| Reported Run wall | The `run_wall_time` of the diagnostics report. It excludes the side-car write and process start and exit. Before #328 it also **excludes** the hash of the Recording after its close. From #328 it **includes** the hash of the Recording, which occurs while the Run writes. |
| Preparation | The `preflight_ns` phase: Manifest load, preparation with all input hashes, Recording open. |
| Execution and Recording | The `setup_ns`, `scheduled_execution_ns` and `shutdown_ns` phases. Replay verification occurs in setup, so its time is in this value. From #328 the Recording digest is also in this value. |
| Finalization | The Recording close (`recording_finalization_ns`) plus the time of the output hash pass from the trace. From #328 a normal Run has no output hash pass. |
| Outside the reported Run wall | End to end minus the reported Run wall. The raw evidence keeps this value. |

A change that moves work across a boundary changes these values, but it does not
save time. Compare the end-to-end time as well.

### Hash work

`tools/hash_trace.c` observes `open`, `fopen`, `read`, `pread` and `fread` of the
runner process with `DYLD_INSERT_LIBRARIES` or `LD_PRELOAD`. It needs no change to
SiL, so it can measure any revision, and it is not in any SiL build. It does not
load in Process participants, and its tables have a fixed size. The tool stops if
a table becomes full.

A **hash operation** is one complete sequential read of a file, from offset 0 to
its size at open. The tool gives each operation a role from the path:

| Role | Path |
| --- | --- |
| `manifest` | The Manifest. |
| `provenance_inputs` | A path with a digest in the provenance side-car. |
| `replay_verification` | The Replay Recording. Only the first complete pass counts. |
| `output` | The output Recording. |

All other reads are in `other_read_bytes`, for example the Replay decode.

### Checks

The tool stops if one of these checks fails:

- Each digest in a side-car must match an independent SHA-256 from Python
  `hashlib`. An entry with no digest string makes no claim, so a later schema
  can state an omission.
- All Runs of a workload, in both series, must write the same Recording bytes.
- A runner must exit with code 0.

### Cache conditions

One discarded Run precedes the series of each workload, and the tool writes the
input files just before it. All values are for a **warm page cache**. The tool
**cannot control a cold cache**, because that needs administrator rights. Thus
there is no cold-cache series, and each capture states this.

## Initial results

The [tables](hash-baseline-600fa429dfcc-results.md) have the minimum, the median
and the maximum of each value. These medians come from five repetitions on a
loaded machine:

- `native-shared-library`: 33 hash operations and 257 MiB, for 8 MiB of distinct
  library bytes. The hash passes are 1,524 ms of 1,554 ms end to end.
- `native-distinct-artifacts`: 513 MiB in 3,041 ms of 3,058 ms end to end. This
  is about 170 MiB/s with the minimal software SHA-256.
- `native-recording-heavy`: the Recording is 72 MiB. Its hash pass is 399 ms of
  735 ms end to end, and the reported Run wall of 324 ms excludes it.
- `process-shared-inputs`: 25 hash operations and 257 MiB. The hash passes are
  1,464 ms of 2,043 ms end to end.
- `replay-verification`: the Replay verification is 40 ms and 7.2 MiB. The output
  hash is 41 ms and 7.2 MiB. These are separate work.

## Limitations

- **Reads, not hash calls.** The trace sees reads. It does not see a call to a
  hash function. Thus:
  - The `manifest` time is only the file read. The hash of the bytes in memory is
    not in it.
  - An input hash pass includes the read and the hash. The trace cannot separate
    them.
  - A digest that the runner calculates while it writes (#328) has no read. The
    tool then sets `output_digest_without_reread` to true, and the `output` row
    shows zero. The digest check still applies. Work that is done during the write
    is in the execution and close times, and the trace cannot separate it.
  - An input with a digest but no complete read is in
    `inputs_without_observed_hash`. For example, a memory-mapped hash causes this.
    Do not use the hash work of that workload as a total.
  - A complete read of an input for another purpose counts as a hash operation.
    At the pinned revision, the capture shows none: operations match the side-car
    entries.
- **Measurement overhead.** The trace adds two clock reads and one mutex lock to
  each read of an observed file, one read for each 64 KiB block or less. The
  attributed series also includes the diagnostics collector. The attributed
  end-to-end median of each workload in this capture is about 3 to 8% above
  the ordinary median, on a loaded machine. No correction is subtracted.
- **Phases come from the attributed series.** They describe the diagnostics runner
  under the trace, not `sil-run`. Use the ordinary series for the cost of an
  ordinary Run.
- **Linux.** A separate check in a `gcc:13` container on arm64 confirmed that the
  trace observes C++ stream reads and `pread` under glibc. No capture on Linux is
  retained here.
- **Short series.** Five repetitions give a spread, not a statistical result.
  [The earlier diagnostics comparison](diagnostics-overhead.md) gives context
  only, and this baseline does not depend on it.
