# Installing and running the ACC acceptance bundle

This is the adoption path for the closed-loop FMI 3.0 baseline (issue #151):
two source-available ACC FMUs, the Runs that judge them, and the pinned
artifacts those Runs compare against. It depends on no company model, no
licensed tool and no network at Run time.

## One preparation command

```sh
proofs/acc-fmi/acceptance-bundle.sh prepare [bundle-directory]
```

The default bundle directory is `build/acc-acceptance-bundle`. This step is
the only one that needs the network. It

1. builds the pinned **tool image** (exporter PythonFMU3, independent importer
   FMPy, compiler, CMake) from `proofs/acc-fmi/Dockerfile`,
2. builds the **production runtime image** from the repository `Dockerfile`
   (`--target runtime`), unchanged,
3. builds the **example image** from `proofs/acc-fmi/Dockerfile.bundle`: that
   same runtime image plus the consumer material in `/opt/acc-example`,
4. runs the tool image with `--network none` to export both FMUs, audit them,
   write the authored configurations, and record the independent trajectories,
5. writes `bundle.json`: one SHA-256 digest per pinned artifact.

## Every later Run

```sh
proofs/acc-fmi/acceptance-bundle.sh run [bundle-directory] [evidence-directory]
```

The default evidence directory is `build/acc-acceptance-evidence`. The Run
executes in the example image with `--network none`. It

- re-hashes the whole bundle and refuses to start if one artifact differs,
- refuses to start unless the executing image is the one the bundle records,
- refuses to start if `sil` is importable from anything but the installed
  distribution, if a SiL source tree is on the import path, if the runner
  comes from a build tree, or if an exporter, a comparison importer or a
  compiler is present,
- authors each Manifest twice, executes each Manifest twice, and compares the
  bytes of both Manifests and both Recordings,
- compares each Recording against the pinned independent trajectory,
- requires the deliberate KPI failure to fail, with its diagnostic.

Nothing is written back into the bundle. The Run works in `/workspace`, the
image's documented working directory, and the evidence directory receives the
Manifests, Recordings, provenance side-cars, logs, `runtime.json`,
`results.json` and `curated/report.json`.

A rebuilt example image is a new execution artifact, and the Run says so
rather than executing against an image the bundle does not name. Rebuild the
image and the bundle together, with one `prepare`.

`SIL_ACC_PARTICIPANT_TIMEOUT_MS` bounds each Participant response; the default
is 30000 ms. It is a wall-clock deadline and never part of the authored
exchange grid. The continuous-integration job carries its own overall
deadline.

## Units and time conventions

All interface variables are Float64 scalars in SI units, declared in each
`modelDescription.xml`.

| Quantity | Unit | Notes |
| --- | --- | --- |
| position | m | positive forward along one longitudinal axis |
| speed | m/s | |
| acceleration | m/s² | |
| gap | m | lead position − ego position |
| relative speed | m/s | lead speed − ego speed; positive means the lead pulls away |
| time | ns | integer; the Run epoch is 0 |

Time conventions:

- A Message carries the **publication time**: the activation Slot it was
  published in.
- A published plant output describes the state at the **endpoint time**:
  publication Slot plus the publishing Participant's Period. Recording Slot
  `t` therefore holds the value reached at `t + period`.
- Initialization exposes the state at `t = 0`, before any integration.
- There is no activation at Duration. The last Slot is `Duration − period`.

## Default Channel Latency

A Message published at `t` becomes visible at `t + latency_ns`. When a Channel
declares no `latency_ns`, SiL applies the default: **one consumer activation**
(unit delay). Same-Slot feedthrough is not the default and has to be declared
with `latency_ns=0`.

Every Channel in this bundle declares its Latency explicitly:

| Channel | Latency | Reason |
| --- | --- | --- |
| `sensing`, `command`, `truth` | one Step (10 ms in the scenarios) | the modeled sample-and-hold loop |
| `maneuver` | 0 ns | the authored input must be visible in the Slot it is published in |
| sensitivity rows | per row, 0/10/20 ms and a 250 ms defect | Latency is the quantity under study |

Changing a Latency changes the Manifest hash, so it is always a different Run
with its own identity.

## Model limitations

- This is a simplified kinematic integration benchmark, not an ADAS product,
  not a validated vehicle model and not a safety qualification.
- The plant integrates `x(t+h) = x + v·h + a·h²/2`, `v(t+h) = v + a·h` with
  the held input. There is no tyre model, no actuator model, no delay model
  and no collision model.
- The plant has **no speed floor**. A negative speed is a failed KPI, not a
  clamped vehicle.
- The controller is one clamped law:
  `clamp(0.35·(gap − (5 + 1.5·ego_speed)) + 1.2·relative_speed, −3, 1.5)`.
  There is no fallback controller, no fault handling and no state machine.
- Sensing faults expose hold-last-value behavior. The bundle does not add a
  degraded mode; the dropped-sensing scenario is expected to violate the
  minimum gap.
- Desired spacing is `5 + 1.5·max(ego_speed, 0)` m, defined at zero speed. No
  headway or time-to-collision division is used anywhere.
- The qualified machine class is Linux x86-64 with glibc 2.36. The FMU
  binaries are `x86_64-linux` only.
- The archives declare `needsExecutionTool=true`: they carry model source, not
  a Python runtime. The example image supplies CPython 3.13 and
  `libpython3.13.so`.

## The FMI 3.0 profile this bundle establishes

`fmi-profile.json` in the bundle is generated from the archive audit and the
executed Runs. It is the profile **this evidence establishes**, not an FMI 3.0
conformance statement.

Supported and exercised:

- Co-Simulation, one constant communication interval per Run, taken from the
  Participant's Step Period.
- `fmi3InstantiateCoSimulation`, `fmi3EnterInitializationMode`,
  `fmi3ExitInitializationMode`, `fmi3SetFloat64`, `fmi3GetFloat64`,
  `fmi3DoStep`, `fmi3Terminate`, `fmi3FreeInstance`.
- Float64 scalar inputs and outputs with declared SI units and input starts.
- An absolute extracted `resources/` path with a trailing separator, supplied
  by the Importer.

Rejected by the qualified archives, and therefore by this bundle:

| Capability | Declared | Consequence |
| --- | --- | --- |
| `canHandleVariableCommunicationStepSize` | false | each period is a separate Run; no adaptive stepping |
| `canGetAndSetFMUState` | false | no rollback and no repeated Step from one instant |
| `canSerializeFMUState` | false | Run state cannot move between processes or hosts |
| `needsExecutionTool` | true | the image supplies the Python runtime |

Not exercised, and therefore not claimed: Model Exchange, Scheduled Execution,
Event Mode, Clocks, intermediate update, early return, Binary, String,
Enumeration and array variables, and any platform other than Linux x86-64.

## Diagnosing a reference mismatch

A comparison failure names the channel, the field, the exact time and both
values, for example:

```
sensing/gap_m publication_ns=1230000000 value=12.34 expected=12.31 tolerance=1e-10+1e-12*abs(expected)
```

Work through it in this order:

1. **Is the bundle intact?** The Run verifies digests first. A message naming
   missing, unrecorded or altered artifacts means the bundle was edited or
   partially regenerated. Prepare it again.
2. **Is the configuration the one that was pinned?** A message that the
   authored configuration differs from the pinned bundle configuration means
   the consumer material changed after preparation. Prepare it again, so the
   references and the configuration come from one revision.
3. **Which comparison failed?** `this Run differs from its pinned independent
   trajectory` is a real disagreement between SiL and FMPy at the same timing.
   `sensitivity exceeded the declared envelope` is a timing-sensitivity
   verdict against the authored envelope, not a bug in either engine.
4. **Missing exact observation time** means a Message is absent at a time the
   comparison needs. Nothing is interpolated. Read the Recording and the
   Manifest: a changed Period, Latency or Duration moves publication times.
5. **Compare the identities, not the plots.** `runtime.json` holds the runner
   digest, the installed `sil` version, the image identity and both FMU
   digests. `bundle.json` holds what preparation pinned. A differing FMU
   digest means a different model; a differing image identity means a
   different runtime.
6. Recordings from two different Manifests are never byte-compared. Only two
   executions of one Manifest are.

## Diagnosing an unsupported FMU

If you point the Importer at your own FMU, these are the failures the profile
above predicts:

| Symptom | Cause | What to do |
| --- | --- | --- |
| exit 2, `does not declare` | a `--bind` names a variable the model description has no entry for | read the names in `modelDescription.xml` |
| exit 2, `causality` | a bound variable exists but has the wrong causality | bind inputs to inputs and outputs to outputs |
| instantiation returns no instance, `basic_string: construction from null` | the exporter requires `resourcePath` | use a runner that supplies it; this repository's correction is described in `README.md` |
| the FMU declares `canHandleVariableCommunicationStepSize=false` and a Run uses several Periods | the archive is fixed-step | give each Period its own Run and Manifest |
| a variable is not Float64 | this bundle binds Float64 scalars only | see the Importer's other binding types in the repository `README.md` |
| the archive ships no `binaries/x86_64-linux` | the machine class is not qualified | export for Linux x86-64 |
| schema validation fails during preparation | the model description does not validate against the pinned FMI 3.0 schema | fix the export; the audit names the element |

An unsupported FMU fails during preparation, in the tool image, before any
acceptance Run exists. That is deliberate: the bundle only ever contains
artifacts that passed the audit.

## What the bundle contains

| Path | Content |
| --- | --- |
| `bundle.json` | digest of every other file, plus tool versions, FMU digests and the source revision |
| `images.json` | tool, runtime and example image identities |
| `fmus/` | both FMUs, their identity documents, `ldd` and symbol audits |
| `archives.json`, `schema-identity.json` | declared capabilities, exported symbols, the pinned FMI 3.0 schema digests |
| `archive-reproducibility.json` | two controlled exporter builds, their ZIP member timestamps and instantiation tokens |
| `configurations/` | the authored scenario and sensitivity configurations |
| `references/` | the independent FMPy trajectories the Runs are compared against |
| `fmi-profile.json` | the profile summarized above |
| `handoff.json` | the baseline signals and KPIs, described in [HANDOFF.md](HANDOFF.md) |
| `environment.json` | the preparation environment, including every Python package version |

## Related documents

- [README.md](README.md) — the proofs this bundle is assembled from.
- [HANDOFF.md](HANDOFF.md) — the baseline a CAN-connected ADAS regression reuses.
- [../../SUPPORT.md](../../SUPPORT.md) — which interfaces are compatibility surfaces.
