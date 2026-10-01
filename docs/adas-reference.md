# Native ADAS reference application

[Documentation index](README.md) · [Repository overview](../README.md)

Commands below run from the repository root unless stated otherwise.

## What it is

[examples/adas-reference/](../examples/adas-reference/) is a small C
application that consumes processed radar and camera objects and commands a
longitudinal acceleration. It runs through the
[Native participant ABI](../include/sil/participant.h), and its output is
compared with hand-enumerated expected trajectories. It is intentionally
simplified example coverage for the path C application → Native participant
→ Recording → independent comparison. Its constants define test behavior, not
vehicle requirements. It makes no vendor, perception-accuracy or safety
claim. [The scoped decision](decisions/222-native-adas-reference.md) records
why it exists.

| File | Role |
| --- | --- |
| `adas_reference.h`, `adas_reference.c` | the application: its own API, caller-owned instances, no SiL include |
| `sil_adapter.c` | the Native participant: exports `sil_participant_init`, converts Messages to the application's types |
| `schemas.json` | the SiL Schemas; `silschema` generates `adas_messages.h` from them |
| `maneuvers/<name>.csv` | the recorded radar, camera and ego-speed input of each maneuver |
| `maneuvers/<name>.expected.csv` | the expected output of each maneuver, enumerated by hand |
| `mapping.json`, `expected-mapping.json`, `contract.json` | the `sil-csv` mappings and the `sil-compare` contract of one maneuver, without a Channel prefix |
| `prepare.py` | converts every maneuver with its Channel prefix and writes its contract |
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
3. converts the maneuvers with `prepare.py`;
4. runs the Manifest twice and requires identical Recording bytes (`cmp`);
5. compares each maneuver's Commands with its expected trajectory;
6. runs the wrong-sign build and requires the hazard comparison to fail
   (exit 1).

## Reference profile

The profile is `sil.adas-reference.radar-camera`, version 1. A change to a
field, a unit, a constant or the time convention below makes a new version.
Each Native config names the profile and its version, and the library refuses
any other.

### Frame and units

All detections are in one ego frame: x forward, y left, in metres.
`relative_vx_mps` is the object's longitudinal speed relative to the ego, in
m/s; it is negative when the object comes closer. Speeds are in m/s,
accelerations in m/s², times in nanoseconds of Virtual time.

### Inputs

Each maneuver has three input Channels, sampled every 10 ms.

| Schema | Field | Type | Meaning and validity |
| --- | --- | --- | --- |
| `adas.Radar` | `sample_time_ns` | u64 | the time the values describe; must equal the activation time |
| | `sequence` | u32 | the sensor's message counter; carried, not interpreted |
| | `sensor_id` | u32 | the sensor's identity; carried, not interpreted |
| | `object_id` | i32 | `-1` for no object, `>= 0` for one object; any other value is rejected |
| | `x_m`, `y_m` | f32 | object position; finite |
| | `relative_vx_mps` | f32 | relative longitudinal speed; finite |
| | `confidence` | f32 | detection confidence; finite |
| `adas.Camera` | `sample_time_ns`, `sequence`, `sensor_id`, `object_id`, `x_m`, `y_m`, `confidence` | as radar | as radar; the camera has no speed |
| `adas.EgoSpeed` | `sample_time_ns`, `sequence` | u64, u32 | as radar |
| | `speed_mps` | f32 | ego speed; finite and `>= 0`. Required sensing; profile 1 does not use its value |

Every f32 field must be finite, also when `object_id` is `-1`. A nonfinite
value, an object ID below `-1`, a negative ego speed, or a Sample time other
than the activation time fails the Run (exit 1). The diagnostic names the
Participant, the activation time, the field and the value:

```text
participant 'live' failed: t=20000000 ns: radar.x_m is not finite: nan
```

### Output

| Schema | Field | Type | Meaning |
| --- | --- | --- | --- |
| `adas.Command` | `sample_time_ns` | u64 | `t + 10 ms`, the end of the advanced interval |
| | `sequence` | u32 | activations completed, from 1 |
| | `mode` | u32 | `0` CLEAR, `1` HAZARD, `2` SENSOR_UNAVAILABLE |
| | `selected_object_id` | i32 | the confirmed radar object's ID, or `-1` for none |
| | `target_acceleration_mps2` | f32 | the acceleration the mode asks for |
| | `acceleration_mps2` | f32 | the commanded, rate-limited acceleration |

