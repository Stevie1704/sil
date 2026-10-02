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
| Process response deadline | 30 s per Run |

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

Pending the CI run.

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
