# ADAS reference controller: native and FMU forms of one experiment (#226)

```sh
proofs/adas-equivalence/run-proof.sh [evidence-directory]   # needs docker and network
```

This proof runs the [ADAS reference controller](../../docs/adas-reference.md)
in two execution forms through installed SiL: the Native participant of the C
library, and [`AdasReference.fmu`](../adas-fmu/README.md), the FMI 3.0 export
of the same sources, through SiL's FMI importer. The experiment is declared
once. Each form is compared with the hand-enumerated oracle, with an FMPy
execution of the archive, and with the other form.

It is integration evidence for the reference product. Native and FMU agree
because they share `adas_reference.c`, so their agreement alone does not
validate that algorithm. The oracle and the negative controls do that. There
is no claim of supplier compatibility, general FMI conformance or ADAS safety.

## What runs where

| Step | Where | Network | Output |
| --- | --- | --- | --- |
| Proof image | `Dockerfile`: pinned Python base; Debian snapshot `build-essential` (GCC 12.2) and `cmake`, as in [`../adas-fmu`](../adas-fmu/README.md); FMPy 0.3.26 by hash; SiL built from the checkout and installed into `/opt/sil` (native prefix and wheel) | image build only | image |
| Proof | `prove.py` with the installed `sil` environment, Linux x86-64 | none | every report and Run |
| Independent execution | `independent.py` with the base interpreter, which has FMPy and no SiL | none | the FMPy Command rows |

## The experiment and its two forms

[`experiment.py`](experiment.py) declares the cases and authors both
Manifests with one call of `reference_manifest` in
[`examples/adas-reference/manifest.py`](../../examples/adas-reference/manifest.py).
Only the `controller` argument differs. So the two Manifests share the
Schemas, the Channels and their Latencies, the Interceptors, the Replay
participant and its Recording, the subscriber routes (three Messages, fail on
overflow) and the Duration. `form_difference` requires that nothing else
differs, and records what does:

