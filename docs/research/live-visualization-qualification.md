# Live visualization and Run control: qualification draft (#350)

Status: **draft**. Date: 2026-10-30. Scope: issue
[#350](https://github.com/Stevie1704/sil/issues/350), section 4 of the
specification [#345](https://github.com/Stevie1704/sil/issues/345).
Proposed decision: [ADR 0005](../adr/0005-live-observation-and-run-control.md)
(status **proposed**, not accepted).

This document does not establish implemented or supported behavior. No
kernel code changes with it. It applies the
[consumer capability gate](../decisions/118-consumer-capability-gate.md) to
live observation and Run control, gives the measurements that are cheap now,
and writes down the contract that #351 and #352 need if the gate opens.

Every figure has one of two labels:

- **measured**: this document's author ran the command or read the retained
  evidence that it names;
- **estimated**: a calculation from measured figures and stated assumptions.
  An estimate is not a capability claim.

## 1. Summary

| Question | Result |
| --- | --- |
| Is there a named workload that needs live visualization? | **No.** The proposed candidate (section 2) finishes in seconds, and its finalized Recording can play back through #346–#349 with the same data. #346–#349 are implemented on this branch (PR #353) and are not yet merged. |
| Does the kernel have a safe seam for control between complete Slots? | **Yes.** `Engine::run` in `kernel/src/engine.cpp`, between the check for "no next Slot" and the dispatch of the Slot's due work (section 4). No Step exchange is outstanding there. |
| Can an observation feed be bounded and nonblocking without a change to Recording bytes? | **Yes, by design** (sections 5 and 7). It is not prototyped. |
| What is the estimated observer cost? | At most 0.7 % of measured wall time per Slot on the measured workloads (section 3.3). |
| Recommendation | **Defer** #351 and #352. Record the contract now. Keep completed-Recording delivery (#346–#349) independent. |

## 2. Proposed workload and external client

### 2.1 Workload

The proposed named workload is the closed-loop ADAS proof
[`proofs/adas-closed-loop`](../../proofs/adas-closed-loop/README.md), case
`approach.native`. It is the longest closed loop that the repository retains.
The ADAS reference controller drives the ACC plant FMU. The controller
changes mode two times: to mode 1 at 2.31 s and back to mode 0 at 3.25 s
Virtual time (`evidence/runs.json`, measured).

The interactive need that a live user states for this kind of workload is:
"pause the Run at the mode change, look at the inputs of that Slot, and
advance one Slot at a time".

| Identity | Value |
| --- | --- |
| Proof | `proofs/adas-closed-loop`, case `approach.native` (also `approach.fmu`) |
| Controller FMU SHA-256 | `f99c95f06ac97a7ce0ade263fe191cc8579c1e93355d360680b2ae72c5d503ab` (`evidence/summary.json`) |
| Plant FMU SHA-256 | `c1e24fafe8ce27de91cb01007b0b31a580fef408677fb99adcbb8a79fe3ff015` (`evidence/summary.json`) |
| Duration, Period, Slots | 3.52 s, 10 ms, 352 Command Messages (`evidence/runs.json`) |
| Launch | `proofs/adas-closed-loop/run-proof.sh [evidence-directory]` (Docker, Linux x86-64) |
| Machine class of the evidence | Native Linux x86-64 |

### 2.2 External client

| Role | Proposed client | Identity status |
| --- | --- | --- |
| Live viewer | Foxglove app, connected to the #349 adapter in live mode (`Time` capability, no `PlaybackControl`) | SDK pinned: `foxglove-sdk==0.30.3`. App version **not pinned**: no app is available on the qualification machine. |
| Run control | Python `RunControl` client and a `sil run control` CLI (section 5.7) | Proposed only |
| Run control from the viewer | Foxglove services `sil/run/*` through an existing service-capable client | SDK primitive verified by source reading in #349; app behavior not verified |

### 2.3 Why finalized playback is sufficient for this workload

| Fact | Value | Label |
| --- | --- | --- |
| Wall time of one `approach.native` Run | 4.464 s, 4.436 s | measured (retained `evidence/runs.json`, Linux x86-64) |
| Wall time of one `approach.fmu` Run | 5.490 s, 5.488 s | measured (retained) |
| Wall time of the esmini consumer Run (8 s Virtual) | 0.663 s | measured (retained, [#118 decision](../decisions/118-consumer-capability-gate.md)) |
| Wall time of the ADAS reference Manifest, 20 Slots | median 14.7 ms over 7 Runs (14.5–19.1 ms), all byte-identical | measured, macOS arm64, retained (section 3.1) |

Runs are deterministic. A second Run with the same Manifest gives the same
Recording bytes. A user who wants the state at the mode change can open the
finalized Recording in served playback (#349), seek to 2.31 s and step
Message by Message. Served playback is implemented on this branch (PR #353)
and is not yet merged. The user gets the result in about 5 s. Live control adds
no information that the Recording does not hold, and it saves less than
5 s of wall time on this workload.

Thus the workload **does not show** a user-visible benefit of live mode.
This is the same result that closed
[#285](https://github.com/Stevie1704/sil/issues/285) ("no named long or
expensive workload exists"). The capability gate says that a fixture made
only to justify expansion does not open the gate. This document therefore
does not make a longer synthetic workload.

### 2.4 Evidence that reopens the gate

A named adopter workload that shows at least one of these:

1. A Run wall time that is much longer than the acceptable feedback time.
   Proposed threshold for the user's decision: at least 60 s of wall time per
   Run, where the user acts on an early observation.
2. An interactive action that the finalized Recording cannot replace. For
   example: attach a debugger to a Process participant at a Slot boundary
   that is not known before the Run. (The proof
   [`proofs/source-debugging`](../../proofs/source-debugging/README.md)
   selects the Activation before the Run and does not need this.)
3. A Participant whose behavior a second Run cannot reproduce, with an
   explanation of why that Run is still useful evidence.

The evidence must include the measured completion cost and the expected time
that the user saves compared with post-run playback.

## 3. Measurements

### 3.1 Method

- Script: [`tools/live_visualization_measure.py`](../../tools/live_visualization_measure.py).
  Its `recordings` command reads each Recording with
  `sil.recording.read_records` and counts Messages, payload bytes, distinct
  publication times, Messages per Channel per time (Burst depth) and
  descending timestamps. Its `time` command times Runs of a Manifest.
- Retained output:
  [`live-visualization-measurements.json`](live-visualization-measurements.json),
  with the machine, the date and the commands.
- ADAS reference Recording: `examples/adas-reference/run.sh` with the
  `sil-run` that `cmake --install` staged from the local build (macOS arm64,
  SDK 26.5). The script stopped at the later `sil compare` step because
  `sil` was not on `PATH`. The two reference Runs completed and were
  byte-identical before that step. The generated Recording is not retained;
  its statistics and SHA-256 are.
- Timing: the `time` command, 7 Runs of `reference.json` with `sil-run` and
  `time.perf_counter` around each subprocess. Every output had the same
  bytes.

"Publication instants" are distinct publication times. A Slot that publishes
nothing is not visible in a Recording, so this count is a lower bound of the
Slot count.

### 3.2 Publication payload sizes, rates and Slot sizes (measured)

| Recording | Channels | Messages | Payload sizes (B) | Publication instants | Virtual span (s) | Messages per Virtual s | Payload B per Virtual s | Max payload B per instant | Mean payload B per instant | Max Messages per instant | Max Burst depth | Descending timestamps |
| --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `proofs/esmini/evidence/run-1.mcap` | 2 | 1,600 | 72 | 800 | 7.99 | 200.3 | 14,418 | 144 | 144.0 | 2 | 1 | 0 |
| `proofs/acc-fmi/evidence/controller-1.mcap` | 4 | 28 | 8, 24 | 10 | 0.90 | 31.1 | 391 | 64 | 35.2 | 4 | 1 | 0 |
| `proofs/acc-fmi/evidence/plant-accelerate-1.mcap` | 4 | 22 | 8, 48 | 10 | 0.90 | 24.4 | 1,084 | 112 | 97.6 | 4 | 1 | 0 |
| `proofs/fmi-ls-bus/evidence/live-coarse.mcap` | 4 | 20 | 2,058 | 3 | 1.00 | 20.0 | 41,160 | 20,580 | 13,720.0 | 10 | 4 | 0 |
| `proofs/fmi-ls-bus/evidence/replay-aligned.mcap` | 4 | 20 | 2,058 | 7 | 0.90 | 22.2 | 45,733 | 8,232 | 5,880.0 | 4 | 2 | 0 |
| `proofs/agent-investigation/…/nominal-control/run-1.mcap` | 4 | 55 | 17, 56, 185 | 20 | 0.19 | 289.5 | 22,289 | 443 | 211.8 | 4 | 1 | 0 |
| ADAS reference `reference-1.mcap` (10 maneuvers in one Run) | 40 | 735 | 17, 56, 185 | 20 | 0.19 | 3,868.4 | 407,447 | 4,413 | 3,870.8 | 39 | 1 | 0 |

ADAS reference `reference-1.mcap`: 119,817 bytes, SHA-256
`19579addd97385152aeba8e783a5a623c82b86c0a33f44f32858f83ee53b5510` (retained
measurement). The generated Manifest names absolute paths in the work
directory, so the Recording SHA-256 and the Manifest hash change with that
directory; the statistics in the table do not. The esmini Recording hash
agrees with the #118 decision.

The largest measured Slot is 20,580 payload bytes (FMI-LS-BUS, 10 Messages).
No retained Recording has descending timestamps, but the Replay participant
can produce them (section 5.9).

### 3.3 Observer cost (estimated)

Assumptions: the kernel copies each published Message once more into a
shared-memory ring (section 5.2), with a 24-byte record header per Message
(Channel index, Channel sequence, publication time, byte length) and a
40-byte batch header per Slot; a copy
runs at 5 GB/s or faster; the fixed cost per Message is 50 ns or less; the
control check costs one `poll` system call with zero timeout per Slot,
2 µs or less.

| Workload | Bytes copied per Slot (payload + headers) | Estimated cost per Slot | Measured wall time per Slot | Estimated overhead |
| --- | ---: | ---: | ---: | ---: |
| esmini | 144 + 2 × 24 + 40 = 232 B | ≤ 2.2 µs | 829 µs (0.663 s / 800, retained) | ≤ 0.3 % |
| ADAS reference, 10 maneuvers | 4,413 + 39 × 24 + 40 = 5,389 B | ≤ 5.1 µs | 736 µs (14.7 ms / 20, includes process start, retained) | ≤ 0.7 % |
| ADAS closed loop | not measured (no retained Recording) | ≤ 5 µs if similar to the reference | ≈ 12,700 µs (4.46 s / 352, retained) | ≤ 0.05 % |

The ADAS reference row is pessimistic: process start dominates its 15 ms, so
the true time per Slot is smaller and the overhead percentage is larger
than the time of execution suggests. The wall times in this table are
measured (section 3.1); the costs and percentages are estimates. A prototype must measure this cost
with `sil-run-diagnostics` before #351 starts. These numbers do not
support a claim of zero cost.

### 3.4 Acceptable freshness and queue capacity (estimated)

- Freshness: a human viewer does not need more than about 30 updates per
  wall second. The supervisor (section 5.3) forwards the latest complete
  batch; it does not need every batch for a live view.
- Ring capacity: 4 MiB holds 778 Slots of the largest per-Slot size of the
  ADAS reference (5,389 B), or 7.7 s of Virtual time at a 10 ms Period. It
  holds 201 Slots of the largest FMI-LS-BUS Slot (20,580 B + 10 × 24 B +
  40 B = 20,860 B).
- Spool: not proposed. Latest-only mode (`seek=false`) needs no history
  spool. Complete history comes from the finalized Recording.

## 4. The Slot-boundary seam in `Engine::run`

`Engine::run` (`kernel/src/engine.cpp`, lines 206–289 at
`9238714`) is one loop. Each iteration:

1. Line 209: checks `run_interrupted()`.
2. Lines 210–217: finds the next Slot as the minimum `next_ns` of all
   Tasks that are not done and all Replay participants' next publication
   time. If there is none, `break` (line 217) goes to normal shutdown.
3. Lines 219–240: sorts the due work.
4. Line 242: sets `now_ns_ = slot`.
5. Lines 246–282: runs the Replay participants and each due Task. A Process
   participant's Task calls `ProcessParticipant::step`, which sends the Step
   request and waits for `step_done` in the same call
   (`kernel/src/process_participant.cpp`, the `exchange` call at line 743).

**Proposed seam:** between line 217 and line 219. At this point:

- The previous Slot is complete. All its due work returned, and all its
  routing and recording calls are done.
- No Step exchange is outstanding. `step` is synchronous: it returns only
  after the response or after a failure.
- The next Slot is known. If no next Slot exists, the loop already left
  through line 217, so "Advance Slot" with no next Slot completes the Run
  through the normal shutdown at lines 285–288. Completion wins a race with
  a pause.
- `now_ns_` still holds the last completed Slot time, so status can report
  both the last completed time and the next Slot time.
- The first time the loop reaches this point is after `setup()` initialized
  all Participants and before the first Slot. This is the controlled-startup
  point. No extra state is necessary.

**Response deadline.** `request_response` (line 829) starts the deadline
when it sends the request (lines 832–846). The deadline does not exist
between exchanges. Thus a wait at the seam does not consume a Process
participant's Response deadline.

**Clock shim.** `write_clock_region(now_ns)` (line 737) writes the Virtual
time before each Step. Nothing writes it at the seam, so the shim value
stays fixed while the Run is paused.

**What a pause does not freeze.** Native participant threads, Process
participant background threads and child processes continue to run in wall
time. This is a documented limit, not a defect to repair in the kernel.

**Not a checkpoint.** A pause holds a live Run; it does not save or restore
state. This draft does not infer checkpoint or resume support from
[#196](https://github.com/Stevie1704/sil/issues/196).

**Hung Activation.** A pause is accepted at once, but it completes only when
the current Slot returns to the seam. A Process participant with a Response
deadline fails the Run at the deadline; the failure is the terminal outcome.
Without a deadline, the pause stays pending. Cancellation reaches a waiting
Process exchange only through a signal. `run_interrupted()`
(`kernel/src/run_signal.cpp`) reads a flag that only the SIGINT, SIGHUP and
SIGTERM handler sets, and `send_line` and `read_line` check it only after a
system call returns `EINTR` (lines 820, 960 and 972). A wait without a
deadline is `poll(-1)` (line 958), so a cancel request must make the
supervisor deliver a signal, for example SIGTERM, to `sil-run`. A flag that
the control descriptor sets would not interrupt that wait.
A Native Task that does not return cannot be interrupted; this limit stays.

**Wait at the seam.** While paused, the kernel waits in `poll` on the control
descriptor with a timeout of at most 100 ms, and checks `run_interrupted()`
after each wake-up. Signals keep their current effect.

**Diagnostics.** Pause time falls inside the `ScheduledExecution` phase.
The diagnostics runner must report it as a separate `paused` duration and
exclude it from Activation and phase statistics.

A prototype of the seam is optional for #350 and is not on this branch.

## 5. Proposed contract

### 5.1 Process structure

```text
client (Python/CLI, Foxglove services)
    │  Unix domain stream socket, length-prefixed JSON (section 5.4)
    ▼
supervisor: `sil run --controlled` (Python)
    │  control: inherited socketpair, fixed binary frames
    │  observations: inherited Mapped region, single-producer ring
    ▼
sil-run (kernel): seam in Engine::run, ring writer in Engine::publish
```

The kernel has no network code, no JSON parser for control and no SDK
callback. The supervisor owns leases, revisions, request deduplication,
the client socket and the Foxglove adapter. One supervisor serves one Run.

### 5.2 Kernel-side observation ring

- One `Mapped region` of fixed size (default 4 MiB, minimum 64 KiB,
  maximum 64 MiB). The kernel is the only writer; the supervisor is the only
  reader. Head and tail are 64-bit atomics.
- One **batch** per Slot that publishes at least one Message. A batch is a
  header and the Slot's Messages in Publish order:

  | Field | Type | Meaning |
  | --- | --- | --- |
  | `sequence` | u64 | Batch number, starts at 1, increments for each batch the kernel attempted, also for a dropped batch |
  | `slot_ordinal` | u64 | Number of the Slot in execution order, starts at 1 |
  | `slot_time_ns` | u64 | Virtual time of the Slot |
  | `dropped_before` | u64 | Count of batches dropped since the last committed batch |
  | `flags` | u32 | `time_regressed`, `resync` |
  | `message_count` | u32 | Messages in this batch |

  Each Message record: Channel index (u32), Channel sequence (u64),
  publication time (u64), byte length (u32), payload. The fields are the
  same values that the recorder gets in `Engine::publish`.
- The kernel writes Messages into reserved ring space while the Slot runs
  and commits the batch at the seam. If the batch does not fit in the free
  space, or exceeds the batch limit (default ring size / 4), the kernel
  stops copying for that Slot and counts one dropped batch. It never waits
  for the reader.
- Batches are whole or absent. The kernel never commits part of a batch.

### 5.3 Supervisor and viewer

- The supervisor reads committed batches, converts them with the shared
  `sil_visualization_json` profile and sends them through the #349 relay and
  barrier window. If the viewer's window is full, the supervisor keeps only
  the newest batch and records a viewer-side gap. A viewer-side gap never
  reaches the kernel.
- Viewer pause in Foxglove pauses only the view. The live server does not
  advertise `PlaybackControl`.
- SDK or client buffering is not execution control: a slow or stalled viewer
  only loses or delays what it shows (a viewer-side gap), and the Run never
  waits for it. Only the `sil.run_control` operations at the seam change
  execution.
- `Time` broadcasts follow acknowledged data (the #349 barrier rule).

### 5.4 Local IPC framing

- Transport: Unix domain stream socket, path from `--control-socket`
  (default: a new file in the Run working directory), mode `0600`. Loopback
  TCP is not provided.
- Frame: a 4-byte unsigned big-endian length, then one UTF-8 JSON object.
  Maximum frame 64 KiB. A larger frame closes the connection with error
  `too_large`.
- Every frame has `"protocol": "sil.run_control"` and `"version": 1`.
  64-bit integers are decimal strings, as in the visualization profile.

### 5.5 Request, response and error schemas

Request:

```json
{"protocol": "sil.run_control", "version": 1, "type": "request",
 "op": "pause", "run_id": "…", "request_id": "c1-17",
 "lease": "…", "expected_revision": "41"}
```

| `op` | Lease | `expected_revision` | Effect |
| --- | --- | --- | --- |
| `status` | no | no | None |
| `acquire` | no | no | Gives a lease if no lease is valid; `"reclaim": true` with the previous token takes it back after a disconnect |
| `renew` | yes | no | Extends the lease |
| `release` | yes | no | Ends the lease; the Run does not change state |
| `resume` | yes | yes | Continue normal execution |
| `pause` | yes | yes | Pause at the next seam |
| `advance_slot` | yes | yes | From `paused`, run one Slot, then pause |
| `cancel` | yes | yes | Existing interruption and cleanup |
| `subscribe` | no | no | Receive `event` frames on this connection |

Response (acceptance, not completion):

```json
{"protocol": "sil.run_control", "version": 1, "type": "response",
 "request_id": "c1-17", "accepted": true, "state": {…}}
```

Error response (no effect occurred):

```json
{"protocol": "sil.run_control", "version": 1, "type": "response",
 "request_id": "c1-17", "accepted": false,
 "error": {"code": "stale_revision", "message": "…"}, "state": {…}}
```

| Error code | Condition |
| --- | --- |
| `malformed` | Not a valid frame or JSON object, or a field has the wrong type |
| `too_large` | Frame larger than 64 KiB |
| `unsupported_version` | `protocol` or `version` not supported |
| `unknown_op` | `op` not in the table |
| `unknown_run` | `run_id` is not this Run |
| `lease_required` | No lease, an expired lease or another controller's lease |
| `lease_held` | `acquire` while another valid lease exists |
| `stale_revision` | `expected_revision` is not the current revision |
| `request_conflict` | A known `request_id` with different content |
| `invalid_state` | For example `advance_slot` while `running` |
| `operation_pending` | A previous operation is not complete |
| `run_terminal` | The Run has a terminal outcome |

`state` object:

| Field | Meaning |
| --- | --- |
| `run_id` | Random 128-bit identity of this Run, in hex |
| `manifest_hash` | Manifest hash of the Run |
| `revision` | Decimal string; increments for each accepted state-changing request and each completion |
| `lifecycle` | One of the states in section 5.6 |
| `slots_completed` | Starts at `"0"` |
| `last_completed_slot_time_ns` | `null` before the first Slot |
| `next_slot_time_ns` | `null` if not known or no next Slot |
| `pending` | `null` or `{"op", "request_id", "accepted_revision"}` |
| `controller` | `null` or `{"lease_expires_in_ms"}`; never the token |
| `outcome` | `null` or `{"status": "completed" | "failed" | "cancelled" | "interrupted", "exit_code", "message"}` |
| `paused_wall_ns` | Total intentional pause time, wall clock, informative |

Event frame: `{"type": "event", "event": "operation_completed" |
"state_changed" | "lease_expired" | "terminal", "request_id"?, "state"}`.

Deduplication: the supervisor keeps the last 256 request IDs with the content
hash and the response. A repeated identical request returns the cached
response. A different request with a known ID returns `request_conflict`.

### 5.6 Lifecycle transitions

| From | Event | To | Notes |
| --- | --- | --- | --- |
| `initializing` | `setup()` complete | `paused` | Controlled startup; `slots_completed` 0 |
| `initializing` | setup fails | terminal `failed` | Exit 2 or 1 as today |
| `paused` | `resume` | `running` | |
| `paused` | `advance_slot` | `stepping` | |
| `running` | `pause` accepted | `pausing` | `pending` set |
| `pausing` | seam reached | `paused` | `operation_completed` |
| `stepping` | seam after one Slot | `paused` | `operation_completed` |
| `running`, `pausing`, `stepping` | no next Slot | terminal `completed` | Completion wins |
| any non-terminal | `cancel` | `cancelling` | The supervisor sends SIGTERM to `sil-run`; the handler sets the interruption flag and interrupts a blocking wait |
| `cancelling` | cleanup done | terminal `cancelled` | Exit 1, as interruption |
| any non-terminal | Run failure | terminal `failed` | Actual exit code and message |
| any non-terminal | signal | terminal `interrupted` | Existing behavior |
| `running` | lease expires | `pausing` | `lease_expired` event; never cancel |

Terminal states do not change. Requests after a terminal state return
`run_terminal` with the actual outcome.

### 5.7 Lease rules

- One controller. A lease is a random 128-bit token.
- Timeout 5 s (proposed in #345). A client renews at least every 1.5 s.
- Expiry while `running` requests a pause at the next seam. Expiry while
  `paused` keeps the Run paused.
- A connected `subscribe` client without a lease cannot control the Run.
- `acquire` with `"reclaim": true` and the expired token gives the lease
  back to the same controller. Any client can `acquire` after expiry.
- Closing the viewer or the controller connection does not cancel the Run.
- Recovery from a lost controller:
  `sil run control status --socket PATH`,
  `sil run control acquire --socket PATH`, then `resume` or `cancel`. The
  Python client offers the same calls.
- If the supervisor process ends, the kernel sees end-of-file on the control
  descriptor. Proposed: the kernel cancels the Run (see open question 4),
  because no client can reach it any more.

### 5.8 Observation sequence and gap model

- Identity: (`run_id`, `sequence`). `slot_ordinal` and `slot_time_ns`
  identify the covered Slot. Batches do not carry wall time; the supervisor
  adds its receive time as a separate, informative field.
- Gap: `dropped_before > 0`, or a missing `sequence`. The supervisor sends a
  gap notice with the first and last missing sequence and the reason
  (`ring_full`, `oversize`, `viewer_slow`).
- After a gap, all displayed Channels are **stale**. The supervisor marks
  them stale in the viewer and requests resynchronization.
- Resynchronization: after a dropped batch, the kernel sets `resync` on the
  next batch that fits and adds the latest Message of each Channel that
  published at least once. The kernel keeps these latest Messages: memory is
  the sum of Schema sizes (ADAS reference: 40 Channels, at most
  40 × 185 B = 7,400 B, estimated). A resync restores current state, not the
  missing history. The finalized Recording is the only complete history.
- Retained history: none in v1 (latest-only, `seek=false`).

### 5.9 Descending replay timestamps

A Replay participant can open a Slot earlier than the previous Slot
(`docs/replay.md`, "Timestamps are replayed as stored"). The live feed keeps
`slot_ordinal` as its order key. A batch whose `slot_time_ns` is less than
the previous batch's time has `time_regressed` set. The supervisor then
clears the viewer session (`clear_session` in the SDK), re-advertises
Channels and continues from that batch. The Time broadcast then also moves
back. Ordinary Runs do not change.

### 5.10 Resource budgets

| Resource | Default | Limit | Overflow |
| --- | --- | --- | --- |
| Observation ring | 4 MiB | 64 KiB–64 MiB | Batch dropped, gap |
| One batch | ring / 4 | — | Batch dropped (`oversize`) |
| Kernel latest-value store | Σ Schema sizes | fixed by Manifest | — |
| Control frame | 64 KiB | — | Connection closed |
| Pending operations | 1 | — | `operation_pending` |
| Request-ID cache | 256 | — | Oldest removed |
| Client connections | 8 | — | New connection refused |
| Viewer queue | #349 relay window, backlog 1024 | — | Newest-only, gap |
| Paused wait wake-up | 100 ms | — | — |

No path in the kernel waits for an observer. The only wait is the paused wait
that a controller requested.

### 5.11 Supervisor shutdown sequence

When the Run reaches a terminal state (completion, failure, cancellation or
a signal):

1. The kernel commits the last whole batch, then finalizes the Recording as
   today. It does not write to the ring after that.
2. The kernel exits. Its end of the control socketpair and its mapping of
   the ring close with the process.
3. The supervisor reaps `sil-run` (`waitpid`), so it knows the exit code.
4. The supervisor reads the committed batches that remain in the ring and
   forwards them to the viewer. Then it unmaps and removes the ring.
5. The supervisor sets the terminal `outcome`, increments `revision` and
   sends the `terminal` event to every connected client.
6. Only then does it close the client connections, stop the viewer adapter
   and remove the control socket file. Thus a client never sees the socket
   close before the terminal state.

## 6. Verification plan

### 6.1 Byte equivalence

For each determinism fixture, the ADAS reference Manifest and the closed-loop
proof:

1. Run with control and observation disabled (today's path).
2. Run controlled, with an immediate `resume` and no viewer.
3. Run controlled with `advance_slot` for every Slot.
4. Run controlled with random pauses, including one pause longer than twice
   the Response deadline.
5. Run with a ring of 64 KiB and a reader that does not read (all gaps).
6. Run with a viewer that stops reading.

Each successful Run must give byte-identical Recordings, the same Manifest
hash, the same exit code and the same Latencies and scheduling order
(`tests/test_determinism.py` style). Response deadlines stay far from the
measured response times. The diagnostics report keeps pause time separate.

### 6.2 Failure and cancellation

| Case | Expected result |
| --- | --- |
| Initialization fails before the first Slot | Terminal `failed`, exit 2 or 1 as today; no `paused` |
| Pause while a Process Step hangs, deadline set | Pause stays pending; Run fails at the deadline; outcome `failed` |
| Pause while a Process Step hangs, no deadline | Pause stays pending; `cancel` delivers SIGTERM, which interrupts the `poll(-1)` wait of the exchange; outcome `cancelled` |
| Pause while a Native Task hangs | Pause stays pending; status never reports `paused`; limit documented |
| `advance_slot` on the last Slot | Normal shutdown; outcome `completed` |
| `pause` and completion in the same Slot | `completed` |
| Lease expires while `running` | `pausing`, then `paused` |
| Reclaim after reconnect | Same controller continues |
| Supervisor `SIGKILL` | Kernel sees end-of-file; outcome per open question 4; process group cleanup as #121 |
| Kernel `SIGKILL` | Supervisor reports `unknown` outcome and missing Recording finalization |
| Stale revision, conflicting request ID, wrong Run ID | Error, no effect, actual state returned |
| Background Participant thread during pause | Documented: not frozen; a test shows that the shim time stays fixed |

## 7. Completed-Recording delivery

Export (#346), navigation (#347), headless playback (#348) and the Foxglove
playback adapter (#349) do not depend on this gate. A defer decision does
not change them.

## 8. Open questions for the user

1. **Build or defer.** This draft recommends defer (section 2.3). Do you
   accept the defer decision and ADR 0005?
2. **Reopen threshold.** Is "at least 60 s of wall time per Run, with an
   early action" the right reopen criterion?
3. **Process structure.** A Python supervisor with an inherited control
   descriptor and a shared-memory ring (proposed), or a control thread
   inside `sil-run`?
4. **Supervisor loss.** When the kernel sees end-of-file on the control
   descriptor: cancel the Run (proposed) or stay paused until a signal?
5. **Resynchronization in the kernel.** Keep the latest Message of each
   Channel in the kernel (proposed, bounded by Σ Schema sizes), or accept
   that stale Channels stay stale until they publish again?
6. **Descending timestamps.** Do you confirm the policy of section 5.9:
   reset the viewer session at a batch with `time_regressed`?
7. **Cancellation exit code.** Report `cancelled` with exit 1 and a new
   message, as interruption does today (proposed)?
8. **Runner.** Support controlled Runs in `sil-run` only, or also in
   `sil-run-diagnostics`, where the pause duration must be reported?
9. **Dependent tickets.** Keep #351 and #352 open and blocked, or move them
   to the future milestone with the reopen criteria?
10. **Foxglove app version.** Which app version do you want pinned when the
    gate opens?
