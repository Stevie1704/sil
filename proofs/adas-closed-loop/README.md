# ADAS reference controller in a closed loop over a plant FMU (#227)

```sh
proofs/adas-closed-loop/run-proof.sh [evidence-directory]   # needs docker and network
```

This proof closes an object-level loop around the
[ADAS reference controller](../../docs/adas-reference.md). The ACC plant FMU
of [proofs/acc-fmi](../acc-fmi/README.md) moves an ego vehicle and a lead
vehicle. Edge Participants derive processed radar, camera and ego
observations from plant truth. The controller commands an acceleration, and
an edge conversion applies it to the plant. The controller runs in two
forms from one declaration: its Native participant, and
[`AdasReference.fmu`](../adas-fmu/README.md) through SiL's FMI importer.

It is integration evidence for the reference product at object level. The
sensing is idealized: one object at the plant gap, straight ahead, with no
camera or radar physics, noise, range or field of view. There is no CAN
arbitration, Ethernet switch model, general solver or change to the
kernel, the Native ABI, the Step protocol or the Transport. There is no
claim of supplier compatibility or ADAS safety.

## What runs where

| Step | Where | Network | Output |
| --- | --- | --- | --- |
| Proof image | `Dockerfile`: the base, compiler and installed SiL of [`../adas-equivalence`](../adas-equivalence/README.md); PythonFMU3 0.3.4 and FMPy 0.3.26 by hash from [`../acc-fmi/requirements.lock`](../acc-fmi/requirements.lock); `AccPlant.fmu` built by [`../acc-fmi/build.py`](../acc-fmi/build.py) | image build only | image |
| Proof | `prove.py` with the installed `sil` environment, Linux x86-64 | none | every report and Run |
| Independent execution | `independent.py` with the base interpreter, which has FMPy and no SiL | none | one row per Sample time |

| File | Role |
| --- | --- |
| `loop.py` | the declaration: Schemas, Channels, priorities, Periods, cases, variants, the consumption table and its check, the contracts, the KPIs, the envelope, the deliberate failures and the failures |
| `edge.py` | the edge Participants: Maneuver, sensors and actuator, with the explicit conversions |
| `kpi.py` | the post-hoc KPIs, mode sequences, coverage and variant effects over a Recording |
| `independent.py` | the FMPy execution of both archives |
| `prove.py` | the proof: artifacts, Manifests, Runs, comparisons, effects, deliberate failures and failures |

## The loop

```text
maneuver --loop.lead--------> plant --loop.truth--> radar, camera, ego --adas.*--> controller
   \--loop.visibility--> radar, camera                                               |
plant <--loop.actuation-- actuator <--adas.command-----------------------------------/
```

| Participant | Kind | Role |
| --- | --- | --- |
| `maneuver` | Process (`edge.py`) | the authored lead acceleration for [t, t + 10 ms] and the lead visibility at t |
| `plant` | Process, `AccPlant.fmu` through `sil.fmi` | ego and lead motion; its Float64 variables carry the Channel fields by name |
| `radar`, `camera`, `ego` | Process (`edge.py`) | processed observations of the truth sampled at t |
| `controller` | Native library, or `AdasReference.fmu` through `sil.fmi` | the reference profile 3 |
| `actuator` | Process (`edge.py`) | the Command's acceleration as the plant's input |

The plant truth (`loop.truth`) and the Maneuver (`loop.lead`,
`loop.visibility`) are separate Channels. No sensor Channel carries them,
and no Interceptor acts on them.

### Consumption table

Every Participant starts at Virtual time 0 (offset 0). A lower priority
activates first within a Slot. The Native controller registers priority 0.
`loop.consumption_table` writes this table for each Manifest into
`manifests.json`, and `loop.consumption_findings` refuses a Manifest that
it does not allow.

