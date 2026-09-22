# ACC FMI 3.0 interoperability proof

[Issue #147](https://github.com/Stevie1704/sil/issues/147): two source-available
Co-Simulation FMUs from the existing simplified ACC example, built by
PythonFMU3 0.3.4 and driven through SiL and FMPy 0.3.26. This is an
interoperability fixture, not a production ADAS model or safety qualification.

## Model contract

The original Process participants remain consumers of the same pure functions
in `python/src/sil/examples/acc/dynamics.py`. The FMUs bundle that file directly;
they do not import SiL. No controller gain, clamp, plant integration operation,
initial condition, or original example scheduling behavior changes.

Positive position and speed point forward along one longitudinal axis. Gap is
lead position minus ego position; relative speed is lead speed minus ego speed
(positive means the lead is pulling away). All FMI interface variables are
Float64 scalars. Units are declared in `modelDescription.xml`.

| FMU | Inputs | Outputs |
| --- | --- | --- |
| `AccController` | gap (m), relative speed (m/s), ego speed (m/s) | commanded acceleration (m/s²) |
| `AccPlant` | commanded ego acceleration (m/s²) | ego and lead positions (m), ego and lead speeds (m/s), gap (m), relative speed (m/s) |

Controller: desired gap = `5 m + 1.5 s × ego_speed`.
Raw acceleration = `0.35 s⁻² × (gap − desired_gap) + 1.2 s⁻¹ × relative_speed`.
Command = `max(−3, min(1.5, raw)) m/s²`. Initial inputs are `(60, 0, 25)`;
initialization evaluates the same law, yielding `1.5 m/s²`. Input writes in Step
Mode leave the previous command held until `doStep` samples the new inputs.

Plant: `x(t+h) = x(t) + v(t)h + a h²/2`, `v(t+h) = v(t) + a h`.
Initially ego `(x,v)=(0 m,25 m/s)` and lead `(60 m,25 m/s)`; lead acceleration
is always zero and ego command defaults to zero. The plant applies the supplied
acceleration without a second clamp, speed floor or collision model. Negative
speed is mathematically possible; these one-second cases never reach it.

Inputs are held constant throughout each communication interval and persist
until explicitly replaced. All Runs use ten fixed 0.1 s intervals covering
`[0,1] s`. Initialization exposes the state at `t=0`, before integration.
Plant getters before `doStep(t,h)` describe `t`; after it they describe `t+h`.
Controller getters after that call carry the command sampled at `t`, held for
that interval. The independent time variable advances to `t+h`.

The existing SiL Importer publishes post-step values: Recording Slot `t`
contains the FMU output reached at `t+0.1 s`. Thus the first Slot, zero, already
contains the plant state at 0.1 s; the last Slot, 0.9 s, contains the state at
1.0 s. There is no activation at Duration. A separate trace uses the Importer's own lifecycle and scalar accessors,
retaining initialization, all steps, and successful termination at 1.0 s. This
deliberately does not change the existing ACC Process plant's publish-before-integrate contract.
Stimulus Channels use explicit zero Latency and earlier priority, so updates at
`t` are used for `[t,t+0.1]`; quiet Slots test input hold.

## Pinned artifacts and independent path

This proof deliberately builds SiL from the checkout, unlike the esmini and
FMI-LS-BUS released-runtime proofs. It qualifies the unreleased `resource_path`
correction, which a published runner image does not contain. `SOURCE_REVISION`
identifies the checkout in the native runner, FMUs and environment evidence.
This proves checkout interoperability; it does not claim released-runtime
adoption. A release-based proof can follow once that correction is published.

The Dockerfile pins the CPython 3.13.7 Debian bookworm image by digest and freezes
the Debian index to `20260901T000000Z`. The hashed dependency lock pins exporter,
FMPy, and their complete Python dependency closure. Each FMU includes model
source, shared dynamics, source revision and source hashes, runtime requirements,
and the SiL and PythonFMU3 licenses. Archive hashes, machine class, actual FMI
symbols, declared capabilities, schema hashes and linked libraries are retained.
Only Linux x86-64 / glibc 2.36 is qualified, even if the exporter includes other
platform binaries.

**Python is supplied by the execution image.** PythonFMU3 packages its Python
support modules in `resources/`; it does not embed the CPython runtime. Both
execution paths in this proof are hosted by the pinned Python interpreter.
`needsExecutionTool=true`, variable communication steps and FMU state operations
are disabled. Symbol presence is audited but is not a claim that every optional
operation works: this proof calls only the scalar Co-Simulation profile.

PythonFMU3's UUID1, XML generation timestamp and ZIP timestamps otherwise change
every build. `build.py` replaces only that identity metadata: a source-bound
token, omitted optional generation date, sorted ZIP members with fixed metadata.
No generated FMI code or behavioral XML is patched. A second clean exporter
invocation must produce byte-identical archives before qualification proceeds.

FMPy was selected instead of the issue's suggested fmusim candidate because its
pinned `FMU3Slave` API permits explicit lifecycle calls, inspection before and
after each step, and two simultaneously live instances of the same library.
`independent.py` imports no SiL module and no production model function. It
validates with FMPy's pinned FMI 3.0 schema tree and then explicitly instantiates,
sets inputs, initializes, steps, reads, terminates and frees both FMUs. The
observed successful profile is the qualification of this tool; general FMI 3.0
support is not treated as evidence. No fmusim support claim is made.

## Checks and evidence

`cases.py` fixes inputs and tolerances before execution: absolute `1e-10` in SI
units and relative `1e-12`; every evaluator rejects nonfinite values. The oracle
in `expected.py` imports neither production functions nor exporter code. It
contains explicit controller expectations (upper/lower saturation, zero,
positive and negative unsaturated commands), and analytic initial-value motion
for constant acceleration `1.5`, `−3`, `0` and `0.5 m/s²`.

Each of three Manifests runs twice and compares entire MCAP bytes separately.
Each contains two concurrent Importer processes with different inputs. FMPy also
interleaves two live instances from one shared library in one process, catching
state sharing that process isolation could mask. Traces retain initialization,
pre-step values, inputs, communication points through the final interval, and
termination. Missing, duplicate, nonfinite or incorrectly timed output fails.

Run from a clean checkout with Docker available:

```sh
proofs/acc-fmi/run-proof.sh [evidence-directory]
```

The default destination is `build/acc-fmi-evidence`. The workflow
`.github/workflows/proof-acc-fmi.yml` runs on native x86-64 Linux and retains all
FMUs, exact Manifests, both Recordings and provenance files, traces, identities,
schema/symbol/dependency audits, and per-Manifest verdicts as an Actions artifact.
`environment.json` identifies the source revision and runner, and `image-id.txt`
identifies the execution image. A rebuilt image is a new execution artifact;
the determinism claim is always between two Runs in that image.

## Importer correction

The external archive demonstrated that SiL passed null for FMI's `resourcePath`.
PythonFMU3 returned no instance with `basic_string: construction from null is
not valid`. FMPy initialized that same archive successfully. Supplying only
the absolute extracted `resources/` path made the SiL lifecycle pass too.

The correction adds an optional resource path to `CoSimulation` and supplies it
from the single and group Importer callers when the directory exists. [FMI 3.0.2 section 2.3.1](https://fmi-standard.org/docs/3.0.2/#resourcePath)
requires an absolute filesystem directory with a trailing separator. This is a
normative API requirement, not a claim that this exporter rejects a path without
the separator; the observed failure concerns a null pointer.
The minimal reproduction is retained in `initialization.py`: omitting
`--resources` demonstrates the null-path failure; adding it demonstrates the
correction. The full proof exercises the actual Process participant wiring.

No Manifest/hash, Step protocol, Arena, Native ABI, Recording or exit-code
contract changes. Existing successful Runs are covered by the full repository
suite and original reference Manifest determinism gate.

## Retained native result

The [committed evidence](evidence/README.md) comes from
[Actions run 35703127743](https://github.com/Stevie1704/sil/actions/runs/35703127743)
on native Linux x86-64. Both archives passed the pinned schema and both call
paths. All 60 SiL output samples matched the independent expectations, and both
Recordings for each of the three Manifests are byte-identical. Both FMU builds
also reproduced their archive bytes. The full identities and original traces
are retained, including the null-resource-path failure and successful fix.

The original FMUs are retained unchanged under `tests/fixtures/pythonfmu3/` and
exercised by `tests/test_fmi_resources.py` in the ordinary test suite. They do
not require an exporter rebuild. Their identities match the original evidence;
new proof builds receive new identities and evidence directories.

Evidence checks use explicit exceptions and remain active with `python -O`.
`rebuild.json` records both build hashes per FMU, and `results.json` records both
Recording filenames, hashes and successful exit codes per Manifest. Lifecycle
traces retain successful termination after the final interval. Empty log files
mean a successful command wrote no console output; they are retained in the new
complete snapshots. The negative resource case records an explicit rejection
JSON instead of relying on the absence of a success file.

XML normalization splices only the root token value and optional generation-date
attribute. It preserves all other XML bytes, including namespace prefixes and
comments; tests cover the missing-date case and quoted values. The script and
workflow retain both a complete evidence artifact and a separate Manifest and
Recording artifact. `SIL_ACC_PARTICIPANT_TIMEOUT_MS` can increase the response
deadline for slower hosts without changing the Manifest.

The [complete post-review native snapshot](evidence-review/README.md) retains
every generated artifact, including both FMUs, successful empty logs, explicit
rebuild comparisons and final termination records. The original snapshot is
preserved separately so improvements to the proof do not rewrite old evidence.
