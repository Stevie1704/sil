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
(see [Gaps](#gaps-for-233)), and the two FMUs cannot share one process
(see [One process](#one-process)).

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

Toolchain (`bundle.json` → `toolchain`): GCC 12.2.0, glibc 2.36,
`protoc` and `libprotobuf-dev` 3.21.12, all from the Debian bookworm snapshot
of 2026-09-01. CMake is 4.4.3 from the hash-pinned
[lock file](requirements.lock) (an FMPy dependency). It comes before Debian's
CMake 3.25.1 on the `PATH`.

## Results

CI run 36986891466 on native Linux x86-64 ([`evidence/`](evidence/),
[`ci-run.txt`](evidence/ci-run.txt)):

| Item | Value |
| --- | --- |
| `OSMPDummySensor.so` | SHA-256 `a0c38eda…`, archive `e6dece86…` |
| `OSMPDummySource.so` | SHA-256 `43bab291…`, archive `3409a410…` |
| Rebuild | every archive member is identical in both builds |
| ELF | x86-64. `NEEDED`: `libprotobuf.so.32`, `libstdc++.so.6`, `libm.so.6`, `libgcc_s.so.1`, `libc.so.6` |
| Configuration request | 20 ms update cycle, range 148.5 m |
| Expected slice | 1500 steps, 7890 detections, exact agreement to 1e-9. Repeat run identical |
| Source | 1500 SensorViews equal the closed form |
| Failing control | diverges at step 1 (40 ms), object 0, `x`: 40.08 m instead of 40.16 m |
| Both FMUs in one process | aborts (signal 6): `File already exists in database: osi_version.proto` |
| Sizes | source SensorView 1996 to 2004 B; SensorData 1396 to 1940 B |
| Sensor step cost | 92.2 µs mean `fmi2DoStep` (138 ms for 1500 steps) |
| Sensor process peak RSS | 84280 KiB (FMPy, Python Protobuf and one FMU) |

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
| Freshness | The sensor consumes the ground truth of the same instant `t_ns`. No stale-data policy is involved |
| Outputs | `bundle/references/expected-detections.json` → `contract.outputs` gives the meaning of each field. The existence probability is a demonstration value, not a calibrated probability |
| Parameter | `nominalrange`, default 135 m |

FMPy runs the FMUs as an independent FMI 2.0 importer, each FMU in its own
process (see [One process](#one-process)):

- **Source** (`drive.py source`). Every SensorView must equal the
  closed-form ground truth to 1e-9, with host 14, sensor 10000, no mounting
  position and the truncated timestamp.
- **Sensor** (`prepare.py`). In each step the proof serializes the
  closed-form SensorView into a buffer that it owns. It sets the three OSMP
  integers to that buffer, as an importer does, and then steps the sensor.
  Every SensorData must equal the expected slice: the same vehicles in the
  same order, tracking ids, timestamps, and each position, orientation,
  dimension and existence probability to 1e-9. `valid` must be true and
  `count` must equal the number of reported objects.

The proof also requires that:

- the sensor's `SensorViewConfiguration` request declares a 20 ms update
  cycle and a range of 1.1 × `nominalrange`. The proof reads the request in
  initialization mode, because upstream sets it to zero once the simulation
  starts;
- two sensor runs produce identical outputs;
- with `nominalrange = 100`, the output agrees with that range's own slice
  and differs from the 135 m slice.

`report.json` → `runs.transitions` lists each time the set of reported
vehicles changes. The 30 s cover a vehicle entering the cone, vehicles
leaving the range and vehicles falling behind the host.

**Failing control.** The sensor receives the previous step's SensorView: a
connection delayed by one Period. The comparison must diverge at step 1, in
a position field.

## One process

Both FMUs link OSI statically and the system `libprotobuf` dynamically.
Each FMU therefore registers the OSI `.proto` files in the one process-wide
Protobuf pool. When a second FMU (or the same FMU from a second path) is
loaded into the same process, libprotobuf aborts with `File already exists
in database: osi_version.proto`. The proof loads both FMUs into one child
process and requires this failure (`runs.both_fmus_in_one_process`). If
upstream changes this behaviour, the proof stops and asks for a new review.

The OSMP pointers are valid only inside the process that loaded the FMU, so
an OSMP connection needs both FMUs in one process. The existing placement
of connected FMUs in one process participant
([ADR 0001](../../docs/adr/0001-connected-fmus-in-one-process-participant.md))
therefore cannot hold these two archives as built. #233 must choose one of
these options:

- build with `LINK_WITH_SHARED_OSI=ON` (one shared OSI library);
- link Protobuf statically with hidden symbols;
- connect the sensor only to an importer-owned SensorView, as this proof
  does.

## Gaps for #233

| Gap | Owner |
| --- | --- |
| FMI 2.0 Co-Simulation; the Importer drives FMI 3.0 only | #191 |
| OSMP binary variables: memory addresses in `fmi2Integer` variables | #244 |
| Runtime `libprotobuf.so.32` must be in the runtime image | #233 |
| Two of these FMUs cannot share one process | #233, #244 |

## Throughput

`report.json` → `runs.observed` gives the SensorView and SensorData sizes
and the time spent in the sensor's `fmi2DoStep` over one 1500-step run
(`sensor_do_step_mean_us`). The proof's own checks and the Python
serialization are not in that time. `runs.source.sensor_view_bytes` gives the
size of the source's own SensorView. `sensor_process_peak_rss_kib` is the
whole sensor process: FMPy, Python Protobuf and one FMU. The run is one
participant in one process. It is not aggregate CI throughput. This is the
target computation measurement for #125.
