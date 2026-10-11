# FMI 2.0 importer acceptance (#191)

```sh
proofs/fmi2-importer/run-proof.sh [output-directory]   # default build/fmi2-importer
```

This qualifies the FMI 2.0 co-simulation profile of the Importer
([docs/fmi.md](../../docs/fmi.md#fmi-20-co-simulation-profile)) for its named
consumer: `OSMPDummySensor` from osi-sensor-model-packaging `v1.6.0`, selected
in [#230](../../docs/decisions/230-external-adas-targets.md). Upstream exports
it as FMI 2.0 only. An FMI 3.0 port made here would be repository-authored
and would no longer be independent evidence.

The image build is the only step that uses the network. It fetches the OSMP
checkout pinned in [`../osmp-sensor/sources.json`](../osmp-sensor/sources.json)
and builds `OSMPDummySensor` exactly as [`../osmp-sensor`](../osmp-sensor/)
does. The Reference FMUs are the `v0.0.41` FMI 2.0 archives vendored under
[`tests/fixtures/reference-fmus/2.0/`](../../tests/fixtures/reference-fmus/README.md),
taken from the release asset that #178 pinned. No new download is needed.
Everything else runs with `--network none` on Linux x86-64 and writes
`evidence/report.json` and `evidence/fmu-inspection.json`.

A check that fails stops the proof, so a failed check cannot write a passing
report. `.github/workflows/proof-fmi2-importer.yml` runs the proof on native
Linux x86-64 and uploads `evidence/`.

## Checks

| Check | Requirement |
| --- | --- |
| Inspection | `sil fmi inspect` reports `fmiVersion` 2.0 and selects `Dahlquist`, `VanDerPol`, `BouncingBall`, `Stair` and `OSMPDummySensor`. It refuses `Feedthrough` and names each `String` and `Enumeration` variable |
| Reference FMUs | Each case runs in SiL and under FMPy 0.3.26 with the same start values and communication points. Every sample of the observation grid, the final one included, agrees within 1e-12 absolute plus 1e-12 relative (Real) or exactly (Integer) |
| Identity | Each case runs twice in SiL, and the two Recordings are byte-identical |
| Wrong input | `Dahlquist` with `k = 0.5` and `BouncingBall` with `e = 0.8` must diverge from the nominal reference |
| Independent failing status | `Stair` answers `fmi2DoStep` with Discard once its counter reaches 10, at 9 s. Run to 10 s, SiL fails the Run and names `fmi2DoStep returned Discard`, and FMPy fails at the same Step |
| Status failure | The fake FMU of `tests/fixtures/fmi2_fake.c`, whose `fmi2DoStep` answers Error, fails the Run (exit 1) and the diagnostic names `fmi2DoStep returned Error` |
| Cleanup | After a successful and a failed Run, nothing is left in the runner's directory. Driven directly, the Importer removes its own extraction after success and after a failed Step |
| Two instances | Two `VanDerPol` participants in one Run agree with each other and with FMPy |
| OSMP | `OSMPDummySensor` runs 50 Steps of 20 ms with its start values: no SensorView, so the input pointer is 0. `valid` and `count` agree with FMPy. If the FMU refused that lifecycle, both importers would have to fail and the proof would record how |

| Case | FMU | Step | Steps | Start values | Outputs |
| --- | --- | --- | --- | --- | --- |
| `dahlquist` | Dahlquist | 100 ms | 100 | | `x` |
| `dahlquist-k-0.5` | Dahlquist | 100 ms | 100 | `k=0.5` | `x` |
| `vanderpol` | VanDerPol | 10 ms | 2000 | | `x0`, `x1` |
| `bouncingball` | BouncingBall | 10 ms | 300 | | `h`, `v` (events inside the step) |
| `bouncingball-e-0.8` | BouncingBall | 10 ms | 300 | `e=0.8` | `h`, `v` |
| `stair` | Stair | 200 ms | 40 | | `counter` (Integer) |

The observation grid of a case is the end of every communication step,
`(k + 1) · step` for `k = 0 … steps − 1`. A SiL Message published in the Slot
at `t` is dated by its Sample time `t + step`. The FMUs have no inputs, so
the authored inputs are the start values above.

## Known limit

Two OSI FMUs in one process abort in the Protobuf pool
([One process](../osmp-sensor/README.md#one-process)). Two instances of
`OSMPDummySensor` are therefore not run in one process. This is a limit of
the FMU build, not an Importer defect. The OSMP binary variables are mapped
by #244 ([proofs/osmp-importer](../osmp-importer/README.md)).

## Results

CI run 37006499200 on native Linux x86-64 ([`evidence/`](evidence/),
[`ci-run.txt`](evidence/ci-run.txt)):

| Item | Value |
| --- | --- |
| Inspection | 5 archives `compatible`; `Feedthrough` `unusable`, naming `String_input`, `String_output`, `Enumeration_input`, `Enumeration_output` |
| Reference FMUs | all 6 cases agree with FMPy; largest difference 0.0 |
| Identity | every case: two Recordings byte-identical |
| Wrong input | `dahlquist-k-0.5` diverges at 0.1 s (`x` 0.95 against 0.9); `bouncingball-e-0.8` diverges at 0.46 s in `h` |
| Failing status | fake FMU: exit 1, `fmi2DoStep returned Error`. `Stair`: exit 1, `fmi2DoStep returned Discard`; SiL and FMPy both complete 44 Steps |
| Cleanup | nothing left after the successful or the failed Run, or after the Importer closes |
| Two instances | identical, and equal to FMPy |
| `OSMPDummySensor` | archive `e6dece86…`, binary `a0c38eda…`: the #230 pin. It accepts the lifecycle without a SensorView; `valid` 0 and `count` 0 at all 50 Steps, as under FMPy |