| Participant | Period | Priority | Takes | Latency | Value taken at t | Route |
| --- | --- | --- | --- | --- | --- | --- |
| `maneuver` | 10 ms | −3 | nothing | | | |
| `radar` | 20 ms | −2 | `loop.truth`, `loop.visibility` | 10 ms, 0 | truth sampled at t, visibility at t | 2, 2 |
| `camera` | 40 ms | −2 | `loop.truth`, `loop.visibility` | 10 ms, 0 | truth sampled at t, visibility at t | 4, 4 |
| `ego` | 10 ms | −2 | `loop.truth` | 10 ms | truth sampled at t | 1 |
| `controller` | 10 ms | 0 | `adas.radar`, `adas.camera`, `adas.ego` | 0, 0, 0 | observations sampled at t, or held | 2 each |
| `actuator` | 10 ms | 1 | `adas.command` | 10 ms | the Command sampled at t | 2 |
| `plant` | 10 ms | 2 | `loop.lead`, `loop.actuation` | 0, 0 | values for [t, t + 10 ms] | 1, 1 |

| Channel | Publisher | Published in Slot t, describes | Latency |
| --- | --- | --- | --- |
| `loop.lead` | `maneuver` | lead acceleration over [t, t + 10 ms] | 0 |
| `loop.visibility` | `maneuver` | visibility at t (`sample_time_ns` t) | 0 |
| `loop.truth` | `plant` | the state at t + 10 ms, the end of the Step | 10 ms |
| `adas.radar`, `adas.camera`, `adas.ego` | sensors | Sample time t | 0 |
| `adas.command` | `controller` | Sample time t + 10 ms | 10 ms |
| `loop.actuation` | `actuator` | acceleration over [t, t + 10 ms] | 0 |

At every Slot t, in this order:

1. `maneuver` publishes the lead acceleration and the visibility.
2. The sensors due at t take the truth sampled at t, which the plant
   published at t − 10 ms, and publish observations with Sample time t.
3. `controller` takes them with age 0, holds the others, and publishes the
   Command for Sample time t + 10 ms.
4. `actuator` takes the Command sampled at t, which the controller
   published at t − 10 ms, and publishes the acceleration.
5. `plant` steps [t, t + 10 ms] and publishes the truth sampled at
   t + 10 ms.

The feedback Latencies, truth and Command, are one Step. So no Participant
consumes a value computed in its own Slot from its own output: there is no
algebraic loop, and the order of plant and controller within a Slot does
not change the result. A Message published by an FMU or by the controller
in Slot t describes t + 10 ms, and is consumed at t + 10 ms, never at t.
The check refuses a Manifest in which a Participant takes a value sampled
after its activation, a zero-Latency edge between equal priorities, a
Period that is not a whole number of Steps or that does not divide the
Duration (3.52 s), and an overflow policy other than `fail`. Each sensor
also refuses at run time any truth that is not sampled at its activation,
and the actuator any Command that is not sampled at its activation.

A route holds a Message from its publication until it is drained. The
truth route of `camera` holds the four truth Messages of one camera
Period. The Command route holds the Command due at t and the one the
controller publishes at t before `actuator` activates.

### Initial condition

