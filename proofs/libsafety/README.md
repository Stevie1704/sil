# Public shared-library acceptance: opendbc `libsafety` (#193, #232)

```sh
proofs/public-workloads/run-proof.sh                 # once: the #178 bundle; needs docker and network
proofs/libsafety/run-proof.sh build/public-workloads/bundle [output-directory]
```

This is use case 1 of the adoption acceptance: recorded data into a shared
library. It takes the public library and CAN recording that
[`../public-workloads`](../public-workloads/README.md) (#178) qualified,
replays the recording into the library through the supported SiL workflow,
and compares every observation with the independent reference.

No company artifact, vehicle or supplier is needed. The evidence is
**public-artifact adoption acceptance**. It is not production-vehicle
validation, and it says nothing about whether the safety logic is correct.

## What runs where

| Step | Where | Network | Output |
| --- | --- | --- | --- |
| Bundle (#178) | public-workload tool image | image build only | `libsafety.so`, `rlog.zst`, `libsafety-states.json`, digests |
| Frame export | the same tool image | none | `prepared/frames.csv`, `frames.json`, `packet-layout.json` |
| Transmit export (#232) | the same tool image, a process of its own | none | `prepared/transmit.csv`, `native-reference.json`, `transmit.json` |
| Acceptance | example image: production runtime + `libubsan1` + this directory + the built Native adapter | none | `inputs/`, `runs/`, `evidence/report.json` |
| Native acceptance (#232) | the same example image | none | `bundles/`, `matrix/`, `evidence/native-report.json` |

Only the tool image has opendbc's log reader, pycapnp and CFFI. The example
image has none of them. SiL comes from the installed wheel and the installed
`sil-run`. The library is the pinned file from the bundle. The acceptance
re-hashes the library, the recording and the reference. It compares each
digest with `bundle.json` and with the committed #178 handoff
([`../public-workloads/evidence/handoff.json`](../public-workloads/evidence/handoff.json))
and stops at the first difference.

## The workflow

1. **Export** (`prepare_frames.py`). Every received frame (`src` < 128) of
   every `can` event, in recorded order, with its recorded `logMonoTime`,
   becomes one CSV row: `address`, `src`, `length`, `d0`…`d7`. The same step
   builds each frame's `CANPacket_t` with this directory's binding and with
   upstream's CFFI `make_CANPacket`, and requires identical bytes for all of
   them.
2. **Convert** (`sil-csv`). The mapping in `workload.py` turns the CSV into
   the `can.rx` Channel, schema `can.Frame`. The Message time is the recorded
   `logMonoTime` in ns. Frames of one event share that time, so they are one
   Burst, in Publish order.
3. **Window** (`sil-window`). The window rebases the Recording to the first
   event and selects all of it, with `max_gap_ns` 12 ms: a lost event fails
   preparation. The warm-up is empty (see below).
4. **Run** (`sil-run`). A Replay participant publishes `can.rx`. One Process
   participant runs `adapter.py`. It loads `libsafety.so` through
   `binding.py` and publishes one observation per Burst on `libsafety.state`.
   The observation also carries `config_valid`, the result of
   `safety_config_valid()`. The reference does not record it, so the contract
   ignores it. As upstream does, the acceptance requires it to be 1 at every
   nominal event that ran `safety_tick`.
5. **Compare** (`sil-compare`). The reference becomes a second Recording
   through `sil-csv`. The contract compares every field it records, at every
   event, exactly.

## Run contract

| Item | Value |
| --- | --- |
| Initial state | `set_safety_hooks(2, 73)` must return 0, then `set_alternative_experience(0)`: the recorded `carParams`. Nothing else is seeded. |
| Calibration | mode 2 (`toyota`), param 73, alternative experience 0, from the recording, checked against the handoff |
| Time | Virtual time = `logMonoTime − 18054876797669` ns, not requantized |
| Library timer | `((18054876797669 + event_ns) // 1000) % 0xFFFFFFFF`, set from the Burst's own instant |
| Step period | 1 ms: shorter than the shortest event interval (8.75 ms), so a Step holds at most one Burst |
| `can.rx` Latency | 0: a Burst is processed in the first Step at or after its instant |
| `libsafety.state` Latency | 0; recorded only |
| `can.rx` route capacity | 47, the largest recorded Burst; a Burst waits less than one Step |
| Duration | the observation Slot of the last event + 1 Step |
| Warm-up | window warm-up empty; `safety_tick` only when more than 1 s from the first and the last event, as upstream |

**Why the observation is in the next Step.** A Process participant is
stepped on its Period, and the recorded events are not on any grid. The
adapter therefore processes a Burst in the first Step at or after its
instant, and gives the library the Burst's own time through `set_timer`. The
library reads no other clock, so what it computes is what it would compute
at the Burst's instant. The observation is published in that Step's Slot and
names the Burst in `event_ns`. The reference rows are published in the same
Slot, and `event_ns` is compared exactly, so an observation of the wrong
event cannot pass.

**Why the warm-up is empty.** The reference starts from
`set_safety_hooks` at the first recorded event, and so does the Run. A later
window start would need a reference that starts there too. The library's own
warm-up, no `safety_tick` in the first and last second, runs inside the
evaluation and is compared.

## Controls

Each control changes one thing in the nominal Manifest. Each must fail, and
fail for its reason:

| Control | Change | Required failure |
| --- | --- | --- |
| `input-one-step-late` | `can.rx` Latency 1 ms | `sil-compare`: no observation at any of the 6000 Slots (missing-actual from Slot 0) |
| `timer-in-ns` | timer unit 1 ns instead of 1 µs | `sil-compare`: `controls_allowed` diverges at event 100, where #178 found it |
| `wrong-param` | param 73 + 256: opendbc's alternative-brake flag | `sil-compare`: a state value diverges; the observation names the correct event |
| `crash` | the library process gets SIGSEGV at event 100 | Run failure (exit 1): `'libsafety' exited unexpectedly` |
| `hang` | the library call at event 100 never returns | Run failure (exit 1): the 5 s response deadline at event 100's Slot |

`library_failures.py` injects the crash and the hang around the bound
library. The library's code is unchanged.

## Determinism and resources

The nominal Manifest runs twice, and the two Recordings must be
byte-identical. `report.json` → `resources` keeps wall-clock time, events and
frames per second and the largest child resident set. These numbers are
observational, from one runner. They are not acceptance criteria. The
library is small decision logic behind a Python adapter, so they are not a
representative vECU Step cost for #125.

## Native participant and transmit (#232)

`native_acceptance.py` runs after `acceptance.py` in the same example image.
It runs the same library and recording as a **Native participant**, in the
runner's own process, and adds **transmit** coverage. The Process form above
is unchanged.

**Adapter.** `native_adapter.c` exports `sil_participant_init`. It loads the
pinned `libsafety.so` with `dlopen` from its config and resolves every symbol
before the Run starts. It applies the same policy as `adapter.py`: one Burst
per activation, the timer from the Burst's instant, `safety_tick` only when
warm, then `safety_fwd_hook` and `safety_rx_hook` per frame. The example
image builds it in the SiL build stage, against the installed
`sil/participant.h` and the layouts that `silschema` generates from
`workload.py`. The example image itself has no compiler.

**Event time.** The Native ABI's `take` returns a payload without its
Message time. Each frame therefore carries its recorded time in a field,
`event_ns` (`can.TimedFrame`). `sil-window` rebases that field together with
the log time (`source_time_fields`), so `event_ns` is the Burst's Virtual
instant. No ABI change is necessary.

**Transmit candidates.** The segment has no `sendcan`. The candidates are
therefore derived from recorded frames, not generated. On this Toyota the
stock camera sends `STEERING_LTA` (0x191), `STEERING_LKA` (0x2E4),
`ACC_CONTROL` (0x343) and `LKAS_HUD` (0x412) on bus 2. openpilot sends these
messages in their place on bus 0. `prepare_transmit.py` makes each recorded
camera frame of these addresses one candidate on bus 0, with its recorded
payload. The candidates of one `can` event are one upstream `sendcan` event
at the same instant, after the `can` event: `set_timer`, `safety_tick` when
warm, then `safety_tx_hook` per candidate. These candidates are recorded
stimulus, not openpilot output: the recorded `STEERING_LKA` torque is always
0, so the torque rate limits are not exercised.

**Reference.** `prepare_transmit.py` runs in the tool image. It drives the
pinned library through upstream's CFFI declarations, event by event, and
records the state and the transmit verdicts per event. Two cross-checks stop
the export when they fail:

- upstream `replay_drive` replays the same `can` and `sendcan` events and
  must count the same received, invalid, transmitted and blocked frames;
- the receive-side state of every event must equal the #178 reference, so
  the candidates do not change what #193 compares.

**Run contract.** As for the Process form, with these differences:

| Item | Value |
| --- | --- |
| Participant | Native, `libsafety_native.so`; Task period 1 ms |
| Inputs | `can.rx` (frames) and `can.tx` (candidates), both `can.TimedFrame`, from two Replay participants, Latency 0 |
| Route capacity | `can.rx` 47, `can.tx` the largest candidate set of one event |
| `can.tx` window | from the first candidate to the last event, origin the first event, `max_gap_ns` 100 ms |
| Observation | `libsafety.NativeState`: the Process fields plus `tx_accepted` and `tx_rejected` |
| Comparison | every recorded field exact at every event; `config_valid` checked separately |
| Instances | one per Run (C globals); a second Participant of the adapter is a Manifest error |
| Termination | none: Native ABI v1 has no termination callback and the library has no shutdown call. The library ends with the runner process |
| Threads and blocking | none: the library starts no thread, reads no clock and blocks on nothing |

**Controls.** Each must fail, and fail for its reason:

| Control | Change | Required failure |
| --- | --- | --- |
| `timer-in-ns` | timer unit 1 ns instead of 1 µs | `sil-compare`: `controls_allowed` diverges at event 100, where #178 found it |
| `stock-longitudinal` | param 73 + opendbc's stock-longitudinal flag (0x200) | `sil-compare`: the first divergence is a transmit verdict; `ACC_CONTROL` is refused |
| `second-instance` | a second Participant of the same adapter | Manifest error (exit 2): `one Run holds at most one instance` |
| `crash` | the adapter raises SIGSEGV at event 100 | sealed case `behavioral-failure`: `sil-run` ends with signal 11 |
| `hang` | the adapter never returns at event 100 | sealed case `timeout`: the 30 s whole-case guard |

**Sealed regression.** The nominal Run is also a sealed offline regression
bundle (`bundles/native-nominal`): the adapter, the library, both windowed
Recordings, the reference and the contract, with the loader dependencies of
both libraries and no compiler. `sil-matrix` runs it under a 600 s
whole-case guard, twice for Recording identity, with the comparison. The
crash and hang controls are sealed bundles of their own. They are simulated
failures: the adapter crashes or hangs just before the library call, and the
library is unchanged. Each failure build
writes `$TMPDIR/libsafety-failure-event` first, so the control shows that
the Run reached event 100. A hung Run is killed before its log is kept.

**What the Native form cannot do.** These are incompatibilities of an
in-process library, recorded and not worked around:

- A crash in the library ends the runner. There is no participant
  diagnostic, and the case evidence keeps a core dump and an empty
  Recording.
- A hang in the library stops the Run. No Process response deadline
  applies, because no Process exists. Only a whole-case guard outside the
  runner ends it.
- One Run holds at most one instance.

**Cost.** `native-report.json` → `resources` keeps the wall-clock time of
whole Native and Process Runs on one runner. It does not measure the library
calls alone. These numbers are observational and are not the #125 vECU
measurement.

The Process form keeps each of these inside one Process participant. It
stays the isolated alternative.

## Retained results

[`evidence/`](evidence/) is the output of CI run 36232866332 on native
Linux x86-64 (glibc 2.36, CPython 3.13.7, `libubsan1` 12.2.0-14+deb12u1). It
holds `report.json`, the three conversion receipts, the comparison reports,
and the export's `frames.json`, `packet-layout.json` and `tool-image.json`.
The Recordings are not committed, because they hold the recorded data. The
job keeps the Manifests and runner output as the `libsafety-evidence`
artifact. A run of the same job under amd64 emulation on an arm64 host gave
the same Manifest hash and the same Recording bytes.

| Check | Result |
| --- | --- |
| Pins | library, recording and reference match the #178 handoff |
| Packet layout | 149,228 frames: binding and upstream CFFI bytes identical |
| Conversion | 149,228 frames on `can.rx`; window 0 to 59,990,294,747 ns, largest gap 11.03 ms, empty warm-up |
| Nominal | 6000 of 6000 observations equal, every compared field, final event included |
| `config_valid` | 1 at all 5800 ticked events; 0 only at event 0, before the first tick |
| Determinism | two Runs of Manifest `35b94095…`, byte-identical Recordings (`40c490f0…`) |
| `input-one-step-late` | 6000 missing-actual divergences, the first at Slot 0 |
| `timer-in-ns` | first divergence at Slot 1.001 s (event 100): `controls_allowed` 0, expected 1 |
| `wrong-param` | first divergence at Slot 1.001 s (event 100, the first tick): `controls_allowed` 0, expected 1 |
| `crash` | exit 1: `participant 'libsafety' exited unexpectedly` |
| `hang` | exit 1: timeout waiting for `step_done` at virtual time 1,001,000,000 ns |

The runtime identity (glibc, CPython, `sil-run --build-info`, `libubsan1`
version, resolved `ldd`, image ID) and the export tool image ID are in
`report.json` → `identities`.

Resource observations from that runner: a nominal Run took 5.6 to 5.8 s of
wall-clock time for 59.99 s of Virtual time. That is about 1060 events and
26,400 frames per second. The largest child resident set was 104 MiB.

## Limits

- **Recorded-derived transmit only.** The segment has no `sendcan`. The
  Native form covers the transmit hook with recorded camera frames
  readdressed to bus 0. Their steering torque is always 0, so no torque
  limit is exercised. The Process form is receive side only.
- **One instance per process.** The library keeps its state in C globals.
  One Process participant, or one Run with the Native form, is one library
  instance.
- **Runtime dependency.** The upstream build links `libubsan.so.1`. The
  example image installs Debian's `libubsan1`; `report.json` names its
  version and the resolved `ldd` of the library.
- **Linux x86-64 only.** No other platform is claimed.
- **Bundle expiry.** The CI bundle artifact expires. The workflow regenerates
  the bundle with `proofs/public-workloads/run-proof.sh`; the acceptance
  requires the regenerated library, recording and reference to have the
  pinned digests.
