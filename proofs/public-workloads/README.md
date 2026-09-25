# Public workloads for adoption acceptance (#178)

```sh
proofs/public-workloads/run-proof.sh [output-directory]   # default build/public-workloads
```

This qualifies one fully public starting workload for each adoption use case:

1. recorded input into a shared library;
2. recorded input into one FMU;
3. several coupled FMUs.

No company artifact, supplier relationship or vehicle is needed. The image
build is the only step that uses the network. It fetches every source in
[`sources.json`](sources.json) and stops unless each one matches its pinned
SHA-256 and size, or, for Git, its commit and tree. Qualification then runs
with `--network none` on Linux x86-64. It writes:

- `bundle/`: the offline acceptance bundle for #193 and #194. It holds the
  built library, the FMUs, the recording files, the independent reference
  traces and `handoff.json`. `bundle.json` has one digest per file.
- `evidence/`: the audits, `report.json` and the gate-test log.

A check that fails stops the proof, so a failed qualification cannot write a
passing report. `.github/workflows/proof-public-workloads.yml` runs the proof
on native Linux x86-64 and uploads both directories. Retained evidence from
that job is in [`evidence/`](evidence/).

## Selected artifacts

| Role | Artifact | Pin | Licence |
| --- | --- | --- | --- |
| Shared library | opendbc safety logic, built by its `libsafety` harness | commit `c4465696`, tree `760d44fd` | MIT |
| Recorded CAN | commaCarSegments `df5ad9d9ae9fd6cd/00000470--cb630a6b9d/46`, TOYOTA_RAV4_TSS2 | dataset revision `edb6480d`, SHA-256 `b0d19f7c…` | MIT |
| Recorded scalar data | JRC OpenACC `ASta_040719_platoon7.csv`, AstaZero | SHA-256 `2696ef06…` | CC BY 4.0 |
| Single and coupled FMUs | `AccController`, `AccPlant` from `proofs/acc-fmi` | archive digests in `bundle.json` | Apache-2.0 |
| Importer-compatibility fixtures | Modelica Reference FMUs | `v0.0.41`, SHA-256 `62babca7…` | BSD-2-Clause |

The ACC FMUs are written in this repository. They are not independent
supplier models. The recorded vehicles' responses are never used as the
models' expected output.

## What kind of evidence this is

| Kind | Library | Single FMU | Coupled FMUs |
| --- | --- | --- | --- |
| Realistic recorded stimulus | yes: 60 s of vehicle CAN, receive side | yes: 50 s of measured car-following | lead acceleration only |
| Independent execution of the same artifact | upstream `replay_drive` and a second driver | FMPy | FMPy |
| Numerical agreement | exact state per event | the control law to 1e-10 | lead speed to 1e-9 m/s at every sample |
| Agreement with a recorded observation | the vehicle's own `pandaStates.controlsAllowed` | none | none |
| Production-vehicle validation | no | no | no |

The same-artifact comparison checks integration only. It says nothing about
the physical validity of the ACC law or the safety logic.

## Shared library: opendbc `libsafety`

**Build.** Preparation calls the upstream build function
`libsafety_py._build_libsafety(release=True)` and pins its output. The flags
are the upstream ones (`-O0 -g`, UBSan). The build runs twice and the report
states whether the bytes are the same. The offline Run never compiles:
`libsafety_py.load(path)` loads the pinned file.

**Runtime dependency.** Because of the upstream UBSan flags, the library
needs `libubsan`. `report.json` → `shared_library.build.runtime_libraries`
lists every dependency. A runtime image without GCC's `libubsan1` cannot load
the library.

**API and lifecycle.**

1. Initialize with `set_safety_hooks(mode, param)`, which must return 0, then
   `set_alternative_experience(ae)`.
2. For each recorded `can` event, call `set_timer(timer_us(logMonoTime))`.
3. When warm, call `safety_tick()` and `safety_config_valid()`.
4. For each received frame, call `safety_fwd_hook(bus, address)`, then
   `safety_rx_hook(packet)`.

There is no shutdown call. `libsafety_workload.py` names every policy.

