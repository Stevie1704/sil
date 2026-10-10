# Installed ADAS regression matrix

Issue [#228](https://github.com/Stevie1704/sil/issues/228) packages the
repository-owned processed-sensor experiments of
[#226](../adas-equivalence/README.md) and
[#227](../adas-closed-loop/README.md) with `sil bundle` and `sil bundle matrix`.
Supported acceptance platform: Linux x86-64, glibc 2.36, CPython 3.13.7.
Reference evidence establishes the integration contract, not supplier
compatibility or ADAS safety.

```sh
proofs/adas-matrix/run-proof.sh build/adas-matrix-evidence
# Reuse the prepared, sealed installation; no checkout needed at execution:
docker run --platform linux/amd64 --network none --init sil-adas-matrix:example
```

The shell command builds preparation tools from the closed-loop proof's pinned
Dockerfile, prepares the artifacts once, then builds a fresh consumer image
from the same pinned CPython base. Preparation needs network during the tools
image build, the checkout, a compiler, the installed headers and `silschema`,
the installed SiL wheel, PythonFMU3 and FMPy. The consumer image carries only
the installed native prefix, wheel environment, bundles and acceptance driver.
It has no `/src`, source-tree `PYTHONPATH`, compiler, exporter or FMPy.
For repeated preparation, `SIL_ADAS_MATRIX_TOOLS_IMAGE` can name a previously
built tools image from the same runtime/source revision; its immutable ID
is used for preparation. `SIL_ADAS_MATRIX_PYTHON_IMAGE` selects an equivalent
platform-specific base digest when an image-index resolver needs it.
Both matrices execute offline as UID 10001. The plant FMU embeds its PythonFMU3
runtime support; CPython's shared library is its external runtime dependency.
No exporter installation is required at execution.

To hand the prepared runtime and its sealed bundles to an offline adopter:

```sh
docker image save sil-adas-matrix:example -o adas-matrix-runtime.tar
# On the Linux x86-64 adopter host:
docker image load -i adas-matrix-runtime.tar
docker run --platform linux/amd64 --network none --init sil-adas-matrix:example
```

This transfers the installed runner, wheel, dependencies and already sealed
bundles together; no repository or preparation tools enter the adopter's
execution. To retain the adopter's evidence, create a container, start it,
`docker cp CONTAINER:/work/evidence/. evidence/`, then remove the container,
as `run-proof.sh` does. Use a new output directory for each execution.

The immutable image ID is archived in `image-id.txt`; use that ID in place of
the example tag to reproduce this installation. Locks are sealed in the
consumer image, so each records the consumer's dependencies. Matrix documents
pin each lock digest separately. Bundles live at `/bundles/<case>`; existing
bundle rules require these exact authored paths when moving between hosts.

| Matrix case | Experiments and comparisons |
| --- | --- |
| `replay-native` | all #226 maneuvers, Interceptors and Latency experiments; C Native participant vs enumerated oracle and independent FMPy |
| `replay-fmu` | same experiment, FMU controller vs the same references |
| `closed-native` | all #227 cases and sampling/hold/Latency variants; native controller over the plant FMU vs independent FMPy |
| `closed-fmu` | same loop and reference, FMU controller |

Each case is a sealed Regression bundle with all target archives/library,
participant code, processed-sensor Schemas, profile 3, calibration, freshness
limits, prepared references, conversion mappings/rows and receipts, authored
Manifests and Comparison contracts. Replay uses each complete authored
Recording: warm-up 0, no window selection or rebasing. `experiment.json`
records this policy and the Schema digest; the conversion receipts seal the
source/mapping identities. Closed-loop input declarations and independent
CSV/mapping identify the maneuver and reference. Preparation refuses a
controller archive that fails its qualified compiler pin and a plant whose
model identity or interface differs from #227's qualified plant.

Every nominal Run has a 30 s Process response deadline, executes twice for
Determinism, and compares against prepared references. A whole-case 600 s
guard bounds the bundle including verification, every Run and comparisons.
Cases execute serially in isolated evidence directories; adopters can invoke
`sil bundle matrix /opt/adas/nominal.json -o /work/nominal --jobs N` directly.
`sil bundle matrix /opt/adas/controls.json -o /work/controls` must exit 1.
The acceptance driver checks both matrix exit codes and every expected case
status. An optional-control matrix returning 0 fails acceptance even when
its diagnostics still detect every fault.

The controls are required cases, with these explicitly checked outcomes:

| Control | Expected status / diagnostic |
| --- | --- |
| sensor missing its initial condition | `manifest-error`, `config keys` |
| radar count 9, capacity 8 | `behavioral-failure`, malformed-list diagnostic |
| sensor-loss Run against an unfaulted independent reference | `behavioral-failure`; first divergence at observation 1.01 s, `adas.command.radar_age_ns`, 20 ms vs 0 |
| altered sealed profile document | `manifest-error`, altered artifact refused before a Run |
| native callback stalls | `timeout`, 30 s whole-case guard |
| native callback aborts | `behavioral-failure`, process terminates |
| controller FMU stalls inside `fmi3DoStep` | `behavioral-failure`, 30 s Process response deadline |

The existing matrix terminates case sessions, including participants in their
own process groups. The driver checks `/proc` for surviving processes that
name bundle artifacts. It also checks Run working directories (including FMU
extraction) and per-case `TMPDIR` files. Normal completion, manifest refusal,
malformed input and FMI deadline paths must clean themselves. A Run-owned marker written and closed by the fault callback proves that the
native hang/crash controls reached their callbacks rather than expiring during
startup or verification; the marker is checked before cleanup and its verdict
is archived. Native abort or
forced termination cannot execute runner destructors: after checking that
processes are gone, the external case owner removes those controls' remaining
Run directories, mapped-region files and any core dumps the host's
`core_pattern` writes into them, and records what it removed. The callback
marker is control evidence and is not listed as removed residue.
This is whole-case containment and file cleanup, not native crash recovery
inside the runner. The supported processes remain in their case session;
deliberately escaping via a new session is outside this lifetime contract.

Evidence includes `acceptance.json`, each matrix's summary JSON and JUnit,
logs, Recordings, provenance, Determinism verdicts, comparison JSON with
first-divergence diagnostics, and separate preparation identities and
independent execution reports. `.github/workflows/proof-adas-matrix.yml`
archives this tree even after failure and runs the installed CLI tests,
including the ignored-exit control. The runtime never prepares a reference.

## Replace a target

Keep the same processed-sensor Channels and Schemas, units, ego frame, list
capacity, finite routes, Periods, priorities, Latencies, duration, calibration,
freshness rules and Comparison contracts. For C, build the replacement with
`include/sil/participant.h` from the installed prefix and regenerate
`adas_messages.h` with installed `silschema schemas.json adas_messages.h`.
Export `sil_participant_init`; the library must accept the same native config
and register the same Tasks/subscriptions. Replace `adas_reference.so` at its
existing bundle path. For an FMU, replace `AdasReference.fmu` with an FMI 3.0
Co-Simulation FMU supporting the same typed variables, starts and 10 ms fixed
Step. If only variable names differ, update every controller `--bind` while
preserving the Channel/field types. Run `sil fmi inspect` before sealing.

For either replacement, declare every external native library and Python
module the target loads in `bundle.json`. Update `identities.json` and
`experiment.json` with the new target/calibration identity and replace the
reference proof's compiler/archive pin with an independently qualified target
identity during preparation. Seal the changed bundle in the final runtime,
then update `expect_lock` in the matrix from the printed digest. Keep the old
lock and reference evidence for attribution. Replacing an implementation under
an unchanged experiment should keep its references and Comparison contracts;
changing them requires an independent justification.

For a different interface, version and regenerate the Schemas/profile, rebuild
the native adapter or rewrite FMI bindings, revise edge conversion and finite
routes, regenerate mapping/conversion receipts, independently prepare new
references and author new Comparison contracts. For a calibration change,
change both the native config and FMI starts, record the calibration identity,
and regenerate independent references. Freshness is compiled reference policy
(40/80/20 ms for radar/camera/ego); changing it requires changing the C constants,
independently re-enumerating the replay oracle rows, bumping the profile
version, rebuilding both target forms and regenerating independent FMI
references, expected modes and contracts.
Reseal and repin all affected cases; never just relax tolerances to obtain a pass.

External artifacts are deferred to [#230](https://github.com/Stevie1704/sil/issues/230),
C qualification to [#232](https://github.com/Stevie1704/sil/issues/232), FMU
qualification to [#233](https://github.com/Stevie1704/sil/issues/233), and a
specified Ethernet interface to [#231](https://github.com/Stevie1704/sil/issues/231).
This example supports processed radar/camera Object lists, not raw images,
radar samples, real sensor physics, supplier message databases, CAN or Ethernet
vehicle-network profiles. FMI Model Exchange, Scheduled Execution, event mode,
early return, arbitrary variable Steps, network terminals/Binary/Clock profiles
and cross-platform bit-exactness are outside this acceptance experiment.
