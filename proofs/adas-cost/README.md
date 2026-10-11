# ADAS reference execution cost: native, process-isolated C library, FMU (#229)

```sh
proofs/adas-cost/run-proof.sh [evidence-directory] [measure.py options]   # needs docker and network
```

This measurement runs one declared reference workload through three
execution forms of the same C sources on Linux x86-64 and retains the
results. It establishes a repeatable procedure for the real-vECU decision of
[#125](https://github.com/Stevie1704/sil/issues/125). It does not close
#125: the workload is the repository's
[ADAS reference application](../../docs/adas-reference.md), not a vECU.
Its outcome is a reference baseline and **no framework change**.

Scope: measurement only. No Python or C++ rewrite, leased buffers, parallel
stepping, raw-sensor capacity claim, Native ABI, Step protocol, Manifest or
Recording change. ADRs 0001–0003 are unchanged.

## What runs where

| Step | Where | Network | Output |
| --- | --- | --- | --- |
| Image | `Dockerfile`: pinned Python base; Debian snapshot `build-essential` (GCC 12.2) and `cmake`, as in [`../adas-equivalence`](../adas-equivalence/README.md); SiL built from the checkout and installed into `/opt/sil` (native prefix with `sil-run` and `sil-run-instrumented`, wheel environment) | image build only | image |
| Measurement | `measure.py` with the installed `sil` environment, `--network none` | none | `results.json`, `results.md`, `work/` |

`measure.py` is the measurement command. It runs against installed SiL: it
imports SiL only from the installed wheel and starts only the installed
runners. `sil-run-instrumented` is not part of a product installation (the
runtime image does not contain it); the image builds it from the same kernel sources
and installs it beside `sil-run`. Outside the image, the command needs the
same on `PATH` (`sil-run`, `sil-run-instrumented`, `silschema`, `python3`
with the `sil` wheel) and a C compiler. `tests/test_adas_cost.py` runs it
against a staged installation.

Terms: a **form** is one execution form of the controller (table below). A
**Participant-Step** is one activation of one controller instance.

## The three forms

All Manifests come from one `reference_manifest` call in
[`examples/adas-reference/manifest.py`](../../examples/adas-reference/manifest.py).
`workload.only_the_controller_differs` requires that the documents are equal
once the controller entries are removed.

| Form | Controller entry | Where the application runs |
| --- | --- | --- |
| `native` | Native participant of `adas_reference.so` | inside `sil-run`, on the kernel thread |
| `process` | [`process_adapter.py`](../../examples/adas-reference/process_adapter.py) loads the same `adas_reference.so` with ctypes | a Python child process; Step protocol |
| `fmu` | SiL's FMI Importer over `AdasReference.fmu`, built from the same `adas_reference.c` | a Python child process; Step protocol; FMI 3.0 Co-Simulation |

The library, the archive and the timing harness are built in one
measurement with one compiler and `-O2 -ffp-contract=off`; their digests are
in `results.json`.

## The declared workload

`workload.declaration()` pins every quantity and `results.json` repeats it:

| Quantity | Value |
| --- | --- |
| controller Period | 10 ms, one Task, priority 0 |
| sensor Periods | radar 20 ms, camera 40 ms, ego motion 10 ms |
| list capacity / active objects | 8 / 8 radar and 8 camera objects in every list |
| Message sizes | `adas.ObjectList` 185 B, `adas.EgoMotion` 17 B, `adas.Command` 56 B |
| Burst depth | route capacity 3, fail on overflow; at most 1 Message per route per activation |
| fan-out | 1 subscriber per input Channel; Command Channels recorded, 0 subscribers |
| Latency | inputs 0, Commands one Period |
| route bounds | inputs as above; no route on Commands |
| Run lengths | `startup` 1 activation (10 ms), `ci` 20 activations (200 ms, the length of every authored maneuver), `long` 6000 activations (60 s) |
| instances | 1 and 4 controllers, each with its own Replay participant and input Recording |
| Recording | off (`--no-recording`) and on |

The maneuver is generated, not authored: the lead radar object approaches
from 50 m to 0.25 m every 2 s and a camera object confirms each radar
object, so every Run passes through the clear and the hazard mode. The
Command count per mode is in the results.

## Procedure

1. Build the artifacts and record their digests and the compiler.
2. Generate the inputs of each Run length, convert one Recording per
   instance with `sil recording csv` (`prepare.prepare_inputs`), and record digests.
3. Author the 36 Manifests (3 forms × 3 Run lengths × 2 instance counts ×
   Recording on/off) and check that only the controller entries differ.
4. Per row, run `sil-run-instrumented` twice for the copy, route and replay
   counters (`SIL_COPY_COUNTERS_OUT`), then the production `sil-run` once
   untimed (warm-up) and five times timed. Every Run has a 30 s Process
   response deadline; the deadline is not Manifest data. A failed Run stops
   the measurement with its stderr.
5. Start every Run through `run_measured` (`run_measured.c`). It forks the
   runner from a small process and reports wall-clock (fork to reap), CPU
   and peak RSS of the Run's process tree. On Linux, `ru_maxrss` survives
   `execve`: a Run started from the Python driver would report at least the
   driver's own peak.
6. Check, then derive the application cost and the estimates.

**Statistical policy.** One discarded warm-up Run per row, then five timed
Runs. The report states the median, with min and max; peak RSS is the
maximum. No confidence interval and no outlier rejection: five samples on a
shared CI runner support neither. Rows run in a fixed order, one at a time.
Every estimate is a difference of medians and carries its spread: the
summed max − min of every observation it comes from. `adaptation + routing`
nets the application from the per-Step difference, so its spread is that
difference's spread plus the application's own. An estimate is `resolved`
only when its magnitude exceeds its spread; otherwise the table marks it
`within spread`.

`--long-s`, `--warmup` and `--repeats` change the policy for a local run;
the values used are in `results.json`.

## Deterministic checks

The measurement exits 1 when one fails. Each is computed from counts or
bytes, never from timing.

| Check | What it proves |
| --- | --- |
| `only_the_controller_differs` | the three forms measure the same declared experiment |
| `counters_repeat` | the two instrumented Runs of a row report the same counters |
| `recording_equals_instrumented` | every timed production Recording equals the instrumented Recording byte for byte: the instrumentation does not change the observed behavior |
| `recording_changes_only_recorded` | with Recording off, every counter but `recorded` equals the Recording-on row |
| `forms_publish_identical_commands` | the process and FMU forms publish the native form's Commands byte for byte, and each Run publishes one Command per activation and instance |
| `application_computes_the_published_commands` | the in-process timing harness computes exactly the Commands the Runs published |

Every instrumented, warm-up and timed Run must exit 0, or the measurement
stops. A Recording-off Run writes nothing to compare, so its inertness rests
on its exit code and on `recording_changes_only_recorded`. The checks can
fail: `tests/test_adas_cost.py` replaces one form's Recording and requires
`forms_publish_identical_commands` to fail.

## What the measurement separates

| Cost | Source | What it includes |
| --- | --- | --- |
| startup | wall-clock of the `startup` Run (one activation), Recording off | runner start, Manifest parse, Replay open, and per form: library `dlopen` (native); Python interpreter, `sil` and ctypes imports, `dlopen` (process); the same plus FMI Importer imports, archive extraction, `modelDescription.xml`, `dlopen`, `fmi3InstantiateCoSimulation` and initialization (fmu). One activation is inside it. |
| FMU startup over process | difference of the two startup medians | estimate of the FMI Importer's own fixed cost beyond the ctypes adapter |
| application | `app_cost.c`: the application's receive and advance calls over the `long` inputs, timed in-process with `CLOCK_MONOTONIC` | the C computation alone, no SiL |
| per Participant-Step | (`long` − `ci`) wall-clock over the extra activations and instances | estimate of steady-state cost per controller activation: application, adaptation, routing and replay |
| adaptation + routing | per Participant-Step − application | estimate: Native ABI calls and kernel routing (native); plus Message decode, ctypes, JSON Step protocol and the pipe round trip (process, fmu); plus FMI variable setting and getting (fmu) |
| Recording | (`long` on − `long` off) wall-clock per Participant-Step | estimate of writing every input and Command Message |
| real-time factor | simulated duration / median wall-clock, per row | total, startup included |

What it cannot separate: replay cost from routing cost (every form replays
the same inputs, so it cancels in comparisons between forms, not in the
absolute figure); the Step protocol's encode from its pipe round trip; and
the cost inside the kernel from the cost inside a Process participant at
the per-Step level. The instrumented Runs report the kernel's own CPU and
RSS apart from the tree, once per row.

## Reference results

Measured by the `proof-adas-cost` workflow at commit `2647ae0` on a
GitHub-hosted `ubuntu-24.04` runner: Linux x86-64, 4 vCPUs, 15.6 GiB, glibc
2.36, CPython 3.13.7, GCC 12.2. The CPU model of this runner class is not
fixed (this run: AMD EPYC 9V45; an earlier run: Intel Xeon Platinum 8573C),
so compare figures only within one `results.json`. Every deterministic check
passed. The full tables are in [evidence/results.md](evidence/results.md).
All figures below are observational medians or estimates, not capacity
claims.

| | native | process | fmu |
| --- | --- | --- | --- |
| application alone, µs per activation | 1.39 | 1.39 | 1.39 |
| per Participant-Step, 1 instance, µs (estimate) | 2.9 | 47.4 | 62.6 |
| per Participant-Step, 4 instances, µs (estimate) | 2.8 | 65.6 | 87.3 |
| startup, 1 instance, ms | 4.8 | 54.2 | 75.8 |
| startup, 4 instances, ms | 5.6 | 204.4 | 294.2 |
| sim/wall, `ci` (0.2 s), 1 instance | 41.7 | 3.6 | 2.6 |
| sim/wall, `long` (60 s), 1 instance | 2728 | 177 | 133 |
| sim/wall, `long` (60 s), 4 instances | 816 | 33.7 | 25.0 |
| largest process RSS, MiB | 4.5–10.6 (kernel) | 20 per adapter | 25 per importer |

Reading:

- **The application is not the cost.** Its own computation over full 8 + 8
  object lists is 1.4 µs per activation. In the native form with one
  instance, adaptation, routing and replay add about 1.5 µs; with four
  instances the same residual (1.4 µs) is within its spread (2.2 µs), so
  this run does not resolve it. In the process form, the ctypes
  adapter and the Step protocol add about 46 µs; the FMI Importer adds
  about 61 µs. These agree in size with the ad-hoc observations of #125
  (~50 µs per Python Process participant).
- **Short Runs are startup.** A 200 ms CI Run of one Process instance spends
  most of its wall-clock before the first Step: 54 ms (process) and 76 ms
  (fmu) for one activation against 55 ms and 78 ms for twenty. Startup grows
  linearly with Process instances (about 50 ms and 73 ms each), as if they
  start one after another; this measurement does not show the mechanism. The FMI Importer costs about 22 ms
  per instance more than the ctypes adapter: imports, archive extraction,
  `modelDescription.xml` and instantiation (estimate; 21.6 ms against a
  spread of 6.9 ms with one instance, 89.8 ms against 47.7 ms with four).
- **Per-Step cost per instance rises with the instance count** in the
  Process forms (47 → 66 µs, 63 → 87 µs) and not in the native form. Four
  child processes and the runner share 4 vCPUs and alternate on pipes; this
  measurement does not separate scheduling from protocol cost.
- **Recording** costs about 1.7 µs per Participant-Step in the native form
  with one instance. In every other row the difference is within the
  spread: this machine and policy do not resolve it.

Which future requirement would make each cost material. The measurement
sets no threshold; each row names the quantity a stated requirement would be
checked against.

| Cost | Material when a requirement states | Check |
| --- | --- | --- |
| startup per Process instance (50–76 ms) | a CI matrix of many short Runs with a wall-clock budget | cases × instances × startup against the budget; at 0.2 s Runs startup is most of the time |
| per Participant-Step (47–87 µs Process, 2.9 µs native) | a target real-time factor R at Step period P with N Process participants | N × cost × R ≤ P; at P = 10 ms and R = 30 that is N × cost ≤ 333 µs |
| application compute | a real vECU Step cost | the #125 rule: at ≥ 250 µs per Step the costs above are noise; at ≤ 50 µs the transport dominates |
| Recording per Participant-Step | Recording of large Messages or many Channels | the [large-Message baseline](../../docs/bench/large-message-routing-baseline.md), not this workload |
| RSS per Process instance (20–25 MiB) | many instances or parallel Runs per host | instances × parallel Runs × RSS against host memory |

## Outcome and #125

The outcome is a **reference baseline with no framework change**. The
Manifest, Manifest hash, Step protocol, Arena layout, Native participant
ABI, Recording bytes and exit-code taxonomy are unchanged.

For [#125](https://github.com/Stevie1704/sil/issues/125): the reference
application's compute, 1.4 µs per Step, would fall in the ≤ 50 µs regime,
where the transport dominates and the Native participant door, not a faster
Process transport, is the lever (2.9 µs against 47–63 µs per Step here).
That is a statement about this repository-owned reference, not about a
vECU. #125 still requires the measured Step cost of at least one real vECU;
this measurement does not supply it and does not close #125. This procedure
(`run-proof.sh` with the vECU in place of the reference controller) is the
repeatable way to measure one.

Any future optimization needs its own bounded issue, justified by real
target measurements, that preserves what every Participant sees and the
output semantics. Nothing here authorizes a transport rewrite.

## Evidence

[`evidence/`](evidence/) holds what the `proof-adas-cost` workflow wrote on
a GitHub-hosted `ubuntu-24.04` x86-64 runner:

| File | Content |
| --- | --- |
| `results.json` | the declaration, machine, artifacts and input digests, Manifest hashes, every row's counters and samples, checks, application timings and estimates |
| `results.md` | the same as tables, generated |
| `image-id.txt` | the ID of the image this build made |

The artifacts, inputs, Manifests and Recordings are in `work/` of the
workflow artifact and are not committed. The estimates in the committed
files were re-derived from the retained samples after the residual spread
check was added; no Run was repeated, and every observation is the
workflow's.
