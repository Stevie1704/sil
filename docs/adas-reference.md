# Native ADAS reference application

[Documentation index](README.md) · [Repository overview](../README.md)

Commands below run from the repository root unless stated otherwise.

## What it is

[examples/adas-reference/](../examples/adas-reference/) is a small C
application that consumes bounded lists of processed radar and camera objects
and commands a longitudinal acceleration. It runs through the
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
| `schemas.json` | the SiL Schemas; `silschema` generates `adas_messages.h` from them |
| `maneuvers/<name>.csv` | the authored radar and camera object lists and ego speed of each maneuver |
| `maneuvers/<name>.expected.csv` | the expected trajectory of each maneuver, enumerated by hand |
| `mapping.json`, `expected-mapping.json`, `contract.json` | the `sil-csv` mappings and the `sil-compare` contract of one maneuver, without a Channel prefix |
| `prepare.py` | expands every maneuver to the flat Schema form, converts it with its Channel prefix and writes its contract |
| `manifest.py` | the Run: one Replay participant and one Native participant per maneuver, one library |
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
   `-DADAS_REFERENCE_WRONG_SIGN`, against the installed `include/sil`;
3. expands and converts the maneuvers with `prepare.py`;
4. runs the Manifest twice and requires identical Recording bytes (`cmp`);
5. compares each maneuver's Commands with its expected trajectory;
6. runs the wrong-sign build and requires the `hazard` and `ordering`
   comparisons to fail (exit 1). This is the control that shows the comparison detects a
   coordinate sign error.

## Reference profile

The profile is `sil.adas-reference.radar-camera`, version 2. A change to a
field, a unit, a constant, the capacity or the time convention below makes a
new version. Each Native config names the profile and its version, and the
library refuses any other. Version 1 carried one object per sensor; version
2 carries bounded lists and replaces it.

### Frame and units

All detections are in one ego frame, `frame_id` 1: x forward, y left, in
metres. `relative_vx_mps` is the object's longitudinal speed relative to the
ego, in m/s; it is negative when the object comes closer. Speeds are in m/s,
accelerations in m/s², times in nanoseconds of Virtual time. A list in any
other frame is rejected; the profile never transforms coordinates.

### Inputs

Each maneuver has three input Channels, sampled every 10 ms: `radar` and
`camera` carry an `adas.ObjectList`, `ego` carries an `adas.EgoMotion`.

An object list is one sensor's complete set of processed objects at one
Sample time. Its objects are flat arrays of capacity 8 and an active count:
elements `[0, count)` are the active objects, in any order. Each list
replaces the previous one; the controller keeps no object between
activations, so an object that is not listed has disappeared. The capacity
of 8 is an example limit for test coverage, not a sensor or hardware
recommendation.

| Schema | Field | Type | Meaning and validity |
| --- | --- | --- | --- |
| `adas.ObjectList` | `sample_time_ns` | u64 | the time the list describes; must equal the activation time |
| | `sensor_id` | u32 | `1` on the radar Channel, `2` on the camera Channel; any other value is rejected |
| | `frame_id` | u32 | `1`, the ego frame; any other value is rejected |
| | `sequence` | u32 | the sensor's message counter; carried, not interpreted |
| | `count` | u32 | active objects, `0` to `8`; a larger count is rejected, never truncated |
| | `validity` | u8 | `1` valid, `0` the sensor has no usable list; any other value is rejected |
| | `object_id` | i32[8] | active IDs `>= 0` and unique within the list; `-1` is reserved for "no selection" |
| | `x_m`, `y_m` | f32[8] | object position; finite |
| | `relative_vx_mps` | f32[8] | relative longitudinal speed; finite. The camera reports no speed: `0` |
| | `confidence` | f32[8] | detection confidence; finite, in `[0, 1]` |
| `adas.EgoMotion` | `sample_time_ns`, `sequence` | u64, u32 | as in the list |
| | `validity` | u8 | as in the list |
| | `speed_mps` | f32 | ego speed; finite and `>= 0`. Required sensing; profile 2 does not use its value |

Radar and camera object IDs are independent: the same number on both
Channels names unrelated objects. Every inactive element is zero, all bits,
in every array. The layout is packed (`silschema`), so there is no padding
byte to clear. A publisher zeroes the inactive elements before it publishes;
the adapter rejects a list that does not.

The adapter checks `count` against the capacity before it reads an element,
then checks that every inactive element is zero. The application then checks
every header field and every active element, also when `validity` is 0. A failed check fails the Run (exit 1). The diagnostic names
the Participant, the activation time, the sensor, the field and, for an
array, the element:

```text
participant 'live' failed: t=20000000 ns: radar.x_m[0] is not finite: nan
participant 'live' failed: t=0 ns: radar.count 9 exceeds the capacity 8
participant 'live' failed: t=10000000 ns: radar.object_id[1] 7 repeats radar.object_id[0]
```

### Output

| Schema | Field | Type | Meaning |
| --- | --- | --- | --- |
| `adas.Command` | `sample_time_ns` | u64 | `t + 10 ms`, the end of the advanced interval |
| | `sequence` | u32 | activations completed, from 1 |
| | `mode` | u32 | `0` CLEAR, `1` HAZARD, `2` SENSOR_UNAVAILABLE |
| | `selected_object_id` | i32 | the selected radar object's ID, or `-1` for none |
| | `target_acceleration_mps2` | f32 | the acceleration the mode asks for |
| | `acceleration_mps2` | f32 | the commanded, rate-limited acceleration |

