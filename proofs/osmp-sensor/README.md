# OSMP FMU target: build, inspection and expected slice (#230)

```sh
proofs/osmp-sensor/run-proof.sh [output-directory]   # default build/osmp-sensor
```

This closes the FMU part of [#230](https://github.com/Stevie1704/sil/issues/230)
for the target selected in
[docs/decisions/230-external-adas-targets.md](../../docs/decisions/230-external-adas-targets.md):
`OSMPDummySensor` from OSI Sensor Model Packaging `v1.6.0`, fed by
`OSMPDummySource` from the same commit. It gives #233 a pinned FMU binary, a
static compatibility report and an independently computed expected slice.
It does not run either FMU in SiL. The Importer cannot drive them yet
(see [Gaps](#gaps-for-233)).

The image build is the only step that uses the network. It fetches the
checkout pinned in [`sources.json`](sources.json) and stops unless the
commit, the tree and both submodule commits match. Everything else runs with
`--network none` on Linux x86-64 and writes:

- `bundle/`: both FMU archives with the OSMP licence, the expected slice
  (`references/expected-detections.json`) and `bundle.json`, which holds the
  source pins, the toolchain and one digest per file.
- `evidence/`: `fmu-inspection.json`, `report.json`, `build.log` and the
  gate-test log.

A check that fails stops the proof, so a failed check cannot write a passing
report. `.github/workflows/proof-osmp-sensor.yml` runs the proof on native
Linux x86-64 and uploads both directories. Retained evidence from that job is
in [`evidence/`](evidence/).

## Build

Upstream CMake project `examples/`, unmodified, targets `OSMPDummySensor`
and `OSMPDummySource`, with `-DCMAKE_BUILD_TYPE=Release`. Upstream defaults
to Debug in a Git checkout, so the build type is set explicitly.
`SOURCE_DATE_EPOCH` is the pinned commit's time, which fixes
`generationDateAndTime` in the model description. OSI is linked statically
into each FMU (upstream default `LINK_WITH_SHARED_OSI=OFF`). Protobuf is
not: the FMUs need the system `libprotobuf` at run time.

The build runs twice. `report.json` → `build.rebuild_identical_members`
states whether every archive member has the same bytes. This is reported,
not required: the bundle pins the first build.

Toolchain, from the Debian bookworm snapshot of 2026-09-01:
`bundle.json` → `toolchain` names the exact compiler, CMake, `protoc`,
`libprotobuf-dev`, `libc6` and `libstdc++6` versions.

## Static compatibility report

`evidence/fmu-inspection.json` has one entry per archive. No FMU code runs
for it:

- archive and shared-object digests, archive members and platform
  directories;
- the model description: FMI version, Co-Simulation capabilities, default
  experiment, every variable with type, causality, variability, initial and
  start;
- OSMP: the annotation's OSMP and OSI versions, and each binary variable
  with its MIME type and the value reference of `base.lo`, `base.hi` and
  `size`;
- arrays and clocks (none: FMI 2.0 has neither);
- the shared object: ELF machine, `NEEDED` libraries, required symbol
  versions, exported `fmi2` functions and `ldd`;
- `sil_gaps`: each reason the Importer cannot drive the FMU today, with
  its issue.

## Expected slice

`scene.py` computes the slice from the source's closed-form motion and the
sensor geometry that upstream documents. It never reads an FMU output.

| Item | Value |
| --- | --- |
| Generator | `OSMPDummySource`: 10 vehicles, ids 10 to 19, `x = x0 + t·v`, `y = y0 + 0.25·sin(t/v)`, z and orientation zero. Host vehicle 14. Deterministic and synthetic: it is not a recording |
| Sensor geometry | Relative to the host. Reported if `d ≤ 1.1 × nominalrange` (inclusive) and `x/d > 0.866025` (about ±30°). Existence probability `cos((2d − R)/R)` with `R = 1.1 × nominalrange` |
| Frame | The source sets no `bbcenter_to_rear` and no mounting position, so the sensor frame equals the host frame here: x forward, y left, z up |
| Units | metres, radians, seconds (OSI) |
| Sensor Period | 20 ms, from the sensor's own `SensorViewConfiguration` request |
| Run length | 1500 Periods (30 s) |
| Sample time | Row `t_ns` is the end of communication step `[t_ns − 20 ms, t_ns]`. Both FMUs stamp their output with that time |
| Freshness | The sensor consumes the SensorView that the source produced in the same step. No stale-data policy is involved |
| Parameter | `nominalrange`, default 135 m |

FMPy drives both FMUs as an independent FMI 2.0 importer. In each step the
source steps first. Its three OSMP integers are then copied to the sensor's
input, as an OSMP connection does, and the sensor steps. The proof requires:

- every SensorView to equal the closed-form ground truth (to 1e-9);
- every SensorData to equal the expected slice: the same vehicles in the
  same order, tracking ids, timestamps, and each position, orientation,
  dimension and existence probability to 1e-9;
- `valid` true and `count` equal to the number of reported objects;
- two runs to produce identical traces;
- with `nominalrange = 100`, agreement with that range's own slice, and a
  difference from the 135 m slice.

`report.json` → `runs.transitions` lists each time the set of reported
vehicles changes. The 30 s cover a vehicle entering the cone, vehicles
leaving the range and vehicles falling behind the host.

**Failing control.** The sensor receives the previous step's SensorView: a
connection delayed by one Period. OSMP keeps the previous output buffer valid
for exactly that step, so the pointer stays valid. The comparison must
diverge at step 1, in a position field.

## Gaps for #233

| Gap | Owner |
| --- | --- |
| FMI 2.0 Co-Simulation; the Importer drives FMI 3.0 only | #191 |
| OSMP binary variables: memory addresses in `fmi2Integer` variables | #244 |
| Runtime `libprotobuf` (see `NEEDED`) must be in the runtime image | #233 |

The OSMP pointers are valid only inside the process that loaded the FMU.
That fits the existing placement of connected FMUs in one process
participant ([ADR 0001](../../docs/adr/0001-connected-fmus-in-one-process-participant.md)).

## Throughput

`report.json` → `runs.observed` gives the SensorView and SensorData sizes
and the FMPy wall time of one 1500-step run. That is one single-process run,
not aggregate CI throughput, and FMPy overhead is included. Feed it to #125
together with the FMU's own step cost once #233 runs it in SiL.