**Time injection.** `timer_us(t) = (t // 1000) % 0xFFFFFFFF`. This keeps the
upstream modulus exactly. The library reads no other clock.

**Initial state and warm-up.** The initial state comes only from the
recorded `carParams`: mode 2 (`toyota`), param 73, alternative experience 0.
Preparation refuses any other contract. `safety_tick` runs only when the
event is more than 1 s from both ends of the segment, as upstream does. The
segment has no `sendcan`, so the upstream steering-state seeding does not
apply. `controls_allowed` becomes true on the first `can` event, from the
recorded cruise state.

**Mutable state.** The library keeps its state in C globals. A second
`dlopen` of the same file in one process shares that state (see
`same_path_dlopen_shares_state`). One instance therefore needs one process.
In SiL, that is one Process participant per library instance.

**Recording.** `evidence/can-segment.json` gives:

- 6000 `can` events over 59.99 s, in recorded order;
- event intervals from 8.75 to 11.03 ms (median 9.76 ms);
- Bursts of 7 to 47 received frames per event;
- 188 received bus/address pairs, each with its rate;
- payloads of 1 to 8 bytes.

Every event is `can`, `carParams` or `pandaStates`. There is no `sendcan`,
so **no recorded transmit coverage exists**. Sources 128 and 130 are transmit
echoes and are skipped.

**Reference and failing variants.** The nominal replay:

- runs twice in separate processes, with identical traces;
- agrees with the upstream `replay_drive` on received frames, invalid frames
  (0) and tick validity;
- agrees with the vehicle's recorded panda `controlsAllowed` at all 600
  samples.

The panda ran different firmware, so that agreement supports the reference
but is not the reference. Two variants must diverge from the nominal trace
and invalidate the configuration. Their first divergence is in `report.json`:

| Variant | Error |
| --- | --- |
| `timer-in-ns` | a unit error: nanoseconds injected where microseconds are expected |
| `corrupt-0x260` | an input error: the Toyota checksum byte of steering-torque frame 0x260 is inverted |

## Recorded scalar data: OpenACC

**Recording.** `evidence/openacc-recording.json` gives:

- 9227 samples from 1.0 s to 923.6 s;
- every interval exactly 100 ms, with no gaps and no missing values;
- vehicle order Audi A8, Tesla Model 3, BMW X5, Audi A6, Mercedes A-Class;
- ACC on, minimum distance setting.

The publisher downsampled the RT-Range DGNSS recording to 10 Hz. Nothing
here generates, resamples or smooths a sample.

**Window.** The window is [300.0, 350.0] s inclusive: 501 samples. It holds
a lead braking from 27.6 to 16.1 m/s, a recovery and a re-acceleration.
Vehicle 1 is the lead and vehicle 2 is the ego.

**Conversion** (`openacc.py`):

- `t_ns` is the recording's own time, rebased to the window start and
  converted exactly on the 100 ms grid.
- `gap_m` is `IVS1`, the publisher's bumper-to-bumper spacing. The GNSS
  antenna separation is about 3.8 m larger and is never used.
- `relative_speed_mps` is `Speed1 − Speed2`.
- `ego_speed_mps` is `Speed2`.
- `lead_accel_mps2` is the forward difference of `Speed1`, held for its
  100 ms interval, with no filter. Integrated under that hold, it gives back
  every recorded lead speed.

`bundle/recordings/openacc-window.csv` keeps the original columns next to the
derived ones. `openacc-ATTRIBUTION.txt` states the source, the licence and
the changes.

## FMUs

**Audit.** `evidence/fmu-audit.json` records, for each ACC FMU:

- the archive digest, FMI version, instantiation token and exporter;
- the Co-Simulation capabilities and platforms;
- every variable with its type, causality, unit, start value and dimensions;
- `ldd` of the Linux binary.

The profile executed here is the profile the ACC evidence established in
[`../acc-fmi/INSTALL.md`](../acc-fmi/INSTALL.md): FMI 3.0 Co-Simulation,
Linux x86-64, scalar Float64 inputs and outputs, and a fixed communication
step.

