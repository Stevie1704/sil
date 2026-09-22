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
| `AccPlant` | commanded ego acceleration (m/s²), lead acceleration (m/s²), initial lead position (m) | ego and lead positions (m), ego and lead speeds (m/s), gap (m), relative speed (m/s) |

Controller: desired gap = `5 m + 1.5 s × ego_speed`.
Raw acceleration = `0.35 s⁻² × (gap − desired_gap) + 1.2 s⁻¹ × relative_speed`.
Command = `max(−3, min(1.5, raw)) m/s²`. Initial inputs are `(60, 0, 25)`;
initialization evaluates the same law, yielding `1.5 m/s²`. Input writes in Step
Mode leave the previous command held until `doStep` samples the new inputs.

Plant: `x(t+h) = x(t) + v(t)h + a h²/2`, `v(t+h) = v(t) + a h`.
Initially ego `(x,v)=(0 m,25 m/s)` and lead `(60 m,25 m/s)`; lead acceleration
defaults to zero and ego command defaults to zero. The sensitivity proof can
author lead acceleration and initial lead position as inputs without changing
the default qualification behavior. The plant applies supplied accelerations
without a second clamp, speed floor or collision model.

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

The two qualified FMUs run as ordinary Process participants. Explicit scalar
bindings connect sensing (gap, relative speed, ego speed) and command
(acceleration). A separate plant output Channel carries both positions and lead
speed. A Test participant checks the minimum-gap KPI during the Run.

The authored grid is 500 intervals of 10 ms. Every Participant's Step period and
Manifest Duration are checked against it, and every Subscriber route must have a
positive integer capacity and overflow policy `fail`. The independent driver
reads only the authored numeric grid from `configuration.json`, uses it for all
FMI calls, and returns a grid receipt and integer interval boundaries. The gate
checks that receipt, every boundary and all Recording timestamps/counts. XML
names, causalities, Float64 types, SI units and input starts are checked too.
These FMUs declare a fixed communication-step profile, no internal Clock and no
DefaultExperiment rate; there is no hidden FMU sample period to infer. The
positive constant driver period supplies their period. Unknown or
wrong-causality bindings
are tested in actual SiL Runs and must fail with exit 2.

### Exchange table

Let x[n] be plant state at FMI communication time n*h, c[n] the controller
output from call n, h=0.01 s. Nominal finite Subscriber route capacity is two.

| Activation / interval | Controller samples | Plant holds | Sensing publication | Command publication | Channel Latency (sensing / command) |
| --- | --- | --- | --- | --- | --- |
| Initialization | x[0]=(ego 0 m,25 m/s; lead 60 m,25 m/s) | 0 m/s² | none; getter x[0] | none; getter 1.5 m/s² | h / h; no initialization Message |
| n=0, [0,h] | initialized sensing x[0] | 0 m/s² | x[1] | c[0]=law(x[0]) | h / h |
| n>=1, [n*h,(n+1)*h] | x[n] from publication (n-1)*h | c[n-1] | x[n+1] | c[n]=law(x[n]) | h / h |

Inputs remain held within each interval. Both Messages publish at n*h. Plant
outputs describe (n+1)*h; the controller samples at n*h even though its FMI
independent time also advances to (n+1)*h. Initialization getters are retained
and asserted against explicit initial outputs before trajectory comparison.
The maneuver closes an initial 60 m gap toward the headway target, transitioning
out of acceleration saturation.

### Independent comparison and negative controls

`loop_reference.py` uses only FMPy's explicit lifecycle and scalar API. It imports
no SiL code, production equations, routing or comparison implementation. Its
coupling variants independently specify state age, command age, initial command
and publication schedule. Each SiL variant matches its own FMPy trajectory at
common communication points; comparison across engines is numeric, never by
MCAP bytes. Per-field tolerances are explicitly declared before execution:
absolute 1e-10 SI and relative 1e-12. Equal budgets are intentional for identical
Float64 artifacts in one runtime, allowing rounding far below the observed
one-Step differences; they do not imply model-fidelity accuracy. Nonfinite
values, missing/duplicate Messages, wrong timestamps and counts fail. The first
numeric mismatch names the field, publication time and communication time.

All three perturbations now execute through **SiL as well as FMPy**:

| Variant | Authored change | Required consequence in SiL Recordings |
| --- | --- | --- |
| `shift-command` | Command Latency 2h, capacity three | plant trajectory changes; nominal comparison fails |
| `shift-sensing` | Sensing Latency 2h, capacity three | subsequent commands change; nominal comparison fails |
| `initial-command` | plant's initial held acceleration 0.5 m/s² | nominal comparison fails |

Capacities of three allow the extra queued Message in delayed variants. A
separate negative Run uses sensing Latency 2h and capacity two: with the plant
publishing before the controller drains, the third queued Message must cause
exit 1 with a capacity diagnostic. This proves the finite bound is enforced.

Saturation masks the sensing delay initially: the first command difference is
at publication 1.56 s, after 156 identical points. The proof claims detection
over the full maneuver, not immediate detection while the controller remains
saturated. First-divergence instants are retained in `results.json`.

Every successful Manifest is constructed independently twice and its exact
serialized bytes are compared. Every such Manifest is also executed twice and
its Recordings must match byte-for-byte. These are separate gates.

### KPI coverage and original Python schedule

The in-run KPI has a 5 m threshold, rejects nonfinite gaps, and must report
499 checked sensing Messages through publication 4.98 s. Its one-Step Latency
prevents delivery of the final Message before Run completion. The post-hoc KPI
checks all 500 sensing Messages, including publication 4.99 s describing plant
state at 5 s. A separate Run raises the in-run threshold to 61 m and must fail
with exit 1, proving the KPI participates in deciding the Run's result.