### Behavior

At each activation t:

1. **Availability.** When a radar, camera or ego-speed Message for t is
   absent, the mode is SENSOR_UNAVAILABLE and no object is selected.
2. **Eligibility.** An object is eligible when it is present, `x > 0`,
   `|y| <= 1.5 m` and `confidence >= 0.5`.
3. **Confirmation.** The radar object is confirmed when it and the camera
   object are both eligible, `|x_camera − x_radar| <= 2 m` and
   `|y_camera − y_radar| <= 0.5 m`. Association is geometric: the two object
   IDs are never compared. A confirmed radar object is the selected object.
4. **Hazard.** For the confirmed object, closing speed is
   `max(0, −relative_vx)`. A hazard is `x < 8 m`, or closing speed `> 0` and
   `x / closing speed < 2 s`. Both comparisons are strict, so `x = 8 m` and a
   time to collision of exactly 2 s are not hazards. A zero closing speed is
   never a time-to-collision hazard. A hazard sets mode HAZARD; otherwise the
   mode is CLEAR.
5. **Target.** The target acceleration is `hazard_acceleration_mps2` for
   HAZARD and SENSOR_UNAVAILABLE, otherwise 0.
6. **Rate limit.** The commanded acceleration starts at 0 and moves toward
   the target by at most `max_change_mps2` per activation.

### Parameters

| Config key | Type | Range | Reference value |
| --- | --- | --- | --- |
| `profile` | string | `sil.adas-reference.radar-camera` | |
| `profile_version` | integer | `1` | |
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
and binary64 represent exactly. Each endpoint case in the maneuvers uses
values that binary32 represents exactly and that the threshold arithmetic
does not round; every other case is far from its threshold.

Build constraints: IEEE 754 binary64 `double`, round to nearest, and no
value-changing optimization. The source refuses `-ffast-math` at compile time.
The CMake targets use ISO C11 (`C_EXTENSIONS OFF`), so GCC does not contract
floating-point expressions.

### Lifecycle and ownership

The application has `adas_ref_init`, `adas_ref_advance`, `adas_ref_reset`
and `adas_ref_terminate`. The caller owns each `adas_ref_instance`. The
application keeps no global state, starts no thread, opens no file or
socket, reads no clock, and allocates no memory. All work happens inside
the registered Task, with the Virtual time the kernel passes.

The adapter allocates one controller per Manifest entry and passes it as the
Task's `user` pointer. One loaded library therefore backs all five
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

| Maneuver | What it shows |
| --- | --- |
| `clear` | a confirmed object 40 m ahead with zero closing speed: CLEAR, acceleration 0 |
| `hazard` | a confirmed object closing at 25 m/s: HAZARD, the rate limit from 0 to −3 m/s² in six activations |
| `release` | a hazard for eight activations, then an opening object: back to 0 at 0.5 m/s² per activation |
| `unavailable` | no ego speed for two activations and no camera for five: SENSOR_UNAVAILABLE, then CLEAR with recovery |
| `boundaries` | one case per row: `x = 8 m`, time to collision exactly 2 s, the association limits `2 m` and `0.5 m` and values just past them, `|y| = 1.5 m`, confidence `0.5`, `x = 0`, zero and opening closing speed, selected IDs `0` and `2147483647`, no radar object, no camera object, equal IDs far apart, and different IDs close together |

The wrong-sign build computes closing speed from `+relative_vx`. The hazard
comparison then fails at the first observation: mode 0 where 1 is expected.

The tests in
[tests/test_example_adas_reference.py](../tests/test_example_adas_reference.py)
also run two instances of the library with different inputs, and require
each to publish what it publishes beside the others. They deliver nonfinite
values and impossible Sample times through a live stimulus Participant,
because `sil-csv` refuses to convert them.

## Scope

Supported acceptance platform: Linux x86-64. One processed object per sensor
and one fixed 10 ms Period. There is no CAN or Ethernet decoder, no sensor
rendering, no OSI dependency, no production controller, no Native ABI change
and no FMU. Arrays and an FMI equivalent are later work against this profile.
