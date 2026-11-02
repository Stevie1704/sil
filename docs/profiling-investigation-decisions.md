# Collector investigation dispositions for #354

Delivery A uses retained evidence. Its delivery does not qualify another
collector. The decisions below explicitly defer the Delivery B work tracked
by [#354](https://github.com/Stevie1704/sil/issues/354); they are not failed
experiments or evidence that a candidate cannot work. No new Linux collector
experiment was performed for these decisions.

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

## Source scopes — #360: defer production contract adoption

The [#320 proof](../proofs/function-tracing/README.md) already established
the narrow benefit: counts and entry order of source-owned logical Native C
calls, including inlined bodies. Its explicit captures each retained 2,640
events with 40 completed Activation joins. The first paired Native
Activation medians were 1,951.5/5,645.5 ns and 7,160.5/50,795.5 ns for
diagnostics/explicit scopes on the one/eight-object cases. Those observations
show substantial local perturbation; no constant overhead can be subtracted.
The loaded-library XRay 18 approach remains deferred.

Defer adopting a supported source-scope API in this delivery. Reuse the
[existing proposal](../proofs/function-tracing/proposed-spec.md) rather than
repeating its successful proof. Neither the proof nor this decision installs
an API, changes a schema, or authorizes transparent rewriting of user code.
The following concrete design boundary is the starting point for reopening,
not a promised interface:

- One source-owned Native C DSO, synchronous runner-thread work, ordinary C
  returns, Linux x86-64/glibc. An optional, separately installed observer
  header would expose a source-owner-maintained scope guard with a stable
  numeric scope ID. Disabled scopes compile away; enabled scopes call a
  separate observer library. No function is added to `sil_api_v1`.
- An explicitly requested diagnostics capture owns one fresh Run UUID and
  one observer instance. Ordinary `sil-run` neither loads nor starts it.
  Unsupported longjmp, cancellation and C++ unwinding cannot synthesize an
  exit. Recursive entries use per-thread nesting; worker events remain
  unattributed.
- A separate version-1 event envelope would bind Run UUID, process lifetime,
  PID/TID, verified `CLOCK_MONOTONIC`, original/instrumented binary digests,
  build IDs and a digest-bound scope/source map. Entry/return records carry
  sequence, monotonic nanoseconds and scope ID. No v1 profile field changes
  meaning. A map mismatch fails decoding before any output is written.
- Join only uniquely contained complete calls to completed Native intervals
  in `[begin,end)`. Missing exits, failed/unclosed intervals, crossing calls,
  ambiguous joins, lifecycle and unsupported threads retain explicit reasons.
  Count/order completeness is unavailable after loss. Elapsed scope duration
  includes waiting, preemption and observer work; nested durations overlap.
- Begin with the proof's fixed 65,536-record/1 MiB preallocated event budget,
  bounded metadata and a nonblocking append attempt. Exhaustion/contention
  increments loss counters; there is no overwrite, allocation, I/O or retry
  in a scope. Finalization uses fresh exclusive paths, preserves the Run
  outcome independently, and records short writes/missing footer as partial.
  A supervisor bounds shutdown; regular-file I/O has no hard latency promise.

Reopen when a source owner identifies a concrete call-count/order question
that sampled stacks cannot answer and accepts maintaining instrumented build
variants. Before a build decision, review an ownership/compatibility ADR,
freeze API/CLI spelling and raw/normalized schemas with independent fixtures,
then qualify recursion, early/nonlocal exits, lifecycle, identity mismatch,
overflow, observer failure, storage/deadline behavior and ordinary-run hook
absence. Retain original/instrumented optimization evidence and generous-
deadline Recording equivalence. Only then create separate API/lifecycle,
decoder and presentation tickets. Process/FMU/prebuilt-library/XRay coverage
and CPU-duration/target/WCET claims remain excluded.

## Late capture windows — #361: defer

Offline selection reaches retained late Activations but cannot reduce capture
volume. The existing collector's storage-reserve disable path establishes
neither exact per-Activation control nor preserved setup metadata for a new
windowed collector. No new enable/disable experiment is claimed here.

Defer because the offline workflow can inspect already captured outliers;
there is no retained named workload demonstrating that current capture volume
prevents that investigation. Reopen with a reproducible optimized Native
late-spike case after Activation 500, a Process variant and equal-time Tasks,
when whole-Run collection exceeds a declared storage/processing budget.
Compare whole-Run/post-filtering with perf control enable/disable on a pinned
Linux native/container setup. Retain requested Virtual-time bounds separately
from actual host sample/control/acknowledgment boundaries, setup module/exec
metadata, latency, overshoot, file growth and observer cost. Cover empty and
unreached windows, interval edges, failure/unclosed work, loss and reserve
exhaustion. Preserve ordinary/observed Recordings with explicit generous
deadlines. Deliberately uncaptured regions need their own coverage state;
asynchronous commands cannot promise isolation of one short Activation.
A positive result still requires a separate coverage/schema contract. No
trigger language, ring buffer or heap windowing is authorized.

## Scheduler/blocking evidence — #362: defer

A long inclusive host interval with little sampled CPU establishes no
off-CPU duration or blocking cause. In particular, subtracting sampled weight
from elapsed wall time is invalid. The #330 permission/container findings are
useful feasibility inputs; its allocator probe is not scheduler evidence.
No sleeping/runnable classification has been newly qualified here.

Defer until a named slow Process Step cannot be explained with existing
sampling/diagnostics and a permitted scheduler-event capture environment is
available. Reopen with independently controlled CPU-bound, sleep,
synchronized-wait and competing-worker phases, deterministic publications and
verified host-clock markers. Assess pinned perf scheduler events and/or eBPF
availability before capture, without silently changing security policy.
Qualify PID/TID/process lifetimes, namespaces, exec/exit, wakeup/switch and
initial/final states, child/background activity, missing transitions, finite
storage/loss, interruption and failed/unclosed Steps. Separate runnable delay,
sleep and unobserved time; process-window association is not exclusive Step
work, and sleeping does not identify a lock or IPC cause. Retain native and
container permission failures, output growth/observer observations and
Recording equivalence. A build decision needs a separate state/coverage
schema and implementation tickets, never a guest RTOS/ECU timing claim.

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
