# Retained #361 capture-window qualification

Decision: **defer production adoption**. Long-window volume reduction is
demonstrated. Asynchronous controls do not isolate a short Activation exactly,
and a competing writer that defeats the reserve can terminate the perf-owned
Run. This completes the investigation, not a supported collector.

[Successful Linux qualification](https://github.com/Stevie1704/sil/actions/runs/41716220608)
ran source `e4d158cf252ea1e9172f3870b47a7332a1d9f670` on Linux x86-64,
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
| Native host / Native | 19,700,448 | 1,219,832 | 93.81% |
| Native host / Process | 10,660,504 | 1,135,808 | 89.35% |
| Container / Native | 20,248,024 | 1,202,968 | 94.06% |
| Container / Process | 10,913,016 | 1,287,360 | 88.20% |

Whole captures are also post-filtered against the same independent workload
intervals. Every whole/window repetition retains leaf samples of the known
`late_hot_function`. Counts, selected/unselected samples and each boundary
are saved per Run. These are independent sampling experiments, so sample
counts need not match across Runs. Post-filtering does not shrink raw files.

Normal-window command-to-ack latency ranged from 0.226 to 6.124 ms across
the four combinations. First samples followed the independent selected-work
begin by 1.647–7.029 ms; final samples could precede its end or extend as far
as 1.831 ms beyond it. No number is an effective enable/disable timestamp or
continuous coverage guarantee. Native host exact-peer selection retained two
samples: one inside the peer's marked interval and one outside. The container
retained one inside sample. Equal Virtual time does not identify a Task's CPU.

Every normal window retains setup exec records before enable (one Native,
two Process), five mappings of the workload DSO, and exit records after
disable (one Native, two Process). The exported event attributes identify
the CPU event and the separately enabled dummy tracking event. Named CPU
disable preserves tracking; a bare all-event disable is not equivalent.

## Observer cost and limits

Observed elapsed medians in ms; three Runs each, with identical workload
marker instrumentation in every mode:

| Environment / Participant | Ordinary | Unmodified diagnostics | Notifications | Whole baseline | Window |
| --- | ---: | ---: | ---: | ---: | ---: |
| Native host / Native | 2257.0 | 2291.2 | 2260.9 | 2384.3 | 2414.2 |
| Native host / Process | 1275.2 | 1297.0 | 1280.5 | 1336.1 | 1314.5 |
| Container / Native | 2384.2 | 2401.6 | 2368.4 | 2438.3 | 2378.8 |
| Container / Process | 1295.4 | 1275.7 | 1279.3 | 1356.1 | 1335.9 |

Variation prevents an isolated constant observer-cost estimate; a lower median
does not establish negative or zero overhead. These measurements describe a
shared CI host and this optimized workload, not a general perturbation bound.
The proof makes no timing threshold, significance, exclusive CPU, target ECU
or WCET claim. Diagnostics and independent marker intervals retain local
Activation durations for further inspection. No overhead is subtracted.

## Edge, failure and storage results

- Empty and unreached selections run the workload without enabling CPU
  sampling; both retain zero CPU samples and remain distinct states.
- Delayed enable begins about 22 ms into known work. A host stop during the
  selected work ends sampling more than 100 ms before that work returns.
  The scheduler never waits for a command or acknowledgment.
- Ordinary/observed successful Runs and deliberate Participant failures
  retain equal Recording hashes and outcomes, with a 10-second Process
  response deadline and a 60-second whole experiment guard. A helper's return
  does not make the encompassing failed Activation completed.
- SIGKILL during late work leaves absent observer/Activation footers and
  an explicit unclosed state. Missing Native workload markers remain missing;
  Process helper intervals do not synthesize a closed runner Activation.
- A small send buffer and paused receiver drop 106–110 notifications.
  A separate one-page perf ring loses sample records while notifications
  remain complete. Each retains a PERF_RECORD_LOST record. Both differ from
  deliberately uncaptured CPU regions and from zero-sample work.
- The 64 KiB capture-limit cases stop sampling at the next 100 ms guard poll
  and retain files of 567,832–694,984 bytes. This is measured overshoot, not a
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

The correctness validator deliberately does not require SIGKILL/ENOSPC Runs
to succeed. Their actual Run/collector statuses, incomplete exports and
Recordings are retained and reviewed here. The production decision therefore
requires an independently surviving Run supervisor and a reviewed versioned
coverage/lifetime contract before reopening. See the
[decision and reopening criteria](../../../docs/profiling-investigation-decisions.md#late-capture-windows--361-defer-production-after-qualification).
No production CLI, ABI, Manifest, Recording format or supported schema changes.

## Retention and offline replay

`results.json` is the compact permanent summary. `contents.json` binds every
archived file by size/SHA-256; `archive.sha256` binds `portable-evidence.tar.gz`.
The archive contains exact proof sources, all raw control/notification/workload
events, Activation logs, sample timestamps/leaf symbols, metadata, statistics,
identities, diagnostics, Recordings and reports. Original `digests.json` also
names raw perf files omitted from this portable archive. Raw perf data remains
in the linked Actions artifact for its 30-day retention; offline replay needs
neither those files, profiler tools, Linux nor the workload binaries.
The portable payload is 67,607,689 bytes across 1,936 files, compressed to
10,195,469 bytes. Analysis reads files sequentially with a 256 MiB per-file
input cap; there is no interactive dataset or DOM.

```sh
cd proofs/capture-windows/evidence
shasum -a 256 -c archive.sha256
replay_dir=$(mktemp -d)
tar -xzf portable-evidence.tar.gz -C "$replay_dir"
for environment in "$replay_dir"/window-evidence/*/; do
  python3 "$replay_dir/proof-sources/validate.py" "$environment"
done
```

The four report-boundary regression tests pass. Repository build, C++ tests,
full pytest and determinism gates are recorded in the PR validation.
