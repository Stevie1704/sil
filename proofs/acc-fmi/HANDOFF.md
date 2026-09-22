# Baseline handoff to a CAN-connected ADAS regression

The closed-loop FMI 3.0 baseline ends here. A later networked-ADAS milestone
puts a bus under the same two FMUs. This document states what that milestone
reuses unchanged, so the first CAN result can be compared against a measured
number rather than an opinion.

The machine-readable form is `handoff.json` in the prepared bundle. It is
generated from the same authored contract the Runs enforce, so it cannot drift
from them.

## Baseline signals

All fields are Float64 in SI units. Rates and Latencies are the scenario
values; the sensitivity study varies its own Periods and Latencies by row.

| Channel | Fields | Rate | Latency | Source |
| --- | --- | --- | --- | --- |
| `sensing` | `gap_m`, `relative_speed_mps`, `ego_speed_mps` | 100 Hz | 10 ms | `AccPlant` outputs the controller consumes |
| `command` | `accel_mps2` | 100 Hz | 10 ms | `AccController` output |
| `truth` | `gap_m`, `relative_speed_mps`, `ego_speed_mps`, `ego_position_m`, `lead_position_m`, `lead_speed_mps` | 100 Hz | 10 ms | `AccPlant` outputs no fault can hide |
| `maneuver` | `lead_accel_mps2` | 100 Hz | 0 ns | the authored lead-vehicle input |
| `freshness` | `age_ns` | 100 Hz | 10 ms | Test participant; time since the last sensing **delivery** |

`sensing` is the signal set a CAN regression moves onto the bus. `truth` and
`maneuver` deliberately stay off it: `truth` is the unaffected physical
observation the KPI is judged on when sensing is degraded, and `maneuver` is
the authored stimulus. Keeping both off the bus is what makes a bus fault
observable rather than invisible.

## Baseline KPIs

| KPI | Threshold | Definition |
| --- | --- | --- |
| minimum gap | ≥ 5 m | `truth.gap_m`, every recorded Message |
| command range | −3 to +1.5 m/s² | `command.accel_mps2`, the controller's declared clamp |
| speeds | ≥ 0 m/s | `truth.ego_speed_mps`, `truth.lead_speed_mps`; the plant has no speed floor |
| spacing error | ≤ 3 m over [15, 20) s | `gap_m − (5 + 1.5·max(ego_speed_mps, 0))` |
| relative speed | ≤ 1 m/s over [15, 20) s | `truth.relative_speed_mps` |

Desired spacing is defined at zero speed. No headway or time-to-collision
division is used, so no KPI has a singular point.

Each KPI is enforced twice: in-run by a Test participant, which aborts the Run
with exit 1 and a diagnostic naming signal, publication time, value and bound;
and post-hoc over the complete Recording, including the final publication that
has no in-run delivery.

## Numerical thresholds

| Comparison | Budget |
| --- | --- |
| SiL against the independent FMPy path, scenarios | absolute 1e-10 SI, relative 1e-12 |
| SiL against the independent FMPy path, sensitivity rows | absolute 1e-9 SI |
| Timing sensitivity against the 10 ms comparison | the acceptance envelope below |

The acceptance envelope is authored before any Run: 1.0 m on positions and
gap, 0.5 m/s on speeds, 0.5 m/s² on command, 1e-12 on the authored maneuver
input, and 1.0 m on the minimum-gap difference. `handoff.json` carries the
same numbers. Its rationale is in `sensitivity-report.json`
(`acceptance_envelope_basis`): across the 20 ms normal timing limit the
controller's full output range can change speed by 0.09 m/s in one interval,
so 0.5 m/s leaves a fivefold allowance and remains 2 % of the 25 m/s initial
speed.

A bus adds latency and jitter. The envelope above is the yardstick for what
that latency is allowed to do to the trajectory before it counts as a
behavioral change rather than a timing shift.

## Time and determinism conventions a bus milestone inherits

- Integer nanoseconds, Run epoch 0, no activation at Duration.
- A Message carries its publication Slot; a plant output describes publication
  Slot plus the publishing Participant's Period.
- Inputs are held for the whole communication interval.
- Determinism is judged per Manifest: author twice, run twice, compare bytes.
  Recordings from different Manifests, different Periods or different
  transports are never byte-compared.
- A new transport is a new Manifest identity. Existing evidence is not
  rewritten.

## Measured baseline values

The numbers a regression compares against come from the evidence the bundle
produces, not from this document:

| Where | Field |
| --- | --- |
| `curated/report.json` → `results.scenarios.<name>.minimum_gap_m` | the achieved minimum gap per scenario |
| `curated/report.json` → `results.scenarios.<name>.acceleration_range` | the observed command range |
| `curated/report.json` → `results.scenarios.dropped.expected_failure` | the deliberate failure's instant and diagnostic |
| `curated/report.json` → `results.sensitivity.<row>.sensitivity_comparison` | the timing sensitivity against the 10 ms comparison |
| `curated/report.json` → `determinism` | Manifest and Recording identities per check |
| `bundle.json` → `fmu_sha256` | the exact models those numbers belong to |

## What does not carry over

- CAN frame layout, DBC signal packing, arbitration and bus scheduling.
- Sensor perception and vehicle-physics fidelity.
- Any safety claim. The minimum-gap KPI is an integration threshold, not an
  ADAS requirement.
- Wall-clock throughput. It is observational here and closes no performance
  issue.
