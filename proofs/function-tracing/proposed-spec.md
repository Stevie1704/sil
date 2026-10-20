# Proposed follow-up: opt-in Native C source scopes

Status: proposal for a separate specification and tickets, not a supported
interface or part of Phase 1. The #320 proof must establish feasibility
before this proposal is accepted. No implementation is authorized by this
document alone.

## Intended behavior

A source owner explicitly builds one Native C library with selected logical
function scopes. One opt-in diagnostic Run captures bounded entry/return
events and joins complete calls to Native Task Activations. A user can inspect
call counts, entry order, nesting and elapsed host durations for an Activation,
including logical calls whose bodies were inlined by the instrumented build.
The source owner chooses and maintains scopes; tooling must not rewrite
arbitrary application source with regular expressions as a supported workflow.

Initial scope is Linux x86-64/glibc, GCC 12, ordinary C returns, one
instrumented library and synchronous runner-thread work. Worker-thread calls
remain unattributed; nonlocal exits/longjmp, cancellation and C++ unwinding
must be rejected or explicitly partial. Recursion has per-thread stacks.
Process participants, FMUs, arbitrary prebuilt libraries and XRay are excluded.

## Contract to freeze before implementation

- Name and source-level API for entry/return scopes, event IDs and their
  build-matched symbol/source identities. It is a separate optional observer
  API, with no change to the Participant ABI, Manifest or Step protocol.
- Explicit Run-boundary opt-in and a separately versioned capture envelope.
  No automatic instrumentation and no changes to the Phase 1 profile schema.
- Shared host CLOCK_MONOTONIC event clock, verified Run/process/thread
  lifetime identity, and half-open Activation correlation. Every incomplete,
  ambiguous, worker-thread or crossing call carries a reason. Missing exit
  events never receive fabricated durations.
- Finite memory/storage/event limits, bounded shutdown, loss counters,
  fresh output paths, partial-capture exit behavior and cleanup consistent
  with the parent #313 capture requirements. Missing requested capture fails
  after retaining the independent Run outcome.
- Raw events plus normalized offline evidence, with a bounded decoder and
  per-Activation call view. Explain inclusive nesting, observer perturbation
  and compiler-dependent inlining. No CPU-time, instruction-trace, target
  timing or WCET claim.
- Original and instrumented build provenance; a library loaded at the same
  path is identified by actual bytes/build ID, not the path alone. Symbolizing
  requires matching debug files. Library loading/unloading bounds capture.

## Required acceptance before shipping

Retain the #320 one/eight-object example and add meaningful Run-boundary
checks for recursive scopes, multiple Tasks sharing Virtual time, deliberate
loss and observer failure. Pin the offline join with independent event files
covering boundaries, failed/unclosed intervals, thread/PID lifetime mismatch,
partial writes and corrupt identities. Verify ordinary and instrumented
Recordings/outcomes with generous deadlines, then show response-deadline
perturbation separately. Measure overhead and storage without timing gates.

Keep ADR 0004's ordinary kernel path free of observation hooks. Before adding
an installed observer API or capture format, record its ownership and
compatibility decision in a separate ADR/spec review. Split source-scope API,
capture lifecycle, offline decoding and user presentation into follow-up
tickets only after that review.
