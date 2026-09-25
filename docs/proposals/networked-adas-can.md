# Proposal: networked ADAS regression on the first-party CAN bus

Status: proposal (issue #158). Nothing here is implemented, and no child
ticket is published from it. A maintainer decides whether to start the work
and how to cut it.

## Baseline this proposal uses

| Baseline | What it fixes |
| --- | --- |
| ACC closed loop, [HANDOFF.md](../../proofs/acc-fmi/HANDOFF.md) (#148–#151) | Signals, rates, Latencies, KPIs, numerical budgets and the acceptance envelope. The measured values are in its bundle's `curated/report.json`. |
| `SilCanBus` 1.0.0, [RELEASE.md](../../models/can/RELEASE.md) (#152–#158) | Classical CAN timing, arbitration, the fault schedule, replay equivalence, packaged limits and the example configuration path |

The ACC FMUs and the bus FMU stay unchanged. The regression adds a signal
codec at the edge and puts the ACC `sensing` and `command` Channels onto the
bus. `truth` and `maneuver` stay off the bus, as HANDOFF.md requires, so a
bus fault stays observable.

## Candidate signal mapping

Three nodes on one bus, all at 100 Hz (every 10 ms, at the ACC Slot grid).
A lower identifier wins arbitration, so the actuation command gets the
highest priority.

| Frame | ID | Node | DLC | Signals (little-endian) |
| --- | --- | --- | --- | --- |
| `AccCommand` | `0x0A0` | Node3, controller | 3 | `accel_mps2`: int16, 0.001 m/s² per bit; byte 2: alive counter (bits 0–3), status (bits 4–7) |
| `RadarObject` | `0x120` | Node1, sensor side of the plant | 5 | `gap_m`: uint16, 0.01 m per bit; `relative_speed_mps`: int16, 0.01 m/s per bit; byte 4: counter and status |
| `EgoSpeed` | `0x130` | Node2, vehicle side of the plant | 3 | `ego_speed_mps`: uint16, 0.01 m/s per bit; byte 2: counter and status |

The alive counter increments by one for each frame of that ID, modulo 16. The
status bits flag saturation (bit 4) and a stale source value (bit 5).

## Quantization and scaling

Encoding rounds to the nearest step, with ties away from zero, and saturates at
the range limits. Saturation sets the status bit. Decoding multiplies by the
step. The codec uses integer arithmetic on the rounded value, so the same
input gives the same bytes on every supported machine.

| Signal | Step | Range | Maximum rounding error | Acceptance envelope (HANDOFF.md) |
| --- | --- | --- | --- | --- |
| `gap_m` | 0.01 m | 0 to 655.35 m | 0.005 m | 1.0 m |
| `relative_speed_mps` | 0.01 m/s | −327.68 to 327.67 m/s | 0.005 m/s | 0.5 m/s |
| `ego_speed_mps` | 0.01 m/s | 0 to 655.35 m/s | 0.005 m/s | 0.5 m/s |
| `accel_mps2` | 0.001 m/s² | −32.768 to 32.767 m/s² | 0.0005 m/s² | 0.5 m/s² |

Each rounding error is at least 100 times smaller than its envelope entry.
The controller clamp (−3 to +1.5 m/s²) is far inside its range. The scenario
speeds (at most about 25 m/s) are too. Saturation is thus a fault-injection
case, not a nominal one.

## Bus load and delay budget

The frame-length formula of the [timing model](../../models/can/README.md#timing-model)
gives `44 + 8n` bits before stuffing. At most `floor((33 + 8n) / 4)` stuff
bits are possible, and each frame is followed by 3 bits of intermission.

| Frame | DLC | Bits, no stuffing | Worst-case bits with intermission |
| --- | --- | --- | --- |
| `RadarObject` | 5 | 84 | 105 |
| `EgoSpeed` | 3 | 68 | 85 |
| `AccCommand` | 3 | 68 | 85 |
| Total per 10 ms | | 220 | 275 |

| Bitrate | Worst-case bus load | Worst-case added delay of one frame |
| --- | --- | --- |
| 500 kbit/s | 5.5 % | 0.55 ms |
| 250 kbit/s | 11 % | 1.1 ms |
| 125 kbit/s | 22 % | 2.2 ms |

The added delay bound assumes that all three frames are requested at one
instant, and that the frame under study waits for the other two. With one
request per ID per period and a load far below 100 %, no queue grows, so the
default `perNodeQueueCapacity` of 4 is enough. 500 kbit/s is the proposed
nominal rate. 125 kbit/s is the stress row.

## Stale-data policy

The receiving codec keeps the last decoded value and its FMI event time.

| Age of the last valid frame | Controller input | Flag |
| --- | --- | --- |
| At most 20 ms (two periods) | Last value | none |
| More than 20 ms | Last value; the controller output is limited to at most 0 m/s² | stale bit and the existing `freshness.age_ns` |
| More than 100 ms | Last value; the controller output is −3 m/s² (the clamp minimum) | stale bit |

A wrong alive counter increment counts as a missed frame. The policy is part
of the edge codec, not of the bus FMU or the ACC FMUs, so each piece keeps its
qualification. The thresholds are hypotheses for the milestone to confirm or
change before the first Run.

## Measurable KPI hypotheses

Each hypothesis names its measure and its pass criterion. The HANDOFF.md KPIs
(minimum gap, command range, speeds, spacing and relative-speed error) apply
to every row, judged on `truth`.

| ID | Row | Measure | Hypothesis |
| --- | --- | --- | --- |
| H1 | Codec only, no bus, 10 ms Latency | Per-signal difference from the baseline Run | At most one rounding step, and the trajectory stays inside the acceptance envelope |
| H2 | Nominal, 500 kbit/s | Frame end minus request time, from the Recording | At most 0.55 ms for each frame |
| H3 | Nominal, 500 kbit/s | Bus load from recorded frame ends and frame lengths | At most 5.5 % in every 10 ms window |
| H4 | Nominal, 125 kbit/s | Trajectory against the 10 ms comparison reference | Inside the acceptance envelope |
| H5 | One scheduled Bit Error on the first `RadarObject` after 5 s, one retry | `freshness.age_ns` and KPIs | Maximum age at most 10 ms plus two frame times; KPIs pass |
| H6 | `ReceiverDeliverySuppression` of three consecutive `RadarObject` frames at the controller | Stale flag and KPIs | Stale flag set for the suppressed interval; KPIs pass |
| H7 | Negative control: suppression of `RadarObject` for 250 ms | Trajectory against the 10 ms comparison reference | Outside the envelope, like the ACC 250 ms timing-defect row |
| H8 | Every row | Author the Manifest twice and Run it twice | Identical Manifest bytes and identical Recording bytes |
| H9 | H2 and H5 | Replay the bus terminals from the Recording | Retained outputs equal the live Run, as in issue #156 |

## Integration shape

- The bus FMU runs in a SiL FMU group, as in the
  [example](../../models/can/example/README.md). The codec nodes publish
  operation buffers to its terminals and decode its Tx operations.
- The ACC FMUs stay Process participants with their fixed 10 ms period. The
  codec is the only new code at their boundary.
- No kernel, transport, Manifest, Step protocol, Arena or Native ABI change is
  expected. If one is needed, a concrete fixture must justify it first.
- The existing ACC evidence stays as it is. The bus rows get new Manifest
  identities and new evidence directories.

## Open questions

- Is a Python codec Participant enough, or does the regression need codec
  FMUs so that an independent FMPy path can run the full loop?
- Must the proposed stale policy agree with a specific ADAS function
  specification, or is it only a test fixture?
- Is one CAN bus enough, or does a later step need a gateway between two
  buses? That needs a second bus FMU instance and more than four terminals.

## Not in scope

A DBC or ARXML toolchain, CAN FD, a second bus type, physical-layer effects,
sensor or perception fidelity, and any safety claim. The KPIs are integration
thresholds, not ADAS requirements.
