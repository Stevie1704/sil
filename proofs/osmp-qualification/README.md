# OSMPDummySensor qualification (#233)

```sh
proofs/osmp-qualification/run-proof.sh [evidence-directory]   # default build/osmp-qualification
# Run the prepared, sealed installation again; no checkout needed:
docker run --platform linux/amd64 --network none --init sil-osmp-qualification:example
```

This qualifies the external FMU that
[#230](../../docs/decisions/230-external-adas-targets.md) selected:
`OSMPDummySensor` from osi-sensor-model-packaging `v1.6.0` (MPL-2.0), driven
by `OSMPDummySource` from the same commit. It runs through installed SiL,
offline, as sealed Regression bundles under `sil-matrix`. The independent
importer is FMPy, in [#230](../osmp-sensor/README.md). Supported acceptance
platform: Linux x86-64, glibc 2.36, CPython 3.13.7.

This is an independent target. No native application represents the same
function, so this proof makes no equivalence claim. `OSMPDummySensor` is a
demonstration sensor model, and `OSMPDummySource` is a deterministic,
synthetic generator, not a recording. The evidence proves the integration
contract below. It does not prove supplier compatibility or ADAS safety.

## What runs where

| Stage | Holds | Does |
| --- | --- | --- |
| `prepare` | checkout, GCC 12.2, CMake, `protoc`, the pinned OSMP sources, FMPy | builds both FMUs as [`../osmp-sensor`](../osmp-sensor/) does, inspects them statically, writes the references and the bundles. Runs no FMU code |
| `runtime` | the pinned Python base, installed SiL (native prefix and wheel environment), Protobuf 4.21.12 (hash-pinned), `libprotobuf32` from the build's Debian snapshot, the bundles | seals each bundle while the image is built; at execution, `run.py` runs both matrices and the cost measurement with `--network none` as UID 10001 |

The runtime has no checkout, compiler, `protoc`, CMake, exporter or FMPy.
Each bundle declares them as excluded, so `seal` and `run` refuse the bundle
if one is reachable.

## Pins and static inspection

The OSMP checkout, its submodules, the build and the toolchain are the
pins of [#230](../osmp-sensor/README.md#results). The preparation rebuilds
both FMUs and reports whether each binary equals its #230 pin
(`preparation/report.json` → `build`). Before any FMU runs, it records:

| Item | Source in `preparation/report.json` |
| --- | --- |
| `sil-fmi-inspect`: both archives `compatible` with every mapping the Runs use | `inspection` |
| Archive members, resources (none), platforms (`linux64`) | `packaging.<FMU>` |
| Declared restrictions: `canBeInstantiatedOnlyOncePerProcess`, `needsExecutionTool`, FMU state | `packaging.<FMU>.declared` |
| Native libraries the loader resolves (`ldd`): `libprotobuf.so.32` beyond the C and C++ runtimes | `packaging.<FMU>.native_libraries` |

The compatibility gaps that #230 named are closed or bounded:

| Gap | Closed by |
| --- | --- |
| FMI 2.0 Co-Simulation | [#191](../fmi2-importer/README.md): the FMI 2.0 profile of the Importer |
| OSMP pointer-in-Integer binaries | [#244](../osmp-importer/README.md): mapped to bounded byte payloads at the Importer edge |
| Two of these FMUs abort in one process | each FMU runs in its own Process participant (#244). Not rebuilt |
| `libprotobuf.so.32` at run time | installed in the runtime image, declared in every bundle as a file dependency and sealed with its digest |

Not needed: #199 (no Clocks or events), #192 (no raw data), #188.

## The experiment

| Item | Value |
| --- | --- |
| Period | 20 ms (the sensor's configuration request) for every participant |
| Duration | 30 s, 1500 Steps |
| Participants | `source` (priority 0), `sensor` and `sensor-50m` (priority 1), `decoder` (priority 2), each its own Process participant |
| Parameters | `sensor`: `nominalrange` at its start value 135 m. `sensor-50m`: `--start nominalrange=50.0` before initialization |
| Connection | `osi.SensorView`, Latency 0: each sensor consumes the ground truth of the instant its own Step ends at |
| Payload bound | 4096 bytes per SensorView and SensorData |
| Process response deadline | 30 s for each `ready` and `step_done` answer |

The Importer moves OSMP payloads as bytes. `osi_edge.py` is an edge
participant of the bundle, not part of SiL. Its `Decoder` decodes each
SensorView into `osi.GroundTruth` and each SensorData into
`<sensor>.Detections` in the Slot it receives them. It uses Python classes
that `protoc` generated from the `.proto` files the FMUs were built from.
`sil-compare` then compares every field.

### Observation grid and tolerances

Each contract compares every Step: observation times 20 ms to 30 s, step
20 ms, both ends included. The first observation is the end of the first
Step and the last one is the Duration. The actual offset is one Period: the
Importer publishes in the Slot at `t` what its FMU reached at `t + 20 ms`.
Integer fields (ids, counts, timestamps, `valid`) are exact. Float fields
are `atol 1e-9, rtol 0`. Both sides compute in binary64 with the same
formulas. The bound covers a different operation order and the last bits of
libm's `sin` and `cos`. It is the bound of #230 and #244 and is not fitted to
an observation.

### References

[`references.py`](references.py) computes every expected value from the
closed-form source motion and the sensor geometry that upstream documents
([`../osmp-sensor/scene.py`](../osmp-sensor/scene.py)), at the Importer's
communication points. Nothing reads an FMU output. Each reference is a CSV
with its `sil-csv` mapping, converted into a Recording with a receipt.

| Reference | Expected behavior |
| --- | --- |
| `nominal` | ground truth of all 10 vehicles, and each sensor's detections at its own range |
| `late` | prediction for a SensorView one Period late: the first Step has no input, so no valid output; each later Step reports the ground truth of the previous instant with the timestamp of its own (upstream takes the timestamp from the FMU time) |
| `unparseable` | valid output with no objects at every Step: upstream ignores the result of `ParseFromArray`, so only a missing input (size 0) gives no valid output |

## Initialization

No Run Channel carries an FMU's outputs at the end of initialization: the
Importer publishes outputs only after each `fmi2DoStep`. So, before the
matrices, `run.py` starts [`initial.py`](initial.py) once for each FMU
instance of the nominal Run (`source`, `sensor`, `sensor-50m`), each in its
own process. It uses the installed Importer's own classes with that
instance's Manifest bindings and start values: extract, bind, instantiate,
apply the start values, set up the experiment, enter and exit initialization
mode. Then it reads every output-direction Channel once, terminates the FMU
and removes the extraction. No Step runs. Each sensor also binds its
`OSMPSensorViewInConfigRequest`, a calculated parameter.

The prediction ([`references.initial_outputs`](references.py)) comes from
the upstream sources. `doInit` sets every variable to zero and every Boolean
to false, and no initialization call sets an output. So each SensorView and
SensorData payload is empty, `valid` is 0 and `count` is 0. The sensor
computes its configuration request only when an importer reads it before
`fmi2ExitInitializationMode`, so a read after initialization, as SiL does,
gives an empty request.

## Matrix

| Case | Run | Required outcome |
| --- | --- | --- |
| `nominal` | `nominal`: source, both sensors, decoder | `pass`: exit 0, two byte-identical Recordings, every field of both sensors and the ground truth equals `nominal` |
| | `unparseable`: a SensorView no parser accepts, into `sensor` | `pass`: exit 0, two byte-identical Recordings, equals `unparseable`. The FMU returns `fmi2OK` and reports `valid` 1 with no objects: it does not detect malformed input |
| `control-late` | `osi.SensorView` with the default Latency | `behavioral-failure`: exit 0; `independent` (against `nominal`) fails, `predicted` (against `late`) passes. The first divergence must be the one preparation computed by comparing the two references |
| `control-binding` | `sensor.Status:count` bound to the variable `objectcount`, which the sensor does not declare | `manifest-error`: Run exit 2 before any Step. The log names `'sensor'` and `objectcount` |
| `control-size` | `sensor.SensorData` bound to 1024 bytes, below the smallest SensorData | `behavioral-failure`: Run exit 1 at the Importer edge. The log names `OSMPSensorDataOut` and the bound |

The nominal matrix must exit 0 and the controls matrix must exit 1. Three
kinds of failure therefore stay separate. An FMU status is reported in the
outputs and the Run still exits 0: the sensor reports `valid` 0 only when it
has no input, which is the first Step of `control-late`, and it reports
malformed input as valid (`unparseable`). A malformed Manifest
(`control-binding`) exits 2 before any Step. A Run failure at the Importer
edge (`control-size`) exits 1. After each case, `run.py` requires that no process survives and
that no Run working directory remains.

Two instances of `OSMPDummySensor` run in one Run, each in its own process.
The FMU declares no restriction against this. Each nominal Manifest runs
twice, and the two Recordings must be byte-identical.

## Cost for #125

After the verdicts, `run.py` measures the source and one sensor, each in
its own Importer, without the decoder and with `--no-recording`: a Run of
one Step (`startup`) and the whole 30 s (`long`), one warm-up and five timed
Runs each, median. The difference over the extra 1499 Steps estimates one
Step of both FMUs with their Step protocol round trips. These figures are
observational wall-clock on one CI machine, not a capacity claim. The
measurement policy of [`../adas-cost`](../adas-cost/README.md) applies.

## Results

CI run 37026128629 on native Linux x86-64 (GitHub-hosted `ubuntu-latest`, 4
vCPUs): [`evidence/`](evidence/), [`ci-run.txt`](evidence/ci-run.txt). The
runtime image ID is in [`image-id.txt`](evidence/image-id.txt).

| Item | Value |
| --- | --- |
| Build | `OSMPDummySensor.so` `a0c38eda…`, `OSMPDummySource.so` `43bab291…`: both equal the #230 pins |
| Inspection | both `compatible`. Resources: none. Platform: `linux64`. `canBeInstantiatedOnlyOncePerProcess`, `needsExecutionTool`, FMU state: all false |
| Runtime dependencies | `libprotobuf.so.32`, `libz.so.1`, `libstdc++.so.6`, `libgcc_s.so.1`, `libm.so.6`, `libc.so.6`, the loader: declared and sealed in every bundle |
| Initialization | `source`, `sensor`, `sensor-50m`: every output equals the prediction (empty payloads and configuration requests, `valid` 0, `count` 0); no extraction left |
| `nominal` | `pass`: 1500 of 1500 observations on each of `osi.GroundTruth`, `sensor.*` and `sensor-50m.*`. Recordings byte-identical (`b382560d…`) |
| `unparseable` | `pass`: 1500 of 1500 observations. `valid` 1, no objects, `fmi2OK` |
| `control-late` | `behavioral-failure`, Run exit 0. `predicted` passes. `independent` first diverges as predicted: `sensor.Detections.nanos` at 20 ms, 0 instead of 20000000 (no input, so no output) |
| `control-binding` | `manifest-error`, Run exit 2: `participant 'sensor': … Channel 'sensor.Status' field 'count' names FMU variable 'objectcount', which FMU 'OSMPDummySensor' does not declare` |
| `control-size` | `behavioral-failure`, Run exit 1: `participant 'sensor' failed: … OSMP binary variable 'OSMPSensorDataOut' reports 2870 bytes; the Channel carries 1024, and nothing is truncated` |
| Cleanup | no process and no Run working directory left after any case |

Cost (observational, median of five, Recording off):

| Quantity | Value |
| --- | --- |
| `startup`: one Step of source and sensor, including both Importers' start, extraction, instantiation and initialization | 285.1 ms (280.5 to 364.1) |
| `long`: 30 s | 1.235 s (1.227 to 1.263): real-time factor 24.3 |
| one Step of both FMUs, with their Step protocol round trips (estimate) | 634 µs, spread 79 µs |
| the sensor's own `fmi2DoStep` under FMPy ([#230](../osmp-sensor/README.md#results)) | 92 µs |

Per FMU Step this is about five times the 62.6 µs that
[#229](../adas-cost/README.md#reference-results) measured for its FMU form. The
OSMP payloads are about 2 to 3 kB, against 17 to 185 B there. They cross
the Step protocol inline, three times per Step. This measurement does not
find which part of the cost comes from the payloads and which from the FMUs'
own Protobuf encoding and decoding.

## Coverage and what remains

- Exporter and profile: one FMU built from source by the PMSF manual FMU
  framework of osi-sensor-model-packaging `v1.6.0`, FMI 2.0 Co-Simulation,
  OSMP binary variables, `linux64`, fixed 20 ms Step. No other exporter,
  OSMP version or platform is covered.
- The two FMUs still cannot share one process. A coupled OSMP connection
  inside one Process participant needs a rebuild with a shared OSI library.
- Not covered: Model Exchange, Scheduled Execution, events, variable Steps,
  FMU state, OSMP `SensorViewConfiguration` negotiation (the request is
  inspected, the configuration parameter keeps its start value), and
  bit-exactness across platforms.
- No native form exists, so there is no equivalence comparison.
- The input is the synthetic `OSMPDummySource` generator that #230
  selected, not a pinned Recording.
- FMPy, the independent importer, ran in #230 at `nominalrange` 135 m and
  100 m and with a one-Period input delay. It did not run the 50 m sensor
  or the unparseable input. For those, the closed-form references are the
  only independent evidence.
- The Run comparisons start at the end of the first Step. The outputs at
  the end of initialization are checked by `initial.py`, through the
  installed Importer, outside a Run.
- SiL reads calculated parameters only after initialization. So
  `OSMPDummySensor`'s SensorView configuration request is always empty in
  SiL, and the OSMP configuration exchange is not available. FMPy read it in
  #230 during initialization mode: 20 ms, 148.5 m.
- `OSMPDummySensor` returns `fmi2OK` from every call. No case gives a
  non-OK FMI status. Its failure states are the `valid` and `count`
  outputs only.
