# Retained #361 capture-window qualification

Decision: **defer production adoption**. Long-window volume reduction is
demonstrated. Asynchronous controls do not isolate a short Activation exactly,
and a competing writer that defeats the reserve can terminate the perf-owned
Run. This completes the investigation, not a supported collector.

[Successful Linux qualification](https://github.com/Stevie1704/sil/actions/runs/41784812043)
ran source `bda3d1af7cceb2f03eea4ed3a373465428e0f02a` on Linux x86-64,
kernel `7.0.0-1017-azure`, glibc 2.39, perf 7.0.14 and GCC 13.3.0.
Exact packages, commands, build IDs, binary/source hashes, host identities,
the diagnostics overlay and container image identity are retained. The
container installs the host's exact perf package version and grants the
non-root perf binary CAP_PERFMON. Sampling uses cpu-clock:u, 997 Hz,
DWARF/8192-byte stacks and CLOCK_MONOTONIC.

| Environment | Availability/result |
| --- | --- |
| Native, original paranoid 4 | Denied; perf exit 255; no Run started |
| Container, default capabilities, paranoid 4 | Capability-bearing perf cannot execute, exit 126; no Run started |
| Container, CAP_PERFMON and unconfined seccomp, paranoid 4 | 62 fresh cases/repetitions; functional/evidence checks passed |
| Native, explicitly changed paranoid 2 | 62 fresh cases/repetitions; functional/evidence checks passed |

Each capture has one fresh Run and one collector. The ordinary runner has no
notification seam. The unmodified diagnostics baseline is copied before the
proof-only overlay is configured; its binary identity is retained separately.
Native Tasks `late` and `peer` share Virtual time; `late` alone executes the
known hot function at Activation 610. A Process participant calls the same
optimized library without a Clock shim. Workload markers, diagnostics observations, control notices,
acknowledgments and CPU samples are distinct evidence sources.

## Volume and sampled evidence

Three fresh repetitions per ordinary/diagnostics/observer/whole/window mode;
medians below are bytes, before post-filtering:

| Environment / Participant | Whole-Run baseline | Window `[610000000,611000000)` | Reduction |
| --- | ---: | ---: | ---: |
| Native host / Native | 26,304,992 | 1,607,640 | 93.89% |
| Native host / Process | 14,602,888 | 1,607,840 | 88.99% |
| Container / Native | 26,271,296 | 1,624,512 | 93.82% |
| Container / Process | 14,577,400 | 1,616,040 | 88.91% |

Whole captures are also post-filtered against the same independent workload
intervals. Every whole/window repetition retains leaf samples of the known
`late_hot_function`. Counts, selected/unselected samples and each boundary
are saved per Run. These are independent sampling experiments, so sample
counts need not match across Runs. Post-filtering does not shrink raw files.

Normal-window command-to-ack latency ranged from 0.217 to 3.046 ms across
the four combinations. First samples followed the independent selected-work
begin by 1.723–4.092 ms; final samples could precede its end or extend as far
as 2.232 ms beyond it. No number is an effective enable/disable timestamp or
continuous coverage guarantee. Native host exact-peer selection retained two
samples: one inside the peer's marked interval and one outside. The container
retained one inside sample. Equal Virtual time does not identify a Task's CPU.

Every normal window retains setup exec records before enable (one Native,
two Process), five mappings of the workload DSO, and exit records after
disable (one Native, two Process). The exported event attributes identify
the CPU event and the separately enabled dummy tracking event. Named CPU
disable preserves tracking; a bare all-event disable is not equivalent.

Every normal window's real begin notice at 611,000,000 ns triggers disable;
workload index 611 remains outside the selection. In deliberate failures the
Run stops at 610,000,000 ns, before its requested 615,000,000 ns end. All four
failure cases report `terminated_before_requested_end` and
`not_reached_run_failed`. The notifier's shutdown event separately requests
disable, acknowledged in those four cases; it does not advance Virtual time.
Hard kills retain an unreached end and missing footer. An observed collector
exit or acknowledgment is never substituted for a requested Virtual-time end.

`disabled_regions` contains initial/tail disable-request intervals, their
control/ack/collector-exit observations, and samples observed during uncertain
transitions. Effective boundaries stay unknown. These planned regions are
separate from observer loss, perf loss and truncated export states.

## Observer cost and limits

Observed elapsed medians in ms; three Runs each, with identical workload
marker instrumentation in every mode:

| Environment / Participant | Ordinary | Unmodified diagnostics | Notifications | Whole baseline | Window |
| --- | ---: | ---: | ---: | ---: | ---: |
| Native host / Native | 3092.4 | 3104.6 | 3113.1 | 3167.2 | 3154.4 |
| Native host / Process | 1763.8 | 1776.9 | 1787.4 | 1822.2 | 1820.8 |
| Container / Native | 3098.9 | 3112.7 | 3111.7 | 3161.1 | 3166.5 |
| Container / Process | 1777.4 | 1767.4 | 1792.7 | 1818.3 | 1826.3 |

Variation prevents an isolated constant observer-cost estimate; a lower median
does not establish negative or zero overhead. These measurements describe a
shared CI host and this optimized workload, not a general perturbation bound.
The proof makes no timing threshold, significance, exclusive CPU, target ECU
or WCET claim. Diagnostics and independent marker intervals retain local
Activation durations for further inspection. No overhead is subtracted.

## Edge, failure and storage results

- Empty and unreached selections run the workload without enabling CPU
  sampling; both retain zero CPU samples and remain distinct states.
- Delayed enable begins about 22–24 ms into known work. A host stop during the
  selected work ends sampling more than 100 ms before that work returns.
  The scheduler never waits for a command or acknowledgment.
- Ordinary/observed successful Runs and deliberate Participant failures
  retain equal Recording hashes and outcomes, with a 10-second Process
  response deadline and a 60-second whole experiment guard. A helper's return
  does not make the encompassing failed Activation completed.
- SIGKILL during late work leaves absent observer/Activation footers and
  an explicit unclosed state. Missing Native workload markers remain missing;
  Process helper intervals do not synthesize a closed runner Activation.
- A small send buffer and paused receiver drop 74–78 notifications.
  A separate one-page perf ring loses samples while notifications remain
  complete. Loss can appear in PERF_RECORD_LOST records, LOST_SAMPLES final
  statistics, or both. The report preserves the evidence sources without
  conflating record counts with a total lost-sample count. Both differ from
  deliberately uncaptured CPU regions and from zero-sample work.
- The 64 KiB capture-limit cases stop sampling at the next 100 ms guard poll
  and retain files of 517,120–669,640 bytes. This is measured overshoot, not a
  hard file quota. Run outcomes/Recordings remain equal.
- Active collection on a separate 16 MiB tmpfs crosses the existing 8 MiB
  reserve, disables sampling and preserves the Run/Recording in all four
  cases. Run outputs remain on the main filesystem.
- Deliberately filling that tmpfs between guard polls produces truncated
  perf files, perf exit 255 and Run exit 1/SIGTERM in all four cases. The
  raw header is not finalized and sample/metadata export fails. Some final
  disables remain unacknowledged. This demonstrates a limitation of the
  perf-owned launch, not a successful continuation under ENOSPC. No successful
  Run, complete capture or exact end boundary is invented for those cases.

The validator requires SIGKILL to retain an unknown Run verdict, incomplete
Activation observations and an unreached requested end. It requires the
ENOSPC injection's actual errno and consistent Run/Recording/export states;
it rejects invented success or complete capture. It also checks real index
611 is outside every normal window, using actual workload Virtual time.
The production decision requires an independently surviving Run supervisor
and a reviewed versioned coverage/lifetime contract before reopening. See the
[decision and reopening criteria](../../../docs/profiling-investigation-decisions.md#late-capture-windows--361-defer-production-after-qualification).
No production CLI, ABI, Manifest, Recording format or supported schema changes.

## Retention and offline replay

`archive.sha256` binds `portable-evidence.tar.xz`. Its 1,634 files contain
46,541,447 payload bytes compressed to 3,807,536 bytes, about 63% smaller than
the earlier gzip bundle. Per-environment `results.json` remains inside;
`analysis.json` is regenerated by the one reporting/validation CLI.

`retain.py` keeps every offline validation input, all CPU-sample host
timestamps, all notices/markers/Activation records, diagnostics, policies,
identities and loss statistics. It retains metadata records and hot-function
leaf exports (plus warnings/errors), rather than duplicate sample text. It
checks every Recording hash before omitting duplicate Recordings; baseline
and exceptional Recordings/provenance remain. `retention.json` records the
measurement revision, omissions, and original/retained hashes of filtered
exports. Exact measured proof sources are included. No standalone JSON
summaries are committed beside the archive.

A separately identified `counter-only-observation/` retains the Native
one-page-ring output from [run 41783885359](https://github.com/Stevie1704/sil/actions/runs/41783885359),
revision `42d91f8cb09145a68426608b466fc05aebeedb96`: no LOST event record,
but positive LOST_SAMPLES statistics, including an aggregated 142 value.
This is the observed counter-only regression; its source identity stays
separate and it is not pooled into the final-run comparisons.

Full raw exports and perf data remain in the linked Actions artifact for its
30-day retention. Offline replay needs neither perf, Linux, SiL installed nor
the workload binaries; use Python 3.11 or later. Analysis has a 256 MiB per-file
input cap and no interactive dataset or DOM. To reproduce curation, download
the Actions artifact and run `retain.py ARTIFACT FRESH_OUTPUT --run-url URL`;
curation includes the recorded revision's sources, not the current checkout's
possibly changed decoder.

```sh
cd proofs/capture-windows/evidence
shasum -a 256 -c archive.sha256
replay_dir=$(mktemp -d)
tar -xJf portable-evidence.tar.xz -C "$replay_dir"
for environment in "$replay_dir"/window-evidence/*/; do
  python3 "$replay_dir/proof-sources/validate.py" "$environment"
done
```

The ten report-boundary regression tests pass. The full suite passed with
3,683 tests and 115 skips; 12 C++ tests and the determinism gate passed. The
last decoder-only change is additionally covered by the ten-test focused run.
Qualification is manually dispatched; PR updates do not change host policy.