The original Python example runs unchanged and is checked separately. It
publishes x[n] before integration; its controller publishes nothing at n=0,
then c[n]=law(x[n-1]); the plant holds zero for n=0 and n=1, then c[n-1]. Its
sensing publication time equals its communication time. This is a different
causal schedule from the post-step FMUs and therefore a different trajectory.

The independent `original` coupling follows that schedule. It retains the real
FMU controller output at every call, including 1.5 m/s² at zero. A separate
published-command field is **null** at zero, representing no publication, so
the initial plant input remains held. The comparator requires the command
Message to be absent there. No FMU output is overwritten with zero, and no
trajectory is shifted to make it match.

### Reproducible evidence

`closed_loop.py` calls the committed `loop_evidence.py` producer automatically.
The clean command above creates full raw evidence and a compact `curated/`
report containing both FMU identities, environment/configuration, all verdicts,
Manifest/Recording determinism hashes, and digests identifying the raw evidence.
The image identity is added by the host script. Only this report and its index
are checked in; full FMUs, Manifests, Recordings, initialization and trajectory
JSON, and failure logs stay in the CI artifact. They remain available to download
while that artifact is retained; the command rebuilds the proof independently.
To regenerate the compact report from a downloaded raw artifact:

```sh
python proofs/acc-fmi/loop_evidence.py build/acc-loop-evidence build/acc-loop-report
```

The CI workflow uses `SIL_ACC_PROOF=all` to build one pinned image and execute
the qualification, closed-loop, and sensitivity proofs in separate containers.
It retains separate `acc-fmi-evidence`, `acc-loop-evidence`, and
`acc-sensitivity-evidence` artifacts plus an additional Manifest/Recording
artifact covering all proofs. The old open-loop baseline remains unchanged. No
kernel, Importer, ABI, protocol or original example behavior changes.

[Retained closed-loop evidence](closed-loop-evidence/README.md) identifies the
native execution snapshot; rebuilding produces a new identified snapshot.

## Communication-period sensitivity (#149)

Run the experiment in the same pinned image with:

```sh
SIL_ACC_PROOF=sensitivity proofs/acc-fmi/run-proof.sh build/acc-sensitivity-evidence
```

`sensitivity.py` writes `configuration.json` before starting either the
independent driver or a SiL Run. It records the equations, exchange algorithm,
sample/hold rules, initial state, Duration, exact endpoint observation grid,
every participant Period, every Channel Latency, the Maneuver and the zero
modeled physical delay. The plant is integrated as
`x(t+h)=x(t)+v(t)h+a*h*h/2`, `v(t+h)=v(t)+a*h`; the controller is the fixed
clamped law already qualified in this directory. A Message contains the
post-step output, is timestamped at its activation Slot, and is compared at
the endpoint time `publication + participant Period`.

The authored matrix is deliberately split:

| Family | Rows | What changes | Reference |
| --- | --- | --- | --- |
| Constant sanity | constant acceleration at 20/10/5 ms | plant fixed-period integration against a closed form | analytic oracle; expected to be exact |
| Plant forcing | piecewise-constant lead acceleration at 20/10/5 ms | held-input plant refinement with a changing Maneuver | continuous piecewise-input analytic oracle |
| Closed-loop plant refinement | plant at 20/10/5 ms, controller and Latencies fixed at 10 ms | plant Period only | independent 1 ms-plant FMPy Run |
| Controller sampling | controller at 20/10/5 ms, plant and Latencies fixed at 10 ms | controller sampling/hold | independent 1 ms-controller FMPy Run |
| Channel Latency | Latency 0/10/20 ms, both participant periods fixed at 10 ms | end-to-end Channel Latency only | independent finer FMPy Run; KPI delta also against the independent 10 ms baseline |
| Combined sensitivity | closed loop at 20/10/5 ms | plant Period, controller sampling, and both Latencies | independent 1 ms combined FMPy Run |
| Negative control | 10 ms loop with a deliberately incorrect 250 ms sensing Latency | timing defect | independent finer FMPy Run; envelope check against the 10 ms baseline |

The constant-oracle rows are a sanity check, not the sensitivity signal: exact
constant-acceleration integration telescopes over every Period. The changing
lead Maneuver has transitions deliberately off the 5/10/20 ms grids, so the
plant-forcing rows measure held-input discretization against a continuous
piecewise-input oracle. The closed-loop plant-refinement rows hold controller
sampling and modeled physical Latencies fixed, which is the separation the
experiment is intended to establish. The qualified FMUs advertise
`canHandleVariableCommunicationStepSize=false`, so each row is a separate
constant-Period Run. The finer 1 ms comparisons are independent fixed-Period
FMPy Runs, not a claim of variable-step FMI support.

Each measured row authors its Manifest twice and runs that same Manifest twice;
only those same-row Recordings are compared byte-for-byte. Recordings from
different step sizes are never compared for byte identity. The report compares
native endpoint samples to exact reference times and rejects missing samples
instead of interpolating across them. It reports maximum gap, speed, position,
and command errors, minimum-gap and other KPI changes, Manifest/Recording and
FMPy-reference identities, a readable Markdown table and two SVG plots in
addition to `sensitivity-report.json`. The declared Sensitivity envelope is
authored before any reference or Run, and is an observation bound rather than a
monotonic-convergence assertion. The negative timing row must exceed it.
The compact report, plots and plot data are retained under `curated/` so the
evidence path is directly reviewable.
