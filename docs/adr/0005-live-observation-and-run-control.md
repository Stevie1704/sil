# 5. Defer live observation and Run control; record the contract

Date: 2026-10-30

Status: proposed

Relates to [issue #350](https://github.com/Stevie1704/sil/issues/350) and
section 4 of the specification
[#345](https://github.com/Stevie1704/sil/issues/345). Applies the
[consumer capability gate](../decisions/118-consumer-capability-gate.md).
Evidence and the full contract:
[live visualization qualification](../research/live-visualization-qualification.md).

## Context

#345 proposes live observation of a Run in an external viewer (Foxglove) and
a separate Run-control interface: controlled startup, `pause`, `resume`,
`advance_slot` and `cancel` between complete Slots. #351 (control seam) and
#352 (observation feed and services) wait for this decision.

The capability gate requires a named consumer workload that the existing
contracts cannot serve. #285 deferred live observation for the same reason.

The qualification found:

- The longest retained closed loop, `proofs/adas-closed-loop`
  `approach.native`, takes about 4.5 s of wall time per Run (measured,
  retained evidence). The esmini consumer Run takes 0.663 s. The ADAS
  reference Manifest takes 15 ms on the qualification machine.
- Runs are deterministic. The finalized Recording, played through #346–#349,
  shows the same data that a live view would show, about 5 s later.
- `Engine::run` has a safe seam between the "no next Slot" check and the
  dispatch of due work. No Step exchange is outstanding there, and a wait
  there does not consume a Response deadline.
- The estimated observer cost is below 0.5 % of wall time per Slot on the
  retained workloads.

## Decision

**Defer.** Do not build #351 or #352 now. No named workload shows a
user-visible benefit of live mode over finalized-Recording playback.

**Record the contract** in the qualification document as the starting point
when the gate opens. Its main rules:

- Control happens only at the seam in `Engine::run` after a complete Slot.
  The kernel never pauses during a Step exchange. Pause time is not charged
  to a Response deadline and is reported apart from performance figures.
- The kernel has no network code. A Python supervisor owns the local IPC
  (Unix domain socket, length-prefixed JSON, `sil.run_control` version 1),
  leases (5 s), revisions, request deduplication and the viewer adapter.
- Observations go through a bounded single-producer ring. Batches are whole
  or dropped. Gaps are explicit. Displayed state is stale until
  resynchronization. The kernel never waits for an observer.
- The viewer's pause does not pause the Run. Closing a viewer or losing a
  controller does not cancel the Run.
- Successful uncancelled Runs keep byte-identical Recordings with control and
  observation enabled or disabled.

**Keep completed-Recording delivery independent.** #346–#349 do not depend on
this decision.

## Reopening evidence

A named adopter workload with a measured completion cost, and one of:

- a Run wall time much longer than the acceptable feedback time (proposed:
  60 s or more) with an early action that the user takes;
- an interactive action at a Slot that is not known before the Run and that a
  finalized Recording cannot replace;
- a Participant whose behavior a second Run cannot reproduce, with the reason
  why the Run is still valid evidence.

The evidence must state the expected wall time saved compared with post-run
playback. A fixture made only to justify the capability does not count.

## Consequences

- The Manifest, Manifest hash, Step protocol, Native ABI, Recording bytes and
  exit codes do not change.
- #351 and #352 stay blocked. Their refinement starts from the contract in
  the qualification document, after the open questions there are answered.
- The control seam and the observation ring are not prototyped. Their cost
  estimate must be measured before implementation.
- If the user does not accept this ADR and decides to build, the open
  questions in section 8 of the qualification document must be answered
  first.
