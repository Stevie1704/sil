# Acceptance proofs and maintained models

[Documentation index](README.md) · [Repository overview](../README.md)

Commands below run from the repository root unless stated otherwise.

## Consumer adoption proof

[proofs/esmini/](../proofs/esmini/) is that derived-image pattern carried through
end to end with a Participant this repository did not write: the third-party
scenario engine [esmini](https://github.com/esmini/esmini) `v3.8.1`, driving a
UN-R157 cut-in scenario as a Process participant. It starts from the published
runner image by digest, declares its own Schema, Channels, route capacities
and Duration, is judged by a domain KPI in-run and post-hoc, reproduces
esmini's own expected trajectory within tolerance, and records bit-identically
across two Runs.

```sh
proofs/esmini/run-proof.sh            # needs docker and network
```

Nothing in that directory is part of the framework. Its README is the evidence:
what worked unchanged, what needed consumer-side adaptation, and what the
current contracts cannot represent.

## ACC acceptance bundle

[proofs/acc-fmi/](../proofs/acc-fmi/) also ships the closed-loop FMI 3.0 baseline
as something a consumer can adopt: two source-available ACC FMUs, the Runs that
judge them, and the pinned artifacts those Runs are compared against.

```sh
proofs/acc-fmi/acceptance-bundle.sh prepare     # once; needs docker and network
proofs/acc-fmi/acceptance-bundle.sh run         # offline, from the installed bundle
```

Preparation is the only step that needs the exporter, the independent importer
and a compiler. The acceptance Runs execute in the production runtime image
plus consumer material: the installed wheel and the installed `sil-run`, no
source tree on the import path, no network, and no build or comparison tool in
the image. They re-hash every pinned artifact first, compare a nominal
trajectory and a timing-sensitivity envelope against independently recorded
references, require the deliberate KPI failure to fail, and compare two
Recordings of each Manifest byte-for-byte.

Nothing in that directory is part of the framework either. The example image
derives from the production image and adds only its own consumer material; the
supported container surface stays the one [SUPPORT.md](../SUPPORT.md) names.

[proofs/acc-fmi/INSTALL.md](../proofs/acc-fmi/INSTALL.md) covers units, time
conventions, the default Channel Latency, the model's limitations, the FMI 3.0
profile this evidence establishes with the capabilities it rejects, and how to
read a reference mismatch or an unsupported FMU.

## ADAS reference FMU

[proofs/adas-fmu/](../proofs/adas-fmu/README.md) exports the
[ADAS reference application](adas-reference.md#fmi-30-export) as an FMI 3.0
Co-Simulation FMU and qualifies it without SiL's importer: archive
reproducibility and a pinned digest on Linux x86-64, an interface audit, and an FMPy run against
independently enumerated trajectories. It is the reference artifact for the
importer's type and array support, not a supplier FMU.

```sh
proofs/adas-fmu/run-proof.sh            # needs docker and network
```

[proofs/adas-equivalence/](../proofs/adas-equivalence/README.md) runs that
archive and the native library through installed SiL against one declared
experiment: each form against the enumerated trajectories, an FMPy
execution and the other form, with negative controls at predicted
divergences and failures with their exit codes.

```sh
proofs/adas-equivalence/run-proof.sh    # needs docker and network
```

[proofs/adas-closed-loop/](../proofs/adas-closed-loop/README.md) closes a
loop with the same controller, in both forms, over the qualified ACC plant
FMU: edge Participants derive processed radar, camera and ego observations
from plant truth, and an edge conversion applies the Command to the plant.
Each form is compared with an independent FMPy execution and with the other
form, against KPIs and an effect envelope declared before the Runs.

```sh
proofs/adas-closed-loop/run-proof.sh    # needs docker and network
```

[proofs/adas-cost/](../proofs/adas-cost/README.md) measures one declared
workload of the same controller through the Native participant, a
process-isolated C-library adapter and the FMI Importer. Every form must
publish the same Commands, and the instrumented Runs must write the same
Recording bytes as the production Runs. Its timings are observational and
its retained results are a reference baseline, not a capacity claim.

```sh
proofs/adas-cost/run-proof.sh           # needs docker and network
```

## Regression bundles

`sil bundle` packages an adopter's own regression in the same way. The
target is a shared library, one FMU or several coupled FMUs. The bundle
holds the authored Manifests, the targets, the Recordings and their
conversion inputs, the prepared references and the comparison contracts.
A declaration, `bundle.json`, names every bundle file. It also names each
executable, Python module and file that the Runs need outside the bundle,
their whole environment, and the tools that must be absent.

```sh
sil bundle seal /bundles/library                        # in the runtime, once
sil bundle run /bundles/library -o evidence/library     # offline
```

`run` verifies every sealed identity before the first Run, then writes the
Recordings, provenance, logs, determinism checks, comparison reports and a
shareable `summary.json` into the evidence directory. The bundle stays
unchanged. A Manifest hash alone does not close a Run's dependencies; the
lock closes what the declaration names, and nothing more.
[docs/regression-bundles.md](regression-bundles.md) covers the
declaration, the three examples in
[examples/bundle/prepare.py](../examples/bundle/prepare.py) and the failure
cases.

`sil bundle matrix` runs a list of sealed bundles as one CI job. Each case names a
bundle, the lock digest `seal` printed, and a whole-case wall-clock guard:

```sh
sil bundle matrix cases.json -o matrix --jobs 4        # complete matrix
sil bundle matrix cases.json -o matrix --fail-fast     # stop starting cases at a failure
```

Every case gets its own evidence directory and one status: `pass`,
`behavioral-failure`, `manifest-error`, `determinism-violation`, `timeout` or
`skipped`. The command writes `summary.json`, a JUnit `junit.xml` and a
readable summary, and exits 1 when a required case does not pass.
[docs/regression-bundles.md](regression-bundles.md#regression-matrix)
covers the case list and the cleanup of timed-out cases.

## FMI-LS-BUS CAN acceptance fixture

[proofs/fmi-ls-bus/](../proofs/fmi-ls-bus/) pins the Modelica Association's
FMI-LS-BUS CAN demo FMUs, builds them for the supported machine class from
sources upstream ships no binary for, and publishes the profile an importer
has to meet to drive them: Event Mode, a triggered output Clock, and a Binary
buffer of CAN operations. A reference exchange drives the node through an
independent FMI 3.0 importer and matches payloads and event times written down
before the Run; the released SiL Importer is then pointed at the same FMU, and
what it answers is kept verbatim.

```sh
proofs/fmi-ls-bus/run-proof.sh        # needs docker and network
```

Most of it is an evidence gate rather than an implementation: the *released*
Importer cannot drive this FMU, and the two reasons it cannot are the retained
measurement the CAN milestone is built against. The last two steps are the other
way round — the same released runner with the checkout's `sil` package ahead of
it, driving the pinned node on both of the fixture's step grids, and then
connecting two instances of that node through the pinned **bus simulation FMU**
in one Run. Both judge the Recording against an expected exchange written from
upstream's sources beforehand, event times included: a frame offered at 300 ms
is confirmed to its sender and delivered to its peer at 300.48 ms, and the
frame that lost arbitration follows at 300.96 ms.

## Public shared-library acceptance

[proofs/libsafety/](../proofs/libsafety/) replays a public vehicle CAN recording
into a public shared library. It uses the supported workflow: `sil recording csv`,
`sil recording window`, a Process participant adapter over the library's own C API, and
`sil compare`. The library is opendbc's safety logic. The recording is one
commaCarSegments segment. [proofs/public-workloads/](../proofs/public-workloads/)
pins both, and an independent reference. All 6000 observations match exactly,
and two Runs are byte-identical. A timing, a time-unit, a calibration, a crash
and a hang control each fail for their own reason.

```sh
proofs/public-workloads/run-proof.sh   # the pinned bundle; needs docker and network
proofs/libsafety/run-proof.sh build/public-workloads/bundle
```

## Public single-FMU acceptance

[proofs/openacc-controller/](../proofs/openacc-controller/) replays a public
recorded car-following window into one FMU with the supported workflow:
`sil recording csv`, `sil fmi replay` and `sil compare` against an independent FMPy
execution. The recording is a 50 s JRC OpenACC window. The FMU is
`AccController`, a PythonFMU3 export written in this repository, not a
third-party model. [proofs/public-workloads/](../proofs/public-workloads/) pins
both, and the reference. All 501 commands agree within 1e-10, the final one
at 50.1 s included, and two Runs are byte-identical. A changed-input, a
wrong-binding and a one-period-shift control each fail where the control law
predicts before the Run.

```sh
proofs/public-workloads/run-proof.sh   # the pinned bundle; needs docker and network
proofs/openacc-controller/run-proof.sh build/public-workloads/bundle
```

## First-party CAN bus model

[models/can/](../models/can/) builds a standalone C++20 FMI 3.0 CAN Bus Simulation
FMU for a restricted FMI-LS-BUS 1.0.0 profile. It declares four terminals,
configures one to four as active, and carries 11-bit Classical CAN data frames.
Queued requests arbitrate by identifier after each intermission; per-node FIFO
capacity and buffer or discard behavior are configurable. A transmitting frame
cannot be preempted. Completion and arbitration instants reach the Importer as
countdown Clock intervals in whole nanoseconds. The model answers a corrupt
operation with the standard Format Error. An unsupported one fails the instance. Queues,
reports and same-instant events are bounded per instance. `models/can/run.sh`
builds and qualifies the same Linux x86-64 artifact through independent FMI
calls, sanitized checks of its C entry points and SiL's existing FMU group.
It is released as `SilCanBus` 1.0.0 with published digests and build
identities; [models/can/RELEASE.md](../models/can/RELEASE.md) states the supported
platform, compatibility policy, licensing and limits, and
[models/can/example/](../models/can/example/README.md) configures a Run without
source edits. `models/can/bundle.sh` validates that example from a clean
installed SiL bundle against an independent FMPy path.

## Installed processed-sensor regression matrix

[proofs/adas-matrix](../proofs/adas-matrix/README.md) packages the #226 replay
and #227 mixed-loop experiments as sealed Regression bundles. The existing
`sil bundle matrix` runs four nominal cases plus required failing controls from a
clean installed Linux x86-64 runtime without network or source-tree imports.
It archives JSON, JUnit, Recordings, provenance, Determinism and comparisons.
Its acceptance driver checks the selected failures' exit codes, diagnostics
and cleanup, including whole-case containment of native hangs and crashes.