| Declared once | Native form | FMU form |
| --- | --- | --- |
| controller Period 10 ms, offset 0, priority 0 | config `period_ns`; the library registers its Task | `step_period_ns`, the FMU's fixed step; Process priority 0 |
| parameters −3 m/s² and 0.5 m/s² | config keys | `--start` of the two Float64 parameters |
| Channel mapping | config `radar`, `camera`, `ego`, `command` | one `--bind` per Schema field ([`recorded-input.mapping.json`](../adas-fmu/recorded-input.mapping.json)) |
| artifact | the library, digest recorded | the archive, digest recorded and held to the [#225 pin](../adas-fmu/evidence/AdasReference.identity.json) |

Initial state: the native controller starts from `adas_ref_init`; the FMU
from its declared start values (nothing received, sequence 0, mode 2). Neither
publishes at initialization. Input delivery: a Message published in Slot t is
drained by the activation at t (Latency 0), and a route delivers in Publish
order. The native adapter receives each Message; the FMU takes a sensor's
inputs as a new observation when its header changed. Consumption: both hold
the last accepted observation and judge its age from its Sample time.

| Case | Inputs | What both forms must show |
| --- | --- | --- |
| `clear`, `hazard`, `release`, `unavailable`, `boundaries` | every sensor every 10 ms | the profile 1 behavior, endpoint cases |
| `occupancy`, `turnover`, `ordering` | lists of 0 to 8 objects | object selection, permutation, turnover |
| `cadence` | radar 20 ms, camera 40 ms, ego 10 ms | held inputs between observations |
| `freshness` | first radar at 20 ms, first camera at 40 ms | initial unavailable sensing, freshness limits, recovery |
| `cadence.drop` | camera lists in [40, 100) ms dropped | lost observations, stale data, recovery |
| `cadence.rewrite` | radar invalid in [100, 120) ms; ego sequence 14 in [150, 180) ms | invalid data, duplicates ignored and counted |
| `cadence.late` | one Period of input Latency | every observation one activation later |
| `cadence.delay` | radar lists published in [60, 100) ms delayed by 50 ms | a stale list on arrival; three lists at one activation |

### The one consumption difference

At 130 ms of `cadence.delay`, the radar route delivers the lists sampled at
80, 100 and 120 ms: the list sampled at 80 ms is delayed, and the two later
ones wait behind it. The native adapter receives all three in Publish order.
Each sequence exceeds the held one, so each is accepted, and the list sampled
at 120 ms is held after the activation. The FMU's inputs hold the last value
written, so the FMU takes only the list sampled at 120 ms. It holds the same
list, and neither form counts an ignored observation.

`experiment.SUPERSEDED` declares this difference: the activation, the sensor,
the superseded and the taken Sample times. The FMPy execution reports every
activation where it wrote more than one observation of a sensor, and the
report must equal the declaration. The Commands of both forms must still
equal the oracle and each other. If a superseded observation were one the
native form ignores and counts, the counters would differ, and the
comparisons would fail.

## Observations

The activation at Slot t publishes, in Slot t, the Command for Sample time
t + 10 ms. Every Run of both forms must hold exactly one Command per Slot
0 ms to 190 ms, with `sample_time_ns` equal to Slot + 10 ms and `sequence`
from 1: the first Command comes from the first activation, not from
initialization. The Duration, 200 ms, is a whole number of controller,
radar, camera and ego Periods, and the `cadence` inputs publish at exactly
those Periods. `freshness` and `cadence.late` start SENSOR_UNAVAILABLE.

| Comparison | Contract | Offsets (actual, reference) |
| --- | --- | --- |
| native or FMU against the oracle | `<maneuver>.contract.json` of `prepare.py` | one Period, 0 |
| native or FMU against FMPy | the same | one Period, 0 |
| FMPy against the oracle | the same, `actual_offset_ns` 0 | 0, 0 |
| native against FMU | `cross_form_contract` | one Period, one Period |

Every comparison observes the 20 Sample times 10 ms to 200 ms. Integer
fields (IDs, counts, modes, sequence numbers, Sample times, ages) are
`"exact"`. Both accelerations are `{"atol": 0, "rtol": 0}`: both forms
compute in binary64 from the same source with `-ffp-contract=off`, and round
each output once to binary32, so the declared rounding policy allows no
difference. Cross-form comparisons use Messages under these contracts, never
whole-file equality: the two forms have different Manifest hashes.

Each Manifest runs twice and must write identical Recording bytes.

## Independent execution

[`independent.py`](independent.py) drives the archive with FMPy. It reads the
expanded maneuver that `prepare.py` writes and a JSON of the case, applies
the drop, override, delay and Latency faults with its own code, with routes
that deliver in Publish order, and writes the Commands in the oracle's CSV
form. `sil recording csv` converts them. It also lists each activation where it wrote
more than one observation of a sensor.

## Negative controls

Each control changes exactly one thing in the nominal FMU form of one case.
Its first divergence from the oracle is predicted in `experiment.CONTROLS`
from the authored maneuver and the profile rules, before any Run. The proof
requires `sil compare` to fail (exit 1) with exactly that first divergence:
Sample time, field, actual and expected value.

| Control | Case | Change | Predicted first divergence |
| --- | --- | --- | --- |
| `binding` | `hazard` | the field `radar.x_m` bound to the variable `radar.y_m`; the variable `radar.x_m` keeps its start value 0 | 10 ms, `mode` 0, expected 1 |
| `sign` | `hazard` | the archive built with `ADAS_REFERENCE_WRONG_SIGN` | 10 ms, `mode` 0, expected 1 |
| `parameter` | `hazard` | `max_change_mps2` 1 | 10 ms, `acceleration_mps2` −1, expected −0.5 |
| `sample_time_offset` | `hazard` | the contract's `actual_offset_ns` 0 | 10 ms, `sample_time_ns` 20 ms, expected 10 ms |
| `input_latency` | `cadence` | the experiment's input Latency, one Period instead of 0 | 10 ms, `mode` 2, expected 0 |

The experiment declares one input Latency, and `reference_manifest` gives it
to every input Channel, as for the native `late` experiment. The Sample-time
offset control changes the comparison contract: both forms compute the
Sample time t + 10 ms themselves, and the contract is where an observer can
mistake the publication Slot for it. The `sign` control covers "unit/sign";
the profile has no unit conversion, since every value is in its SI unit on
both paths.

## Failures

Each failure runs the FMU form of `hazard` with one change, under
`--participant-timeout-ms 30000` and a whole-Run guard of 300 s. It must exit
with the established code, name its cause in the diagnostic, and leave no
importer process behind.

| Failure | Change | Exit | Diagnostic names |
| --- | --- | --- | --- |
| `incompatible_array` | the `[8]` variable `radar.x_m` bound to the scalar field `ego.speed_mps`, and back | 2, before the FMU is loaded | the variable, its dimensions and the field |
| `unsupported_step` | a 20 ms step; the FMU has one fixed 10 ms step | 1 | the FMU's message and `fmi3DoStep returned Error` |
| `parameter_out_of_range` | `max_change_mps2` 20 | 1 | the FMU's message and `fmi3ExitInitializationMode returned Error` |
| `malformed_input` | an Interceptor writes `radar.count` 9 in [20, 40) ms | 1, in both forms | `t=20000000 ns: radar.count 9 exceeds the capacity 8` |

The importer gets the step size at the first Step, not at initialization,
so it cannot refuse a 20 ms step before the Run without a Step protocol
change. The FMU refuses it, and every failed co-simulation call is exit 1
with the call and the FMU's message in the diagnostic
([FMI importer](../../docs/fmi.md)).

After `fmi3Error` the importer frees the instance without `fmi3Terminate`,
as FMI 3.0 requires, so the diagnostic names the failed call and not a
cleanup call. A Process participant's hang is bounded by its response
deadline. A Native participant runs inside `sil-run`, so its crash or hang
is bounded only by the whole-Run guard outside the Run. At the guard,
`prove.py` reads the process tree of `sil-run` and kills every process in
it, also the participants that `sil-run` starts in process groups of their
own. The container runs with `--init`, which reaps them.

## Evidence

[`evidence/`](evidence/) holds the reports the proof wrote on Linux x86-64:

| File | Content |
| --- | --- |
| `summary.json` | each step's result, the archive digest and the pin verdict |
| `artifacts.json` | the archive, the wrong-sign archive and the library, with digests |
| `forms.json` | per case, what both Manifests share and what differs |
| `runs.json` | each Run twice: exit, time, byte identity, observation findings |
| `independent.json` | each FMPy execution, and its superseded observations against the declared ones |
| `comparisons.json` | each comparison report, with its counts and first divergence, and every contract document |
| `controls.json` | each control, its prediction and what was observed |
| `failures.json` | each failure, its exit, diagnostic, time and leftover processes |
| `image-id.txt` | the ID of the image this build made, as `docker build --iidfile` wrote it |

The committed evidence was written under linux/amd64 emulation on an arm64
host. The `proof-adas-equivalence` workflow runs the same proof on native
Linux x86-64. The Recordings, Manifests and archives are in `work/` of the
evidence directory and are not committed.
