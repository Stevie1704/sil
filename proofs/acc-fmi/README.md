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

The [curated evidence](evidence/README.md) comes from
[Actions run 35705741334](https://github.com/Stevie1704/sil/actions/runs/35705741334)
on native Linux x86-64. Both archives passed the pinned schema and both call
paths. All 60 SiL output samples matched the independent expectations; both
Recordings per Manifest and both clean builds per FMU were byte-identical.

One authoritative FMU pair lives under `tests/fixtures/pythonfmu3/`, shared by
this evidence and the ordinary-suite resource regression tests. Compact CSV
traces retain initialization, communication points and final termination.
Manifests, Recordings, provenance, both build/Run hashes and runtime identities
remain directly verifiable. Full raw JSON and routine logs stay in CI artifacts;
older development snapshots remain in Git history. See the evidence index for
column meanings and the retention policy.

Evidence gates raise explicitly, including under `python -O`. XML normalization
splices only the root token and optional generation date, preserving all other
bytes; tests cover namespace prefixes, comments and a missing date.
`SIL_ACC_PARTICIPANT_TIMEOUT_MS` can increase the response deadline for slower
hosts without changing the Manifest. The workflow uploads the complete raw
outputs and a separate Manifest/Recording artifact on every qualification Run.

## Closed-loop trajectory gate (#148)

```sh
SIL_ACC_PROOF=closed_loop proofs/acc-fmi/run-proof.sh build/acc-loop-evidence
```

This separate evidence directory and CI job preserve the open-loop baseline.
The same two qualified FMUs run as ordinary Process participants, with explicit
scalar bindings, 10 ms periods, 10 ms Channel Latency and finite capacity-two
Subscriber routes. Sensing contains gap, relative speed and ego speed; a second
plant output Channel records both positions and lead speed. Commands contain
acceleration. XML names, causalities, scalar types, SI units and input starts
are checked before execution; the existing Importer validates every binding.

Let x[n] be plant state at FMI communication time n*h, c[n] the controller
output from call n, h=0.01 s. The complete interval exchange table is:

| Activation / interval | Controller samples | Plant holds | Published sensing | Published command |
| --- | --- | --- | --- | --- |
| Initialization (not a Recording Slot) | x[0]=(ego 0 m,25 m/s; lead 60 m,25 m/s) | 0 m/s² | none; getters give x[0] | getter gives 1.5 m/s², not routed |
| n=0, [0,h] | initialized sensing x[0] | 0 m/s² | x[1] | c[0]=law(x[0]) |
| n>=1, [n*h,(n+1)*h] | x[n] from publication (n-1)*h | c[n-1] | x[n+1] | c[n]=law(x[n]) |

All inputs remain constant within each interval. Publication time is n*h;
post-step plant communication time is (n+1)*h. The command is sampled at n*h,
even though its FMU independent time also advances to (n+1)*h. Initialization
outputs are retained by the independent driver. The last publication at 4.99 s
contains plant state at 5 s; the post-hoc minimum-gap KPI includes this final
sample, with a declared threshold of 5 m. The maneuver closes an initial 60 m
gap toward the headway target, transitioning out of acceleration saturation.

`loop_reference.py` uses only FMPy's explicit lifecycle and scalar API. It
snapshots both inputs before stepping either FMU and imports no SiL code,
production equations or comparison implementation. Comparison is numeric at
common communication points, with per-signal absolute 1e-10 SI and relative
1e-12 tolerances declared before execution. Nonfinite values, missing/duplicate
outputs, wrong timestamps and sample counts fail. The first numeric mismatch
names the signal, publication instant and communication instant.

Three independently executed negative controls delay commands one Step, delay
sensing one Step, or change the initial held acceleration to 0.5 m/s². Each must
fail comparison. Command delay must change motion; sensing delay must change
subsequent commands. Each successful SiL Manifest runs twice and requires exact
Recording byte equality; different engines are never compared by MCAP bytes.

The original Python example is also executed unchanged, twice. Its reference
uses a separately declared schedule: plant publishes x[n] before integration;
controller publishes nothing at n=0, then c[n]=law(x[n-1]); plant applies zero
for n=0 and n=1, then c[n-1]. Its publication time equals sensing communication
time. It therefore does **not** have the same closed-loop trajectory as the
post-step FMUs. The `original` reference mode matches that causal schedule and
compares at those common points, without translating or offsetting traces.
Its command comparison starts at h, exactly where its first command exists.

Evidence includes exact Manifests, both Recordings and provenance, FMU archives
and identities, schema/symbol/dependency audits, image and machine identities,
all configuration/tolerances, FMPy initialization and per-interval sampled and
applied values, final states, numeric verdicts and negative-control diagnostics.
The `acc-loop-evidence` CI artifact retains this independently of #147 evidence.
No kernel, Importer, ABI, protocol, or existing example behavior changes.

[Retained closed-loop evidence](closed-loop-evidence/README.md) records a native
Linux pass: 500 matched communication points, minimum gap 49.96364767075116 m,
byte-identical repeat Runs, all three negative controls detected, and both
invalid-binding cases rejected with exit 2.