The plant starts at ego position 0 m, both speeds 25 m/s, and lead position
equal to the case's initial gap (`--start initial_lead_position_m`). Its
held acceleration inputs start at 0. The sensors at t = 0 describe the
declared initial truth, because no truth Message exists before the plant's
first Step. `independent.py` reads the plant's outputs after
initialization and requires them to equal that declaration. The actuator
applies the declared initial acceleration, 0 m/s², at t = 0. The
controller form starts as in [#226](../adas-equivalence/README.md).

### Edge conversions

The plant's interface is Float64, the profile's is Float32, and the
importer refuses to bind one to the other. `edge.py` converts explicitly:

| Direction | Conversion | Refused |
| --- | --- | --- |
| truth to `x_m`, `relative_vx_mps`, `speed_mps` | rounded once to the nearest binary32 (`to_f32`) | a value that is not finite, or that has no finite binary32 |
| ego speed | as above | a negative speed, which profile 3 does not allow and the plant has no floor for |
| `acceleration_mps2` to `accel_mps2` | binary32 widened to binary64, which is exact | a Command that is missing, repeated or not sampled at the activation |

Units are SI on both sides, so no value is scaled. The plant's relative
speed and the profile's `relative_vx_mps` have the same sign: negative when
the lead comes closer.

### Sensing

Radar and camera each list the lead as one object: radar ID 1, camera ID 7,
`x_m` the plant gap, `y_m` 0, confidence 0.875. The radar reports the
relative speed; the camera reports none. When the Maneuver hides the lead,
each list is valid and empty. The camera list confirms the radar object
because both describe the same gap; the held camera list lags by up to
30 ms, far inside the 2 m association limit at the closing speeds of
these cases. A list's sequence counts the sensor's publications from 0,
also the ones an Interceptor drops.

## Cases

Each case is 352 activations of 10 ms. `modes` is the expected sequence of
Command modes; a start Sample time is predicted where the Maneuver and the
profile rules fix it.

| Case | Initial gap | Maneuver and faults | Expected modes (start) |
| --- | --- | --- | --- |
| `clear_road` | 60 m | the lead is never visible | CLEAR (10 ms) |
| `approach` | 30 m | lead −6 m/s² in [0.2, 1.7) s, +4 m/s² from 3.0 s | CLEAR, HAZARD, CLEAR |
| `cut_out` | 30 m | as `approach`; from 2.6 s the lead is hidden and accelerates at +4 m/s² | CLEAR, HAZARD, CLEAR (2.61 s) |
| `sensor_loss` | 60 m | `drop` on `adas.radar` for publications in [1.0, 1.3) s | CLEAR, SENSOR_UNAVAILABLE (1.04 s), CLEAR (1.31 s) |
| `near_start` | 7 m | none | HAZARD (10 ms), CLEAR |
| `approach.ideal` | 30 m | as `approach`, every sensor every 10 ms | CLEAR, HAZARD, CLEAR |
| `approach.latency` | 30 m | as `approach`, 10 ms Latency on every sensor Channel | SENSOR_UNAVAILABLE (10 ms), CLEAR (20 ms), HAZARD, CLEAR |

In `sensor_loss`, the radar list sampled at 0.98 s is held. It is fresh at
age 40 ms (t = 1.02 s) and stale at 1.03 s, so the Command for 1.04 s is
SENSOR_UNAVAILABLE and brakes. The list sampled at 1.3 s recovers. This is
the expected degraded behavior. In `cut_out`, the radar list at 2.6 s
lists no object, so the Command for 2.61 s is CLEAR and the braking is
released at 0.5 m/s² per activation.

## Acceptance

Everything below is declared in `loop.py` before any Run.

| Check | Rule |
| --- | --- |
| determinism | each Manifest runs twice; identical Recording bytes |
| coverage | one Command and one truth Message in every Slot 0 to 3.51 s; the last Command describes 3.52 s; Sample time = Slot + 10 ms; sequence from 1 |
| KPIs | gap ≥ 2 m, ego speed ≥ 0, commanded acceleration in [−3, 0] m/s², at every Sample time |
| modes | the case's sequence, at the predicted start Sample times |
| forms | the native and the FMU Manifest differ only in the controller entry |
| native against FMU | every Command and truth field exact, through `sil-compare` |
| each form against FMPy | Commands exact; truth within `atol` 1e-10 m or m/s and `rtol` 1e-12 |

### Reference model, sampling and Latency

Three Runs of `approach` separate the effects. `approach.ideal` samples
every sensor every 10 ms: the reference model on unheld truth. `approach`
adds the radar and camera hold. `approach.latency` adds 10 ms Latency to
the sensor Channels. Each variant differs from its baseline only in what
it isolates (`loop.effect_difference`). Each effect must change the
Commands, move the hazard onset later by 0 to 20 ms, and move the minimum
gap by at most 0.5 m. The hazard comes from the radar position and relative
speed; the camera only confirms the radar object within 2 m. A radar
observation is held for one Step at most, and the sensor Latency adds one
Step. So each variant can move the onset by two Steps at most, and only
later, because the closing speed grows and the gap shrinks until the
onset.

### Deliberate failures

| Failure | Case | Predicted |
| --- | --- | --- |
| a KPI that allows no braking | `sensor_loss` | first violation: `acceleration_mps2` −0.5 at Sample time 1.04 s |
| the faulted Run against the independent execution without the fault | `sensor_loss` | first divergence: `adas.command` `radar_age_ns` 20 ms, expected 0, at 1.01 s |

### Failures

Each Run below must exit with its code, name its cause, and leave no
importer or edge process behind. `malformed_list` runs in both forms.

| Failure | Change | Exit | Diagnostic names |
| --- | --- | --- | --- |
| `route_overflow` | `delay` of 50 ms on `adas.radar` for publications in [0.5, 0.6) s: three lists in a route of two | 1 | `subscriber route capacity exceeded: Channel 'adas.radar'` |
| `malformed_list` | `override` of `adas.radar` `count` to 9 at 0.2 s | 1 | `t=200000000 ns: radar.count 9 exceeds the capacity 8` |
| `future_truth` | the plant first in the Slot and truth at Latency 0, past the authoring check | 1 | `holds truth sampled at 10000000 ns, after the activation time` |
| `manifest_error` | a sensor config without the initial condition | 2 | `participant 'radar'`, `config keys` |

## Independent execution

[`independent.py`](independent.py) drives both archives with FMPy and
imports no SiL. It implements the schedule above with its own code: the
Maneuver windows, the sensor Periods, the binary32 rounding, the dropped
lists, the sensor Latency, the Command latency of one Step and the initial
acceleration. It writes one row per Sample time; `sil-csv` converts the
rows. It uses the same two archives, so it checks the coupling, the
sensing, the conversions and the schedule; it does not check the control
law. [#226](../adas-equivalence/README.md) checks the control law against
hand-enumerated trajectories.

## Preserved artifacts

The plant source, its dynamics and the ACC evidence are unchanged.
`prove.py` reads the plant archive's `resources/identity.json` and requires
its model, dynamics and exporter identity to equal the ones the
[multi-rate evidence](../acc-fmi/multirate-evidence/report.json) qualified.
Only the source revision and the archive digest name this build. It also
requires the plant interface that `loop.py` binds: Float64 inputs
`accel_mps2`, `lead_accel_mps2`, `initial_lead_position_m` and the six truth
outputs, with their units. The controller archive is held to its
[#225 pin](../adas-fmu/evidence/AdasReference.identity.json).

## Evidence

`run-proof.sh` writes into the evidence directory:

| File | Content |
| --- | --- |
| `summary.json` | each step's result and both archive digests |
| `artifacts.json` | the controller archive and its pin, the library, the plant identity and interface |
| `manifests.json` | per case, the form differences and the consumption table; per variant, its differences |
| `runs.json` | each Run twice: exit, time, byte identity, modes, KPIs, coverage, final Command |
| `independent.json` | each FMPy execution and the plant's initial outputs |
| `comparisons.json` | each comparison report and both contracts |
| `effects.json` | each effect against the envelope |
| `deliberate.json` | each deliberate failure, its prediction and what was observed |
| `failures.json` | each failure: exit, diagnostic, time and leftover processes |
| `image-id.txt` | the ID of the image this build made |

The Manifests, Recordings and archives are in `work/` and are not
committed. [`evidence/`](evidence/) holds the reports of one passing run,
written under linux/amd64 emulation on an arm64 host. The
`proof-adas-closed-loop` workflow runs the same proof on native Linux
x86-64 and keeps the evidence directory as an artifact.

## Host tests

[`tests/test_adas_closed_loop.py`](../../tests/test_adas_closed_loop.py)
runs what needs no plant FMU and no FMPy: the conversions, the edge
Participants, the consumption table and its refusals, the predictions, the
KPIs and the independent schedule. It runs every case, the variants, the
deliberate KPI failure and the failures through SiL with
[a stand-in plant](../../tests/participants/standin_acc_plant.py) that
steps the same dynamics under the importer's time convention, and one case
through both controller forms with a host build of the controller archive.
