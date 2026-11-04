# Collector investigation dispositions for #354

Delivery A uses retained evidence. Its delivery does not qualify another
collector. The decisions below explicitly defer the Delivery B work tracked
by [#354](https://github.com/Stevie1704/sil/issues/354); the original deferrals
were not failed experiments or evidence that a candidate cannot work. The #361 entry below
was subsequently revisited with a Linux capture-window experiment, and the
#360 entry now records a gated build decision with its contract; the other
entries retain their original evidence and scope.

The existing [#321](https://github.com/Stevie1704/sil/issues/321) and
[#340](https://github.com/Stevie1704/sil/issues/340) retain their ownership,
acceptance and open investigation scope. Their deferral from this delivery
does not resolve their original parent specifications. A future build
decision requires a separate production specification and implementation
tickets. ADR 0004, ordinary `sil-run`, the Manifest, Participant ABI and Step
protocol remain the governing boundaries.

## Allocation activity — #340: defer

The [Heaptrack/Clock-shim proof](../proofs/heaptrack-shim/README.md) and
[availability results](../proofs/heaptrack-shim/availability-notes.md)
are the evidence for this decision. Clock-shimmed Process Heaptrack remains
rejected. Its coarse process-relative ticks cannot be calibrated into exact
Activation observations. The privileged bpftrace smoke probe counted 50,000
malloc entries but did not resolve the required stack frame; the ordinary
user and default-capability container paths were unavailable. That result
does not qualify allocation lifetimes, arbitrary allocators, descendants,
event history or an Activation join.

The synchronous Native runner counter candidate has not been tested. Defer
its proof, Process/shim composition and a new eBPF collector. Current offline
allocation-site views answer where captured allocations originated without
requiring any of those unqualified measurements. They provide no ownership
or exclusive per-Participant/FMU memory account.

Reopen this delivery's allocation work when a named Native Task needs
allocation activity per Activation and a Linux x86-64/glibc qualification
runner is allocated. Start with #340's bounded proof: independently authored
malloc/calloc/realloc/free/aligned-allocation sequences; failed/zero-size
and overflow cases; equal-time Tasks; lifecycle/background/custom-pool
exclusion; observer reentrancy and self-allocation exclusion; failed/unclosed
intervals and forced loss. Requested bytes are distinct from successful
allocation bytes. Free-call counts supply no freed-byte estimate without a
separately qualified bounded lifetime tracker. Retain actual allocator/build
coverage, buffers, loss, overhead and byte-identical ordinary/observed
Recordings. Child allocations require their own observation mechanism.

## Source scopes — #360: build, gated by the accepted contract

The [#320 proof](../proofs/function-tracing/README.md) established the
narrow benefit: exact counts and entry order of source-owned logical Native C
calls, including inlined bodies. Its explicit captures each retained 2,640
events with 40 completed Activation joins. The first paired Native
Activation medians were 1,951.5/5,645.5 ns and 7,160.5/50,795.5 ns for
diagnostics/explicit scopes on the one/eight-object cases. Those observations
show substantial local perturbation; no constant overhead can be subtracted.
The loaded-library XRay 18 approach remains deferred.

**Build, gated.** The [source-scope contract](proposals/native-source-scopes.md)
and [ADR 0007](adr/0007-source-owned-native-scope-observer.md) replace the
earlier deferral. They freeze the source API, the opt-in `sil profile
--collector scopes` lifecycle, separately versioned raw/normalized files with
[worked fixtures](../tests/fixtures/source_scopes/), the join, the
Run/capture outcomes and the resource budgets. The named benefit is exact
per-Activation call counts and order, which sampled stacks cannot give, first
for the ADAS reference's object search. The constraints stay: one
source-owned Native C library, synchronous runner-thread calls, ordinary C
returns, Linux x86-64/glibc, GCC 12 and durations of the instrumented build
only.

No supported interface ships with this decision. Separate API/lifecycle,
decoding and presentation tickets implement the contract; each ships only
after its rows of the contract's qualification matrix pass. Process/FMU/
prebuilt-library/XRay coverage and CPU-duration/target/WCET claims remain
excluded.

## Late capture windows — #361: defer production after qualification

The [capture-window proof](../proofs/capture-windows/README.md) now performs
this investigation. Its [retained evidence and decision](../proofs/capture-windows/evidence/README.md)
supersede the earlier evidence-free deferral. It uses an optimized Native
late-spike workload at Activation 610, a Process variant, equal-time Native
Tasks, independently retained workload markers and actual perf control.

**Long-window feasibility is positive; production adoption remains deferred.**
On the named workload, bounded files are approximately 94% smaller for Native
work and about 89% smaller for Process work than whole-Run capture. Setup exec/DSO
metadata and post-disable lifetime records survive named CPU controls. The
proof retains requested Virtual time separately from notification, command,
acknowledgment, workload and CPU-sample host timestamps. An acknowledgment identifies no
exact effective collection edge. The short equal-time peer selection retains
only one or two samples, including a sample outside the requested Task on
the native host. This is no exact per-Activation isolation guarantee.

Failure can terminate a Run before its requested Virtual-time end. That end
is reported as unreached; shutdown requests disable separately, with any
missing acknowledgment or effective end left unknown. Disabled request regions
and samples observed during their uncertain transitions are retained separately
from loss/truncation. No Virtual-time range is treated as a host deadline.

The active storage-reserve case preserves the Run and its Recording. A
competing writer that consumes the reserve between guard polls can instead
cause perf to exit 255, send SIGTERM to the Run, and leave truncated capture
data; the measured Run reports exit 1. This negative result is distinct from
intentional disable, empty/unreached selection, observer backpressure,
PERF_RECORD_LOST and an unclosed hard-killed Activation. Generous-deadline
ordinary/observed successful fixtures and deliberate Participant failures
retain equal outcomes and Recordings. Storage fault injection does not.

Defer a production windowed collector until a source owner accepts its
asynchronous boundaries and a reviewed contract resolves both of these:

- A separate versioned coverage envelope: exact requested `[from,to)` and
  optional Participant/Task/index identity; host clock/process lifetime;
  notification/control/ack observations; first/last samples; deliberate
  disabled regions; empty, unreached, failed/unclosed and unknown transitions;
  independent observation/perf loss, capture-limit, reserve, truncation and
  finalization states. Unknown effective boundaries stay unknown. Never
  reinterpret an existing v1 coverage field or infer continuous coverage,
  exclusive Process CPU, exact Activation isolation or idle time from samples.
- Collector lifetime ownership that preserves the Run's functional outcome
  when capture storage fails. Requalify actual ENOSPC with independently
  surviving Run supervision, existing guards/reserve, bounded shutdown and
  explicit partial output. A reserve is not a guarantee against other writers.
  The proof's perf-owned workload launch does not satisfy this requirement.

Reopen with a named adopter storage/processing budget, an allocated Linux
native/container qualification environment, and those coverage/lifetime
contracts. Then create separate production specification and implementation
tickets, retain repeated perturbation measurements, and qualify permission,
loss, failure and metadata behavior again. No production CLI, ordinary-run
hook, scheduler wait, deadline relaxation, trigger language, ring buffer or
heap windowing is authorized by this experiment.

## Scheduler/blocking evidence — #362: defer production after qualification

The [bounded scheduler experiment](../proofs/host-scheduling/README.md) adds
independent fixed CPU, sleep, synchronized pipe-wait and same-CPU competitor
phases to two Process participants at equal Virtual times. It uses direct
perf scheduler events, actual diagnostics-runner intervals and direct-syscall
host-clock markers. The [retained evidence](../proofs/host-scheduling/evidence/README.md)
pins native/container observations, privilege contexts, build/tool identities,
file growth, observer cost and outcome/Recording equality. The #330 negative
permission and stack evidence is reused, without treating its allocator
probe as scheduler qualification or adding another eBPF dependency.

Switch/wakeup events distinguish observed running, runnable and sleeping
intervals for the named fixture. Initial/final unknown states, missing
transitions, loss, truncation and absent failed/unclosed Step intervals remain
explicit. Thread/child overlaps are process-window associations, never
exclusive Step work. Sleeping alone proves no lock or IPC cause. Elapsed
time minus sampled CPU weight is still invalid; this experiment collects no
CPU samples. Normal generous-deadline equality does not establish inertness
at a deadline edge; the separate 180 ms cases retain their own observations.
No sysctl or host mount changes follow permission failures.

**Defer production.** System-wide events, initial-PID-namespace mapping,
environment-specific privileges/tracefs access, finite capture with incomplete
edges and conservative loss handling are a narrow feasibility result. A
source owner still needs a named slow-Step question that justifies those
costs over existing diagnostics/sampling. Namespace-changing/detached workers
and nonleader exec remain unqualified. Scheduler states alone cannot supply a
general blocking-cause diagnosis.

Reopen with that adopter workload, a permitted native/container deployment,
measured capture/storage and perturbation budgets, and an accepted mapping
and privacy scope. Before a build decision, review a **separate versioned
state/coverage schema**: clock and process/TID lifetimes, namespace lineage,
exec replacement, actual half-open Step intervals, thread/child association,
initial/final unknown states, loss boundaries, storage limits, interruption,
collector-vs-Run outcomes and failed/unclosed observations. Create separate
implementation tickets after that contract. Do not reinterpret portable v1
profile/correlation fields or add ordinary-run hooks, live control, a
wall-minus-sample estimate, target RTOS/ECU scheduling or WCET claims.

## Internal state — #321: retain the existing GDB investigation

The [ADAS reference](adas-reference.md) has the named hazard maneuver and
optimized source owned by #321. Source debugging remains the route to held
inputs, selected objects, decisions and limited commands. Neither a
Recording nor a profile stores historical variables. Defer this work from
#354 without changing #321's acceptance, labels or #313 ownership.

Reopen the #354 internal-state branch when the hazard sequence-5/6 decision
needs inspection and a pinned GDB Linux runner is available. Execute #321's
existing Native/Process/FMU matrix, index/Virtual-time stops, matching separate
debug identities, optimized-out states, explicit deadline policy,
attach/launch permissions, shim clocks, signals/resume/detach/failure/cleanup
and Recording equality. Launch scripts come first; any supported hook needs
its own evidence and specification. No duplicate debugging ticket, live Run
control, reverse execution or checkpoint promise is introduced here.