**Single FMU, open loop.** `AccController` runs under FMPy on a 100 ms grid
with 501 steps. Sample 0 is its start value. Sample *k* is set at *t_k* and
held for [*t_k*, *t_k+1*). The command it produces is observed at *t_k+1*,
so the last command is at 50.1 s and the final recorded sample is covered.
The trace must:

- match `command_for` of every sample within 1e-10 absolute and 1e-12
  relative;
- repeat exactly on a second run;
- diverge under a one-period input shift. The first divergence is in the
  report.

**Coupled FMUs, closed loop.** `AccPlant` and `AccController` run on the
qualified 10 ms baseline grid:

- sensing and command each delayed by one period;
- the plant's authored initial state (both vehicles at 25 m/s, 60 m apart);
- the measured lead acceleration held per recorded sample.

The ego is the plant's, closed through the controller. The recorded ego is
never replayed into the loop, because that would remove the feedback under
test. The plant's lead speed therefore follows the recorded change from its
own 25 m/s start.

The maneuver qualifies only if, at every recorded sample, the lead speed is
within 1e-9 m/s of 25 + (v₁(t) − v₁(300 s)). A one-sample maneuver shift must
fail that check. The loop must also:

- hold a gap of at least 5 m;
- keep the command within [−3, 1.5] m/s²;
- keep every speed at 0 m/s or higher;
- repeat exactly on a second run.

**Reference FMUs.** `evidence/reference-fmus.json` classifies every archive
in `v0.0.41` against the qualified profile, with every reason when it falls
outside. Inside the profile: FMI 3.0 `BouncingBall`, `Dahlquist`, `Roberts`
and `VanDerPol`. Each one runs under FMPy from its default start for at most
10 s, as an importer smoke test. That is not a SiL result. Roberts' default
experiment runs to 1e8 s on a 1e-3 s fixed internal step, so it is not run in
full. Outside the profile:

- every FMI 2.0 archive;
- `Clocks` (no Co-Simulation interface, Clock and Int32 variables);
- `Feedthrough` (non-Float64 types);
- `Resource` and `Stair` (Int32 outputs);
- `StateSpace` (array variables).

These are reported, not adapted. No expansion to FMI 2.0 or to other
variable types follows from them.

**Not executed.** Project Chrono vehicle FMUs stay a later third-party
dynamics candidate. They need a build and a capability inspection, and
nothing here claims they are compatible. esmini remains qualified by
[`../esmini`](../esmini/README.md) as a synthetic scenario fallback.

## Handoff to #193 and #194

`bundle/handoff.json` is generated from the same constants the checks
enforce. Where it and this document disagree, `handoff.json` is bound to the
evidence. It names, per workload:

- the artifact path and digest;
- the API and lifecycle;
- the Channel conversion;
- the period, Duration, start values, input hold and Latency;
- the observation convention;
- the comparison policy and coverage;
- the reference trace digest;
- the first divergence of each failing control.

The two conventions a SiL Run most easily gets wrong are these:

- **Library.** A Burst must be processed at the instant it names: Latency 0,
  or an equivalent same-Slot delivery. Its timer is
  `timer_us(first_log_mono_ns + virtual time)`. The recorded intervals are
  not a fixed grid, so a fixed-Period replay requantizes them. That is a
  different workload and needs its own policy (#185).
- **Single FMU.** An output published in Slot *t_k* describes *t_k* + 100 ms.
  Compare it with the reference row at that time. The Run's Duration is
  50.1 s.

## Resource observations

`report.json` records wall-clock throughput for each workload and the peak
resident memory of the child processes. These are observational, from one CI
runner. The workload is a small public decision-logic library and a
Python-exported ACC model. It is **not** a representative real-vECU
measurement and does not satisfy #125.

## Limits

- The library slice is receive-side only. Transmit decisions need a public
  segment with `sendcan` or separately declared generated messages.
- The ACC law and plant are repository-authored teaching models. Agreement
  shows correct execution, not ADAS behavior.
- Only the lead acceleration in the coupled loop is measured. The plant's
  initial state is authored.
- Linux x86-64 only. No cross-platform bit-exactness is claimed.
