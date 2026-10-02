# Native ADAS reference application

[Documentation index](README.md) · [Repository overview](../README.md)

Commands below run from the repository root unless stated otherwise.

## What it is

[examples/adas-reference/](../examples/adas-reference/) is a small C
application that receives bounded lists of processed radar and camera objects
and ego motion, each sensor at its own Period, holds the last accepted
observation of each sensor, and commands a longitudinal acceleration. It runs through the
[Native participant ABI](../include/sil/participant.h), and its output is
compared with hand-enumerated expected trajectories. It is intentionally
simplified example coverage for the path C application → Native participant
→ Recording → independent comparison. Its constants define test behavior, not
vehicle requirements. It makes no vendor, perception-accuracy, sensor-fusion
or safety claim. [The scoped decision](decisions/222-native-adas-reference.md)
records why it exists.

| File | Role |
| --- | --- |
| `adas_reference.h`, `adas_reference.c` | the application: its own API, caller-owned instances, no SiL include |
| `sil_adapter.c` | the Native participant: exports `sil_participant_init`, checks each list's bounds and converts Messages to the application's types |
| `process_adapter.py` | the same library as a process-isolated Process participant: loads it with `ctypes`, takes the Native config, checks the same bounds |
| `schemas.json` | the SiL Schemas; `silschema` generates `adas_messages.h` from them |
| `maneuvers/<name>.csv` | the authored radar and camera object lists and ego speed of each maneuver |
| `maneuvers/<name>.expected.csv` | the expected trajectory of each maneuver, enumerated by hand |
| `maneuvers/cadence.<experiment>.expected.csv` | the expected trajectory of each [experiment](#experiments), enumerated by hand |
| `mapping.json`, `expected-mapping.json`, `contract.json` | the `sil-csv` mappings and the `sil-compare` contract of one maneuver, without a Channel prefix |
| `prepare.py` | expands every maneuver to the flat Schema form, converts it with its Channel prefix and writes its contract |
| `manifest.py` | the Run: one Replay participant and one controller per maneuver, by default the Native participant of one library; the experiments and their authoring checks |
| `run.sh` | the one-command demonstration over installed SiL interfaces |

## Run it

With an installed prefix (`sil-run`, `silschema`, `include/sil`) and the
`sil` wheel on `PATH`:

```sh
examples/adas-reference/run.sh "$(mktemp -d "$HOME/sil-adas.XXXXXX")"
```

`make example-adas-reference` stages the build into a prefix and runs the
same script. The script:

1. generates `adas_messages.h` with `silschema`;
2. builds the library, and the same sources with
   `-DADAS_REFERENCE_WRONG_SIGN` and with `-DADAS_REFERENCE_ARRIVAL_TIME`,
   against the installed `include/sil`;
3. expands and converts the maneuvers with `prepare.py`;
4. runs the reference Manifest and each experiment Manifest twice and
   requires identical Recording bytes (`cmp`);
5. compares each maneuver's and each experiment's Commands with its expected
   trajectory;
6. runs the wrong-sign build and requires the `hazard` and `ordering`
   comparisons to fail (exit 1). This is the control that shows the
   comparison detects a coordinate sign error;
7. runs the arrival-time build under the `delay` experiment and requires its
   comparison to fail (exit 1). This is the control that shows the
   comparison detects an age counted from arrival instead of from the Sample
   time.

## Reference profile

The profile is `sil.adas-reference.radar-camera`, version 3. A change to a
field, a unit, a constant, the capacity or the time convention below makes a
new version. Each Native config names the profile and its version, and the
library refuses any other. Version 1 carried one object per sensor; version
2 carried bounded lists and consumed only observations sampled at the
activation time. Version 3 holds the last accepted observation of each
sensor, judges its freshness from its Sample time, and adds the ages and an
ignored-observation counter to the Command. It replaces version 2.

The freshness limits, the sequence rule and the treatment of future Sample
times below are reference policies for test coverage. They are not
production stale-data requirements; a selected target replaces them.

### Frame and units

All detections are in one ego frame, `frame_id` 1: x forward, y left, in
metres. `relative_vx_mps` is the object's longitudinal speed relative to the
ego, in m/s; it is negative when the object comes closer. Speeds are in m/s,
accelerations in m/s², times in nanoseconds of Virtual time. A list in any
other frame is rejected; the profile never transforms coordinates.

### Inputs

Each maneuver has three input Channels: `radar` and `camera` carry an
`adas.ObjectList`, `ego` carries an `adas.EgoMotion`. Each sensor publishes
at its own Period. The `cadence` maneuver uses the reference Periods: radar
20 ms, camera 40 ms, ego motion 10 ms. The other maneuvers sample every
sensor every 10 ms, and some omit observations.

An object list is one sensor's complete set of processed objects at one
Sample time. Its objects are flat arrays of capacity 8 and an active count:
elements `[0, count)` are the active objects, in any order. An accepted list
replaces the held list of its sensor; the controller keeps no track, so an
object that is not listed has disappeared. The capacity
of 8 is an example limit for test coverage, not a sensor or hardware
recommendation.

| Schema | Field | Type | Meaning and validity |
| --- | --- | --- | --- |
| `adas.ObjectList` | `sample_time_ns` | u64 | the time the list describes; not after the activation time that receives it |
| | `sensor_id` | u32 | `1` on the radar Channel, `2` on the camera Channel; any other value is rejected |
| | `frame_id` | u32 | `1`, the ego frame; any other value is rejected |
| | `sequence` | u32 | the sensor's message counter; increases within a Run, with no wrap |
| | `count` | u32 | active objects, `0` to `8`; a larger count is rejected, never truncated |
| | `validity` | u8 | `1` valid, `0` the sensor has no usable list; any other value is rejected |
| | `object_id` | i32[8] | active IDs `>= 0` and unique within the list; `-1` is reserved for "no selection" |
| | `x_m`, `y_m` | f32[8] | object position; finite |
| | `relative_vx_mps` | f32[8] | relative longitudinal speed; finite. The camera reports no speed: `0` |
| | `confidence` | f32[8] | detection confidence; finite, in `[0, 1]` |
| `adas.EgoMotion` | `sample_time_ns`, `sequence` | u64, u32 | as in the list |
| | `validity` | u8 | as in the list |
| | `speed_mps` | f32 | ego speed; finite and `>= 0`. Required sensing; profile 3 uses only its validity, Sample time and sequence |

Radar and camera object IDs are independent: the same number on both
Channels names unrelated objects. Every inactive element is zero, all bits,
in every array. The layout is packed (`silschema`), so there is no padding
byte to clear. A publisher zeroes the inactive elements before it publishes;
the adapter rejects a list that does not.

Every Schema name and field name is a valid C and C++ name, because
`silschema` refuses a name that is not. The naming rule is in the
generated-header contract in [SUPPORT.md](../SUPPORT.md#covered-by-the-compatibility-policy).

The adapter checks `count` against the capacity before it reads an element,
then checks that every inactive element is zero. The application then checks
every header field and every active element, also when `validity` is 0. A
Sample time after the activation time is malformed. A failed check fails the
Run (exit 1). The diagnostic names the Participant, the activation time, the
sensor, the field and, for an array, the element:

```text
participant 'live' failed: t=20000000 ns: radar.x_m[0] is not finite: nan
participant 'live' failed: t=0 ns: radar.count 9 exceeds the capacity 8
participant 'live' failed: t=10000000 ns: radar.object_id[1] 7 repeats radar.object_id[0]
participant 'cadence' failed: t=40000000 ns: camera.sample_time_ns 50000000 is after the activation time 40000000; a future Sample time is malformed
```

A repeated or decreasing `sequence` is not malformed: the observation is
ignored and counted, and the Run continues. The counter is part of the
Command, so the comparison checks it.

### Output

| Schema | Field | Type | Meaning |
| --- | --- | --- | --- |
| `adas.Command` | `sample_time_ns` | u64 | `t + 10 ms`, the end of the advanced interval |
| | `sequence` | u32 | activations completed, from 1 |
| | `mode` | u32 | `0` CLEAR, `1` HAZARD, `2` SENSOR_UNAVAILABLE |
| | `selected_object_id` | i32 | the selected radar object's ID, or `-1` for none |
| | `target_acceleration_mps2` | f32 | the acceleration the mode asks for |
| | `acceleration_mps2` | f32 | the commanded, rate-limited acceleration |
| | `radar_age_ns`, `camera_age_ns`, `ego_age_ns` | i64 | the age at t of the sensor's held observation, valid or not; `-1` while nothing is held |
| | `ignored_observations` | u32 | observations ignored for their sequence since the start of the Run |

### Behavior

At each activation t:

0. **Receive.** The controller drains each input route, radar, camera, then
   ego, and receives every delivered Message in Publish order. An
   observation whose `sequence` does not exceed the sensor's held sequence
   is a duplicate or a regression: it is ignored, counted, and does not
   refresh the age. Any other observation, valid or not, becomes the
   sensor's held observation.
1. **Availability.** A sensor is available when it holds an observation
   with `validity` 1 whose age is within the sensor's freshness limit. The
   age is t minus the observation's `sample_time_ns`; publication and
   arrival time do not count. The limits are radar 40 ms, camera 80 ms and
   ego motion 20 ms. An age equal to the limit is fresh, a greater age is
   stale. Before its first observation a sensor is unavailable; after an
   observation with `validity` 0 it is unavailable until a later valid one.
   When any of the three sensors is unavailable, the mode is
   SENSOR_UNAVAILABLE and no object is selected. A valid list with `count` 0
   is available: it reports that the sensor sees nothing.
2. **Eligibility.** An object is eligible when `x > 0`, `|y| <= 1.5 m` and
   `confidence >= 0.5`.
3. **Confirmation.** A radar object is confirmed when it is eligible and at
   least one eligible camera object lies within `|x_camera − x_radar| <= 2 m`
   and `|y_camera − y_radar| <= 0.5 m`. Association is geometric: radar and
   camera IDs are never compared.
4. **Selection.** The selected object is the confirmed radar object with the
   smallest x; at equal x, the one with the smaller radar ID. IDs are unique
   within a list, so the selection does not depend on list order. With no
   confirmed object, the mode is CLEAR and no object is selected.
5. **Hazard.** For the selected object only, closing speed is
   `max(0, −relative_vx)`. A hazard is `x < 8 m`, or closing speed `> 0` and
   `x / closing speed < 2 s`. Both comparisons are strict, so `x = 8 m` and a
   time to collision of exactly 2 s are not hazards. A zero closing speed is
   never a time-to-collision hazard. A hazard sets mode HAZARD; otherwise the
   mode is CLEAR. A farther object is not considered, even when it closes
   faster.
6. **Target.** The target acceleration is `hazard_acceleration_mps2` for
   HAZARD and SENSOR_UNAVAILABLE, otherwise 0.
7. **Rate limit.** The commanded acceleration starts at 0 and moves toward
   the target by at most `max_change_mps2` per activation.

Steps 2 to 7 use the held radar and camera lists, which can have different
Sample times: a radar list sampled at 140 ms is combined with the camera
list sampled at 120 ms until the next camera list arrives.

With every sensor sampled at each activation, the steps give the results of
profile 2, and with at most one object per sensor those of profile 1. The
eight earlier maneuvers keep their modes and accelerations; their expected
trajectories add the ages and the counter.

### Parameters

| Config key | Type | Range | Reference value |
| --- | --- | --- | --- |
| `profile` | string | `sil.adas-reference.radar-camera` | |
| `profile_version` | integer | `3` | |
| `radar`, `camera`, `ego`, `command` | string | declared Channels | `<maneuver>.radar`, … |
| `period_ns` | integer | `10000000` only | `10000000` |
| `hazard_acceleration_mps2` | number | finite, `[-10, 0)` | `-3` |
| `max_change_mps2` | number | finite, `(0, 10]` | `0.5` |

The config is a flat JSON object. A missing, unknown or repeated key, a
value of the wrong kind, a value outside its range, or another Period fails
initialization before the first Step, as a Manifest error (exit 2). A
different Period is rejected, never rescaled.

### Time, scheduling and replay order

The controller is one Native Task: Period 10 ms, offset 0, priority 0. Each
maneuver's Replay participant has no priority, so it publishes the Messages
of a Slot before every activation of that Slot, in Publish order. Input
Channels have `latency_ns` 0, so a Message published in Slot t is drained by
the activation at t. An observation sampled at t is therefore received at t
with age 0, unless an Interceptor or a Latency delays it.

Each input route holds three Messages and fails the Run on overflow. A
delayed Message holds its place in the route from its publication until it
is drained, and a later Message on the same Channel waits behind it: routes
deliver in Publish order, never by delayed time. The `delay` experiment
keeps three radar lists in one route.

The activation advances the state over [t, t + 10 ms] and publishes, in
Slot t, a Command with `sample_time_ns` t + 10 ms. The Recording stores the
publication Slot t. This is the convention of the [FMU importer](fmi.md):
the comparison contract uses an `actual_offset_ns` of one Period.

A change to this schedule changes the result. `manifest.py` accepts an
input Latency of 0 or one Period: one Period delivers every observation one
activation later, and the `late` experiment compares that predicted
trajectory. It rejects every other input Latency and every replay priority
before the Run, with `ExperimentError`, because the profile predicts no
trajectory for them:

```text
input latency 20000000 ns: the profile predicts input Latency 0 or one controller Period (10000000 ns) only
replay priority 1: the replay publishes before every activation of its Slot; the profile does not predict a replay ordered among the activations
```

### Arithmetic and tolerance

Each f32 input is converted once to binary64. All reference arithmetic is
binary64. The two output accelerations are rounded to f32 once, when the
Command is written. The comparison contract is exact for every field: integer
fields are `"exact"` and both accelerations are `{"atol": 0, "rtol": 0}`.
Ages are integer nanoseconds and the freshness comparisons are integer
comparisons. The accelerations are exact because the reference values are
multiples of 0.5, which binary32
and binary64 represent exactly. Each endpoint case in the maneuvers (`x = 8`,
a time to collision of exactly 2 s, the association and eligibility limits)
uses binary32 values for which the comparison is exact. Selection compares
binary32 positions converted to binary64, so equal authored distances are
exactly equal. Where the arithmetic
rounds, as in a time to collision just below 2 s, the result is far from the
threshold compared with the rounding error.

Build constraints: IEEE 754 binary64 `double`, round to nearest, and no
value-changing optimization. The source refuses `-ffast-math` at compile time.
The CMake targets and `run.sh` build with `-ffp-contract=off`, because Clang
contracts floating-point expressions by default, also in ISO C mode. Profile 3
has no expression that a contraction could fuse; the flag keeps that true when
the arithmetic changes.

### Lifecycle and ownership

The application has `adas_ref_init`, `adas_ref_receive_radar`,
`adas_ref_receive_camera`, `adas_ref_receive_ego`, `adas_ref_advance`,
`adas_ref_reset` and `adas_ref_terminate`. The caller passes each delivered
observation to a receive function, in Publish order, then advances at the
same t. The caller owns each `adas_ref_instance`, and the held observations
live in it; reset forgets them. The
application keeps no global state, starts no thread, opens no file or
socket, reads no clock, and allocates no memory. All work happens inside
the registered Task, with the Virtual time the kernel passes.

The adapter allocates one controller per Manifest entry and passes it as the
Task's `user` pointer. One loaded library therefore backs all
Participants of the Run. Native ABI v1 has no termination callback: a
controller lives until the runner process exits, which reclaims it, and the
adapter never calls `adas_ref_terminate`. The application holds no other
resource, so this loses nothing. This example does not add a callback to the
ABI.

## Maneuvers and the oracle

Each maneuver is 20 activations, from 0 ms to 190 ms. The expected
trajectories are written row by row from the behavior above, and compare
every output field, ages and counter included, up to the final Sample time
200 ms. The oracle is
`sil-compare` against these rows: it does not call the C application, import
its code, or use the output of an earlier Run.

An authored row is one activation, `time_ms,radar,camera,ego_speed_mps`. A
list cell is empty for no Message, `invalid` for a list with validity 0,
`empty` for a valid list without objects, or up to eight objects separated
by `;`. A radar object is `id x_m y_m relative_vx_mps confidence`, a camera
object `id x_m y_m confidence`. The ego cell is empty, `invalid` or a speed.
`prepare.py` expands each row to the flat Schema form: the header, the
active objects first and zero in every inactive element, one CSV column per
array element. It rejects a list longer than 8 rather than truncate it.
The sequence of every observation is its row index, so it increases within
the maneuver. `sil-csv` then converts the expanded CSV under `mapping.json`. Its receipt
records the digests of the expanded CSV, the prefixed mapping and the
Recording; `prepare.py` writes identical bytes on every run.

| Maneuver | What it shows |
| --- | --- |
| `clear` | a confirmed object 40 m ahead with zero closing speed: CLEAR, acceleration 0 |
| `hazard` | a confirmed object closing at 25 m/s: HAZARD, the rate limit from 0 to −3 m/s² in six activations |
| `release` | a hazard for eight activations, then an opening object: back to 0 at 0.5 m/s² per activation |
| `unavailable` | no ego speed for two activations and no camera for five: SENSOR_UNAVAILABLE, then CLEAR with recovery |
| `boundaries` | one case per row: `x = 8 m`, time to collision exactly 2 s, the association limits `2 m` and `0.5 m` and values just past them, `|y| = 1.5 m`, confidence `0.5`, `x = 0`, zero and opening closing speed, selected IDs `0` and `2147483647`, an empty radar list, an empty camera list, equal IDs far apart, and different IDs close together |
| `occupancy` | empty, single and full (8) lists; a full list whose nearer objects are unconfirmed, ineligible or of low confidence; the same lists with the camera array permuted; validity 0 on each sensor in turn; a full list whose nearest object is a hazard |
| `turnover` | objects that appear, disappear and reappear, with no retained track; camera IDs that equal or swap with radar IDs; camera detections that match no radar object, also with an equal ID; the association limits |
| `ordering` | equal distances broken by the smaller radar ID, at IDs `0` and `2147483646`; the same lists permuted, up to eight objects at one distance; a nearer non-hazardous object selected ahead of a farther closing one |
| `cadence` | radar every 20 ms, camera every 40 ms, ego motion every 10 ms; a near radar object from 140 ms that the held camera list confirms only from 160 ms; the base of the [experiments](#experiments) |
| `freshness` | radar first at 20 ms and camera first at 40 ms; each sensor at its exact freshness limit (fresh) and one Period past it (stale); recovery with the next observation |

The first five are the profile 1 maneuvers, with each object as a list of
one and each "no object" as an empty list. Their modes and accelerations are
unchanged.

The wrong-sign build computes closing speed from `+relative_vx`. The hazard
comparison then fails at the first observation: mode 0 where 1 is expected.

### Expected deliveries, held values and ages

The rows below are part of the enumerated trajectories. An activation
receives the listed observations, named by their Sample time in ms; the
ages are those of the held observations. `U` is SENSOR_UNAVAILABLE, `C`
CLEAR, `H` HAZARD. The acceleration is the one commanded for the Sample
time t + 10 ms.

| Case | t (ms) | Delivered | Held radar, camera, ego | Ages (ms) | Mode | Acceleration |
| --- | --- | --- | --- | --- | --- | --- |
| `cadence`: first activations | 0 | radar 0, camera 0, ego 0 | 0, 0, 0 | 0, 0, 0 | C | 0 |
| | 10 | ego 10 | 0, 0, 10 | 10, 10, 0 | C | 0 |
| | 20 | radar 20, ego 20 | 20, 0, 20 | 0, 20, 0 | C | 0 |
| | 30 | ego 30 | 20, 0, 30 | 10, 30, 0 | C | 0 |
| `cadence`: held camera list | 140 | radar 140 (near object 31), ego 140 | 140, 120, 140 | 0, 20, 0 | C, object 30 | 0 |
| | 160 | radar 160, camera 160, ego 160 | 160, 160, 160 | 0, 0, 0 | H, object 31 | −0.5 |
| `late`: first activations | 0 | nothing | none | −1, −1, −1 | U | −0.5 |
| | 10 | radar 0, camera 0, ego 0 | 0, 0, 0 | 10, 10, 10 | C | 0 |
| `freshness`: missing initial data | 0 | ego 0 | none, none, 0 | −1, −1, 0 | U | −0.5 |
| | 20 | radar 20, ego 20 | 20, none, 20 | 0, −1, 0 | U | −1.5 |
| | 40 | all three at 40 | 40, 40, 40 | 0, 0, 0 | C | −1.5 |
| `freshness`: ego limit 20 ms | 70 | nothing | 60, 40, 50 | 10, 30, 20 | C | 0 |
| | 80 | radar 80, camera 80 | 80, 80, 50 | 0, 0, 30 | U | −0.5 |
| | 90 | ego 90 | 80, 80, 90 | 10, 10, 0 | C | 0 |
| `freshness`: radar limit 40 ms | 140 | ego 140 | 100, 80, 140 | 40, 60, 0 | C | 0 |
| | 150 | ego 150 | 100, 80, 150 | 50, 70, 0 | U | −0.5 |
| `freshness`: camera limit 80 ms | 160 | radar 160, ego 160 | 160, 80, 160 | 0, 80, 0 | C | 0 |
| | 170 | ego 170 | 160, 80, 170 | 10, 90, 0 | U | −0.5 |
| | 180 | radar 180, camera 180, ego 180 | 180, 180, 180 | 0, 0, 0 | C | 0 |
| `delay`: delayed old list | 80 | camera 80, ego 80 | 40, 80, 80 | 40, 0, 0 | C | 0 |
| | 90 | ego 90 | 40, 80, 90 | 50, 10, 0 | U | −0.5 |
| | 110 | radar 60, ego 110 | 60, 80, 110 | 50, 30, 0 | U | −1.5 |
| `delay`: simultaneous arrival | 130 | radar 80, 100, 120 in that order, ego 130 | 120, 120, 130 | 10, 10, 0 | C | −1.5 |

At 110 ms of `delay`, the radar list sampled at 60 ms arrives 50 ms after its
Sample time. It is held, but its age is 50 ms, so it is stale on arrival:
the controller does not treat it as new sensing. The arrival-time build
counts its age from 110 ms, reports age 0 and mode CLEAR, and the comparison
fails at the observation 120 ms on `mode`: 0 where 2 is expected.

## Experiments

An experiment runs the `cadence` maneuver alone, in its own Manifest that
differs from the nominal `cadence` Manifest only by the declared Interceptors
or the input Latency. Each is compared with
`maneuvers/cadence.<experiment>.expected.csv`, and each repeats
byte-identically. The Interceptors act on the Run's input Channels only: the
authored input Recording, the expected Recording and the comparison stay
outside every faulted path.

| Experiment | Declaration | What it shows |
| --- | --- | --- |
| `drop` | `drop` on `camera` for publications in [40, 100) ms | lost camera lists: fresh at age 80 ms (t = 80 ms), stale from 90 ms, recovery with the list sampled at 120 ms |
| `delay` | `delay` of 50 ms on `radar` for publications in [60, 100) ms | a delayed old list is stale on arrival; three lists arrive in one Slot, in Publish order; recovery at 130 ms |
| `rewrite` | `override` of `radar.validity` to 0 in [100, 120) ms; `override` of `ego.sequence` to 14 in [150, 180) ms | an invalid list makes radar unavailable until the next valid one; three duplicate ego sequences are ignored and counted, and ego motion goes stale at 170 ms |
| `late` | `latency_ns` of one Period on every input Channel | every observation one activation later: unavailable at 0 ms, then ages one Period greater |

The tests in
[tests/test_example_adas_reference.py](../tests/test_example_adas_reference.py)
also show that a delay of 70 ms in the same window fills four places of a
route that holds three, and fails the Run with the route diagnostic; that
an Interceptor writing a future Sample time fails the Run as malformed
input; and that an expectation with one wrong age fails the comparison at
that age's Sample time.

The tests in
[tests/test_example_adas_reference.py](../tests/test_example_adas_reference.py)
also run two instances of the library with different lists, and require
each to publish what it publishes beside the others. A live stimulus
Participant delivers what `prepare.py` and `sil-csv` refuse to write:
nonfinite values, a count above the capacity, nonzero inactive elements,
negative and repeated IDs, invalid confidence, a wrong frame or sensor ID,
future and past Sample times, duplicate and decreasing sequences, a list
repeated in one Slot, and four lists in one Slot, which overflow the finite
Subscriber route of capacity 3.

## FMI 3.0 export

[examples/adas-reference/fmu/](../examples/adas-reference/fmu/) packages the
same application behind an FMI 3.0 Co-Simulation interface:
`AdasReference.fmu`. It is the reference acceptance artifact for SiL's FMI
importer: the FMU [#189](https://github.com/Stevie1704/sil/issues/189) and
[#190](https://github.com/Stevie1704/sil/issues/190) must be able to drive.
It is repository-owned. It is not a supplier FMU and makes no supplier
compatibility claim. [proofs/adas-fmu/](../proofs/adas-fmu/README.md)
builds, audits and checks it on Linux x86-64 and keeps the evidence.

| File | Role |
| --- | --- |
| `fmi3_controller.c` | the FMI 3.0 functions around `adas_reference.c`; it adds no control behavior |
| `modelDescription.xml` | the interface; `package.py` writes the instantiation token into it |
| `package.py` | compiles with fixed flags, writes the archive and its identity |
| `fmi3/` | the FMI 3.0.2 headers and their BSD-2-Clause license, unmodified |

### Variables

Each Schema field is one variable named `<channel>.<field>`: `radar.`,
`camera.`, `ego.` (inputs) and `command.` (outputs). A field with a `count`
is a one-dimensional variable with a literal `<Dimension start="8"/>`, not a
structural parameter. Element `i` is element `i` of the Schema field.
The names of the object fields follow some ASAM OSI detected-object terms
(ID, position, relative velocity, existence probability). The profile is not
OSI-compatible and needs no Protobuf.

| Schema type | FMI 3 type | Variables |
| --- | --- | --- |
| u64 | `UInt64` | `*.sample_time_ns` |
| u32 | `UInt32` | `sensor_id`, `frame_id`, `sequence`, `count`, `command.mode`, `command.ignored_observations` |
| u8 | `UInt8` | `*.validity` |
| i32 | `Int32` | `*.object_id` (8 elements), `command.selected_object_id` |
| i64 | `Int64` | `command.*_age_ns` |
| f32 | `Float32` | `x_m`, `y_m`, `relative_vx_mps`, `confidence` (8 elements each), `ego.speed_mps`, both accelerations |
| | `Float64` | the parameters `hazard_acceleration_mps2` (start −3) and `max_change_mps2` (start 0.5) |

Float variables carry the units `m`, `m/s` and `m/s2`; FMI 3 integer types
have no unit attribute. Inputs and outputs are `discrete`. Outputs are
`initial="exact"`: before the first step they read `sequence` 0, mode 2, no
selection, accelerations 0, ages −1 and no ignored observation. The
parameters are `fixed`: they can be set before initialization ends, never
after it. A value outside the [parameter ranges](#parameters) fails
`fmi3ExitInitializationMode`.

### Steps and deliveries

Each `fmi3DoStep` is one activation at its communication point t, over
[t, t + 10 ms]. The step is fixed: `canHandleVariableCommunicationStepSize`
is false and `fixedInternalStepSize` is 0.01 s. A step that is not 10 ms to
the nanosecond, or at another point than the FMU's next one, fails with
`fmi3Error`; it is never rescaled. A point matches when it is within 1 µs,
or within 4 units in the last place of a double where seconds are coarser
than that, near the start limit. The start time must be below
2^63 ns. The start time is rounded to the nearest
nanosecond, and the FMU counts Virtual time in integer nanoseconds from it.

FMI inputs hold their last value, and they carry no "new message" signal.
The FMU therefore takes a sensor's inputs as a new observation when its
header, `sample_time_ns` and `sequence`, differs from the header at the
previous step. An unchanged header delivers nothing, and the held
observation ages. A changed header whose sequence does not increase is
delivered, ignored and counted, as in the native example. A republished
observation with an identical header is indistinguishable from a held
input; the FMU cannot count it.

The start header, `sample_time_ns` 2^64 − 1 and `sequence` 2^32 − 1, means
"nothing received yet". No accepted observation can carry that Sample time,
because it is after every activation time. At most one observation per
sensor is delivered per step, radar, camera, then ego, before the advance.
When a Run delivers several Messages of one Channel in one Slot, as the
`delay` experiment does, the importer writes each in Publish order and the
FMU takes the newest. The native adapter receives each of them.

### Lifecycle and diagnostics

The instance is allocated by `fmi3InstantiateCoSimulation` and freed by
`fmi3FreeInstance`. Two instances in one process are independent. Another
instantiation token, `eventModeUsed`, or required intermediate variables
refuse the instance. The FMU reads no resource, so the resource path may be
anything, also NULL. Every refused call logs one message with category
`logStatusError` and returns `fmi3Error`. Malformed input fails the step
with the native diagnostic and the time:

```text
t=0 ns: radar.count 9 exceeds the capacity 8
t=0 ns: radar.x_m[0] is not finite: nan
```

After a failed initialization or step, only `fmi3Reset`, `fmi3FreeInstance`
and the getters are accepted. `fmi3Terminate` ends stepping; the outputs stay
readable. There is no Model Exchange, Scheduled Execution, Event Mode, Clock,
early return, intermediate update, FMU state, derivative or execution tool.
Each of those functions is exported, because FMI 3.0 requires every function,
and each returns `fmi3Error`.

## Native and FMU forms of one experiment

[proofs/adas-equivalence/](../proofs/adas-equivalence/README.md) runs the
maneuvers and the four experiments through both
forms with installed SiL: the Native participant, and `AdasReference.fmu`
through SiL's FMI importer. `reference_manifest` takes a `controller`
argument, so both Manifests come from one declaration and differ only in the
controller entry: the artifact, the Period as `period_ns` or
`step_period_ns`, the parameters as config keys or `--start`, and the
Channels as config keys or `--bind`.

Each form is compared with the expected trajectory, with an FMPy execution
of the archive, and with the other form. Every field is exact, the
accelerations with a tolerance of 0: both forms round once from the same
binary64 arithmetic. Five negative controls (a binding, the sign, a
parameter, the output Sample-time offset, the input Latency) must fail at a
first divergence predicted before the Run. In the `delay` experiment the FMU
takes only the newest of three radar lists at 130 ms (see
[Steps and deliveries](#steps-and-deliveries)). The proof declares that
difference and requires the same Commands from both forms.

```sh
proofs/adas-equivalence/run-proof.sh    # needs docker and network
```

## Process-isolated library form and execution cost

`process_adapter.py` runs the same library build as a Process participant.
It loads the library with `ctypes` in a child process of its own and calls
the application's C API directly. A crash or hang of the library ends that
process, not `sil-run`; `--participant-timeout-ms` bounds a hang.
`process_controller(library)` gives the controller entry, so its Manifest
comes from the same `reference_manifest` call as the Native and FMU forms.
It takes the Native config as one JSON argument, applies the same checks,
and publishes byte-identical Commands
([tests/test_adas_process_adapter.py](../tests/test_adas_process_adapter.py)).

[proofs/adas-cost/](../proofs/adas-cost/README.md) measures one declared
workload through the three forms on Linux x86-64: startup, steady-state
application computation, adaptation/routing and Recording cost, with
deterministic counters apart from observational timings. It is a reference
baseline for [#125](https://github.com/Stevie1704/sil/issues/125), not a
capacity claim, and it changes no framework contract.

```sh
proofs/adas-cost/run-proof.sh    # needs docker and network
```

## Closed loop over a plant FMU

[proofs/adas-closed-loop/](../proofs/adas-closed-loop/README.md) runs the
controller in a closed loop. The ACC plant FMU of
[proofs/acc-fmi/](../proofs/acc-fmi/README.md) moves the ego and a lead
vehicle. Edge Participants derive idealized processed observations from
plant truth: radar every 20 ms, camera every 40 ms and ego motion every
10 ms. An edge conversion applies the Command's acceleration to the plant.
The native and the FMU form come from one declaration and must give the
same Commands and the same truth. The proof states the whole consumption
table, so that no Participant consumes a value sampled after its
activation.

```sh
proofs/adas-closed-loop/run-proof.sh    # needs docker and network
```

## Scope

Supported acceptance platform: Linux x86-64. Processed objects only, at most
8 per sensor list, and one fixed 10 ms controller Period. The freshness and
sequence rules are reference policies, not production stale-data
requirements. There is no network transmission model and no change to the
kernel's Latency semantics. There is no raw image or
radar data, object tracking, sensor-fusion accuracy claim, CAN or Ethernet
decoder, sensor rendering, OSI dependency, production controller, Native ABI
change, variable-length Schema or dynamic Channel creation. The FMU is a
reference export of this profile for FMI 3.0 Co-Simulation on Linux x86-64
only: no FMI 2.0, Model Exchange, Scheduled Execution, dynamic dimension or
general FMI conformance claim.
