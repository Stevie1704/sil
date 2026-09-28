# Public single-FMU acceptance: `AccController` on OpenACC (#194)

```sh
proofs/public-workloads/run-proof.sh                 # once: the #178 bundle; needs docker and network
proofs/openacc-controller/run-proof.sh build/public-workloads/bundle [output-directory]
```

This is use case 2 of the adoption acceptance: recorded data into one FMU. It
takes the FMU, the recording window and the independent reference that
[`../public-workloads`](../public-workloads/README.md) (#178) qualified. It
replays the recording into the FMU through the supported SiL workflow and
compares every command with the reference.

No company artifact, vehicle or supplier is needed. The evidence is
**public-artifact adoption acceptance**. It is not production-vehicle
validation.

**Model origin.** `AccController` is written in this repository
([`../acc-fmi`](../acc-fmi/README.md)) and exported with PythonFMU3 0.3.4. It
is not a third-party or supplier model. The recording is third-party measured
data. Agreement shows that SiL executes the FMU correctly. It says nothing
about ADAS validity. Third-party FMU coverage stays open: no model here
claims it.

## What runs where

| Step | Where | Network | Output |
| --- | --- | --- | --- |
| Bundle (#178) | public-workload tool image | image build only | `AccController.fmu`, `openacc-window.csv`, `controller-open-loop.json` (FMPy), digests |
| Acceptance | example image: production runtime + this directory | none | `inputs/`, `runs/`, `evidence/report.json` |

The example image adds no package to the runtime image. The FMU declares
`needsExecutionTool=true`; the tool is CPython with `libpython`, which the
runtime image's base already has. SiL comes from the installed wheel and the
installed `sil-run`. FMPy, the exporter and the compiler stay in the #178 tool
image.

## Pins

The acceptance stops at the first difference:

| Artifact | Check |
| --- | --- |
| Recording window | SHA-256 equal to `bundle.json` and to the committed handoff ([`../public-workloads/evidence/handoff.json`](../public-workloads/evidence/handoff.json)) |
| FMPy reference | the same |
| Run contract | the bundle's `single_fmu_194` equal to the committed one, except the FMU digest |
| FMU archive | SHA-256 equal to `bundle.json` and to the bundle's own handoff |
| FMU model source | `resources/controller.py` equal to the committed [`../acc-fmi/models/controller.py`](../acc-fmi/models/controller.py); `resources/dynamics.py` equal to the installed `sil.examples.acc.dynamics`; both equal to the archive's embedded identity |
| Exporter | the identity's exporter equal to the audited `PythonFMU3 0.3.4` |
| Interface | `sil-fmi-inspect` verdict `compatible`; FMI version, model, exporter, capabilities, platforms and every variable (type, causality, unit, start, dimensions) equal to the committed [#178 audit](../public-workloads/evidence/fmu-audit.json) |
| Runtime | `ldd` of the Linux binary resolves; `libpython` is present |

**Why the FMU digest is not pinned across revisions.** The exporter embeds
the SiL revision in `resources/identity.json` and derives the instantiation
token from it. A bundle regenerated at another revision is another archive of
the same model. The acceptance therefore pins what does not change: the model
source, the control law, the exporter and the declared interface. The FMPy
reference is pinned exactly, so the model's behavior is pinned too.
`report.json` states whether the archive is the handoff's archive. When CI
regenerates the bundle at a new revision, it is not: the archive
`12df3da…` itself is then not executed, only the same model exported again.

## Initialization and shutdown

`sil-fmi-inspect` cannot verify statically that the FMU takes its start
values, initializes from `resources/`, and terminates. `lifecycle.py` checks
this outside a Run, with the installed importer's FMI calls, one lifecycle
per process:

- **With the resource path**: instantiate, set sample 0 as the start values,
  initialize, step 100 ms, terminate and free. The command after
  initialization and after the Step must equal the control law of sample 0
  exactly.
- **Without the resource path**: PythonFMU3 cannot find its model source, so
  `fmi3InstantiateCoSimulation` must return no instance. The process exits
  nonzero and its phases stop at `described`. The archive and its description
  are the same as above, so the refusal is at instantiation. This shows that
  initialization depends on `resources/` and that the importer supplies it.
  The check uses exit codes and phases, not diagnostic text.

`CoSimulation` has no public value accessor, so `lifecycle.py` sets and reads
values through `sil.fmi.runtime.ScalarBuffer`, the importer's own buffer. That
module is not in the compatibility policy. The workflow therefore runs on
every change under `python/src/sil/fmi/`.

In a Run, the importer terminates and frees the FMU after the last Step. If
`fmi3Terminate` fails, the importer exits nonzero and `sil-run` fails the Run.
Every accepted Run exited 0.

## The workflow

1. **Check the conversion** (`controller_replay.recorded_inputs`). For every row of the
   window CSV, the acceptance recomputes each derived column from the
   recorded ones and refuses a difference:
   `gap_m` = `IVS1` (bumper to bumper, never the antenna separation),
   `relative_speed_mps` = `Speed1 − Speed2`, `ego_speed_mps` = `Speed2`, and
   `t_ns` = k × 100 ms from 300.0 s. There are 501 samples.
2. **Convert** (`sil-csv`). The derived columns become the `acc.sensing`
   Channel, with `t_ns` as the Message time. The reference trajectory becomes the
   `reference.command` Channel, each row at the time it describes.
3. **Expect** (`inputs/expectations.json`). Before any Run, the control law
   over what the FMU sees in each variant gives the first divergence of each
   control. The law gives the pinned reference bit for bit.
4. **Author** (`sil-fmu-replay`). One authoring document per variant. The
   nominal document is authored twice; the two Manifests must be
   byte-identical.
5. **Run** (`sil-run`). The nominal Manifest runs twice; the two Recordings
   must be byte-identical. That is determinism, and it is separate from the
   agreement with FMPy below.
6. **Compare** (`sil-compare`) with the contract below.

## Run contract

| Item | Value |
| --- | --- |
| FMU profile | FMI 3.0 Co-Simulation, Linux x86-64, three Float64 inputs and one Float64 output, fixed communication step |
| Bindings | `acc.sensing` fields `gap_m` (m), `relative_speed_mps` (m/s), `ego_speed_mps` (m/s) to the inputs of the same names; `accel_mps2` (m/s2) to `acc.command` |
| Start values | sample 0 of each input, with its unit; the controller has no parameter |
| Period | 100 ms, the recording's grid |
| Duration | 50.1 s: Slots 0 to 50.0 s |
| `acc.sensing` Latency | 0: sample k is written before the Step [t_k, t_k+1) and held for it |
| `acc.sensing` route | capacity 1 + Latency ÷ period: 1 in the nominal Run; overflow fails the Run |
| `acc.command` Latency | 100 ms; recorded only |
| Observation | the command published in Slot t_k is the value at t_k + 100 ms (`actual_offset_ns` 100 ms) |
| Coverage | every reference row from 100 ms through 50.1 s: 501 observations, the final one included |
| Tolerance | `abs(actual − reference) ≤ 1e-10 + 1e-12 × abs(reference)` |

The final recorded sample is written in Slot 50.0 s and held one period past
the recording's end, so its command, at 50.1 s, is compared too.

## Controls

Each control changes one thing in the nominal Run. The expected verdict is
fixed before any Run. A failing control must first diverge at the
observation that the law predicts. There, the expected value must be the
reference value, and the actual value must be the law's value within the
contract's tolerance.

| Control | Change | Expected |
| --- | --- | --- |
| `changed-input` | `sil-csv` converts `relative_speed_mps` with scale −1: `Speed2 − Speed1` | fail, first divergence from the law |
| `wrong-binding` | the two speed fields bound to each other's input variable | fail, first divergence from the law |
| `one-period-shift` | `acc.sensing` Latency 100 ms: Step k sees sample k−1, Step 0 the start values. The route holds the one Message in flight, so its capacity is 2 | fail at 21.8 s, as #178 found |
| `declared-starts` | no start values: the FMU's declared starts (60 m, 0 m/s, 25 m/s) | pass |

**The wrong-parameter control is a binding.** The controller has no
parameter. A wrong start value is not observable either: at Latency 0,
sample 0 overwrites every start value before the first Step, and the
controller holds no state. `declared-starts` shows this: it must pass. The
wrong-parameter criterion is therefore met by `wrong-binding`, a mapping
error of the same kind: the Run is valid, the units agree, and the values go
to the wrong variable.

**The clamp window.** Until 21.8 s the command is held at its −3 m/s² clamp,
so no change that keeps it clamped can show there. `one-period-shift` and
`changed-input` both first diverge at 21.8 s for that reason. `wrong-binding`
leaves the clamp at once and diverges at the first observation.

## Retained results

[`evidence/`](evidence/) is the output of CI run 36386108635 on native
Linux x86-64 (glibc 2.36, CPython 3.13.7, `/usr/local/lib/libpython3.13.so`).
It holds `report.json`, every `sil-compare` report, the contract and
`expectations.json`. `report.json` includes the inspection result, the
lifecycle phases, the conversion receipts and the nominal authoring receipt.
The Recordings are not committed. The job keeps the full output as the
`openacc-controller-evidence` artifact: every receipt, the Manifests, the
runner output, the converted input Recordings and every Run's Recording,
so the two nominal Recordings and the 501 observations can be checked
independently.

| Check | Result |
| --- | --- |
| Pins | recording `968b007a…` and reference `f8607c9f…` as handed off; the FMU is the bundle's re-export `3f8a22e9…` at revision `6520667`, not `12df3da…`, with the committed model source and the installed control law |
| Interface | `sil-fmi-inspect` verdict `compatible`; equal to the #178 audit |
| Runtime | every library of the Linux binary resolves: libstdc++, libm, libgcc_s, libc |
| Lifecycle | with `resources/`: described, instantiated, initialized, stepped, terminated, command −3.0 m/s² as the law gives; without: exit nonzero, stopped after described |
| Conversion | 501 samples on `acc.sensing`, 0 to 50.0 s; 501 reference rows, 0.1 to 50.1 s |
| Nominal | 501 of 501 commands within tolerance, the final one at 50.1 s included |
| Determinism | two authorings: byte-identical Manifest `e8d342e7…`; two Runs: byte-identical Recordings `891b1cdf…` |
| `changed-input` | 251 divergences, the first at 21.8 s: −3.0, expected −2.9407 |
| `wrong-binding` | 501 divergences, the first at 0.1 s: 1.5, expected −3.0 |
| `one-period-shift` | 216 divergences, the first at 21.8 s: −3.0, expected −2.9407, as #178 found |
| `declared-starts` | pass: the start values are not observable |

Every first divergence is the one `expectations.json` fixed before the Runs.

Resource observations from that runner: a nominal Run took 0.46 s of
wall-clock time for 50.1 s of Virtual time. This is observational. It is not
an acceptance criterion and not a representative vECU Step cost for #125.

## Limits

- **Repository-authored model.** The FMU is not a third-party artifact. This
  is adoption evidence for the workflow, not third-party FMU coverage.
- **Stateless controller.** The model holds no state between Steps, so the
  start values cannot be judged by its outputs.
- **Linux x86-64 only.** The archive ships `x86_64-linux` and
  `x86_64-windows` binaries; only Linux is accepted.
- **Bundle expiry.** The CI bundle artifact expires. The workflow regenerates
  the bundle with `proofs/public-workloads/run-proof.sh`.