### Behavior

At each activation t:

1. **Availability.** When a radar, camera or ego Message for t is absent, or
   has `validity` 0, the mode is SENSOR_UNAVAILABLE and no object is
   selected. A valid list with `count` 0 is available: it reports that the
   sensor sees nothing.
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

With at most one object per sensor, steps 1 to 7 give the results of
profile 1: the five profile 1 maneuvers keep their expected trajectories.

### Parameters

| Config key | Type | Range | Reference value |
| --- | --- | --- | --- |
| `profile` | string | `sil.adas-reference.radar-camera` | |
| `profile_version` | integer | `2` | |
| `radar`, `camera`, `ego`, `command` | string | declared Channels | `<maneuver>.radar`, … |
| `period_ns` | integer | `10000000` only | `10000000` |
| `hazard_acceleration_mps2` | number | finite, `[-10, 0)` | `-3` |
| `max_change_mps2` | number | finite, `(0, 10]` | `0.5` |

The config is a flat JSON object. A missing, unknown or repeated key, a
value of the wrong kind, a value outside its range, or another Period fails
initialization before the first Step, as a Manifest error (exit 2). A
different Period is rejected, never rescaled.

### Time

Inputs sampled at t are consumed by the activation at t. Input Channels have
`latency_ns` 0, and a Replay participant without a priority publishes before
every activation of the Slot. The activation advances the state over
[t, t + 10 ms] and publishes, in Slot t, a Command with `sample_time_ns`
t + 10 ms. The Recording stores the publication Slot t. This is the
convention of the [FMU importer](fmi.md): the comparison contract uses an
`actual_offset_ns` of one Period.

### Arithmetic and tolerance

Each f32 input is converted once to binary64. All reference arithmetic is
binary64. The two output accelerations are rounded to f32 once, when the
Command is written. The comparison contract is exact for every field: integer
fields are `"exact"` and both accelerations are `{"atol": 0, "rtol": 0}`.
This holds because the reference values are multiples of 0.5, which binary32
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
contracts floating-point expressions by default, also in ISO C mode. Profile 2
has no expression that a contraction could fuse; the flag keeps that true when
the arithmetic changes.

### Lifecycle and ownership

The application has `adas_ref_init`, `adas_ref_advance`, `adas_ref_reset`
and `adas_ref_terminate`. The caller owns each `adas_ref_instance`. The
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
trajectories are written row by row from the behavior above. The oracle is
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
`sil-csv` then converts the expanded CSV under `mapping.json`. Its receipt
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

The first five are the profile 1 maneuvers, with each object as a list of
one and each "no object" as an empty list. Their expected trajectories are
unchanged.

The wrong-sign build computes closing speed from `+relative_vx`. The hazard
comparison then fails at the first observation: mode 0 where 1 is expected.

The tests in
[tests/test_example_adas_reference.py](../tests/test_example_adas_reference.py)
also run two instances of the library with different lists, and require
each to publish what it publishes beside the others. A live stimulus
Participant delivers what `prepare.py` and `sil-csv` refuse to write:
nonfinite values, a count above the capacity, nonzero inactive elements,
negative and repeated IDs, invalid confidence, a wrong frame or sensor ID,
impossible Sample times, and a second list in one Slot, which overflows the
finite Subscriber route of capacity 1.

## Shapes for a future FMU

A later FMU that implements this profile exchanges the same values as FMI 3
variables. Each list field becomes one variable; the sizes are literal
`Dimension start` values, not structural parameters. Variable names below
use the `radar.` prefix; the camera uses `camera.` with the same shapes.

| Variable | FMI 3 type | Shape |
| --- | --- | --- |
| `radar.sample_time_ns` | `UInt64` | scalar |
| `radar.sensor_id`, `radar.frame_id`, `radar.sequence`, `radar.count` | `UInt32` | scalar |
| `radar.validity` | `UInt8` | scalar |
| `radar.object_id` | `Int32` | one dimension, `<Dimension start="8"/>` |
| `radar.x_m`, `radar.y_m`, `radar.relative_vx_mps`, `radar.confidence` | `Float32` | one dimension, `<Dimension start="8"/>` |
| `ego.sample_time_ns` | `UInt64` | scalar |
| `ego.sequence` | `UInt32` | scalar |
| `ego.validity` | `UInt8` | scalar |
| `ego.speed_mps` | `Float32` | scalar |
| `command.*` | as `adas.Command` | scalars |

Element `i` of each array is element `i` of the Schema field. The rules
above on count, inactive elements and IDs apply unchanged. The names of the
object fields follow some ASAM OSI detected-object terms (ID, position,
relative velocity, existence probability). The profile is not OSI-compatible and
needs no Protobuf.

## Scope

Supported acceptance platform: Linux x86-64. Processed objects only, at most
8 per sensor list, and one fixed 10 ms Period. There is no raw image or
radar data, object tracking, sensor-fusion accuracy claim, CAN or Ethernet
decoder, sensor rendering, OSI dependency, production controller, Native ABI
change, variable-length Schema, dynamic Channel creation or FMU. An FMI
equivalent is later work against this profile.
