# SiL Framework — Design Decisions

Outcome of a design grilling session, 2026-07-02. Greenfield project for an
automotive ADAS/AD company. Requirements: (1) determinism, (2) lightweight
(scheduling + data routing kernel), (3) usable across vECU levels.

## Decision record

### 1. Primary use case: CI regression testing
Headless, massively parallel, faster-than-real-time execution in a build farm.
Determinism means: same inputs → bit-identical outputs, so every test failure
is reproducible. Desktop debugging, re-simulation, and homologation are
follow-on use cases, not drivers of v1.

### 2. Determinism scope: same artifacts + same machine class
Bit-exact reproducibility is guaranteed when the same compiled artifacts run
on the same OS/CPU architecture (e.g. x86-64 Linux CI runners). Dev machines
get best-effort repro. Cross-platform bit-exactness is a non-goal — it would
require policing FP behavior in vECU code the framework doesn't own.

### 3. vECU scope (v1)
- **L0/L1** host-compiled algorithm/application code (in-process).
- **POSIX/Adaptive AUTOSAR** stacks as separate Linux processes.
- **L2/L3** classic AUTOSAR vECUs from vendor tools — via adapter only (see #10).
- L4 (ISS/QEMU) deferred.

### 4. Time model: central time master, sequential stepping
One coordinator owns virtual time. Every participant is stepped explicitly in
a fixed, configured order — never by the OS scheduler. Bit-exact by
construction. Parallel stepping is a later optimization, added only where
provably race-free.

### 5. Scheduling unit: two-tier
- Native L0/L1 code registers **periodic tasks** (period, offset,
  order-priority) that the scheduler orders directly.
- Opaque vECUs (POSIX processes, FMUs) implement a single **`step(t, Δt)`**
  contract and manage internal rates themselves.

### 6. Internal nondeterminism of opaque vECUs: contract + verification
The vECU author is responsible for internal determinism given stepped virtual
time and delivered inputs (single-threaded executors, no wall-clock reads,
seeded RNG). The framework provides the virtual-time/clock API that makes this
achievable, and a **determinism-check mode** in CI: run twice, bit-diff
outputs, fail loudly. Violations are made visible, not silently guaranteed
away. No framework-enforced thread serialization (conflicts with lightweight).

### 7. Data routing: neutral typed pub/sub core + bus adapters
The core routes typed messages on named channels with virtual-time stamps —
nothing bus-specific. CAN, SOME/IP, DDS semantics (signal packing, service
discovery, QoS) are adapter layers, opt-in per channel.

### 8. Delivery semantics: explicit per-channel latency, default 1 activation
A message published at t becomes visible at t + declared channel latency;
default is the consumer's next activation (unit-delay style). Results are
therefore independent of execution order within a time slot — reordering the
manifest cannot change outputs. Same-slot direct feedthrough must be declared
explicitly, which documents real data-path dependencies.

### 9. Message representation: schema-defined fixed layout + shared memory
Types declared in a schema, code-gen to C/C++/Python, fixed versioned
POD-style layout. Small messages copied; large sensor payloads (camera frames,
point clouds) passed via shared-memory ring buffers with ownership handover —
zero-copy across processes. Deterministic byte layout makes recording and
bit-diffing trivial.

### 10. Adapters at the edge
- **Environment/plant** (scenario engine, dynamics, sensor models): just
  another stepped participant with `step(t, Δt)` + channels. Ship thin
  reference adapters (e.g. esmini + internal dynamics model). The framework
  never becomes a simulator.
- **Vendor L2/L3 vECUs and models**: one blessed importer — **FMI 3.0
  co-simulation + FMI-LS-BUS** for bus traffic. Importer-as-adapter, not an
  FMI master in the kernel.

### 11. Implementation: C++ kernel, stable C ABI, Python bindings
C++20 core (matches vECU code, org skills, vendor SDKs). The integration
contract is a small stable C ABI — survives compiler/version skew, trivially
bindable. Python bindings for test orchestration, config generation, analysis.

### 12. Configuration: declarative manifest + Python builder
A strict-schema declarative manifest is the single execution input —
canonical, hashable, archived with every run as part of the reproducibility
contract. A Python builder API generates/validates manifests for test matrices;
the kernel only ever consumes the manifest.

Manifest objects are closed: unknown keys are rejected at load time. The one
intentional extension point is a native participant's `config` object; its
contents are participant-specific and are passed through as JSON to the native
participant, while the `config` container itself must still be an object.

### 13. Recording: MCAP native; record/replay inside stepped virtual time
Recorder and replayer live inside the stepped virtual-time world, so both are
deterministic by construction: the recorder as a direct sink at the publish
choke point (equivalent to a latency-0 subscriber running last in every
slot), the replayer driven by the kernel loop, which folds its recorded
timestamps into slot selection and publishes them before task activations.
MCAP as the container: self-describing, schema-embedded, indexed, free
tooling (Foxglove). Bit-diff of two runs = compare channel streams. Replaying
recorded channels as stimulus covers open-loop re-simulation with no new
machinery.

### 14. Test API: in-schedule test participant + pytest frontend
A test is itself a scheduled participant: publishes stimuli and evaluates
assertions at defined virtual times — fully inside the deterministic world, so
failing tests replay exactly. Authored in Python, executed under pytest.
Fault injection = channel interceptors declared in the manifest (faults are
part of the reproducible config). Post-hoc KPI evaluation against MCAP is a
complementary pattern.

### 15. CI unit: one run = one container; orchestration out of scope
A run is a self-contained process tree (kernel + participants), packaged in
one container image; outputs are exit code + MCAP + manifest hash (for
cache/dedup). Scheduling thousands of runs is the job of existing CI/batch
infrastructure.

### 16. Milestone 1: walking skeleton + determinism proof
Kernel (time master, task scheduling, typed channels), two toy native
participants, one out-of-process participant, MCAP recorder, manifest, pytest
test participant. **Exit criterion: run twice → bit-identical MCAP, enforced
in the framework's own CI from day one.**

## Explicit non-goals
- Cross-platform bit-exactness.
- Own environment/scenario/sensor simulation.
- Native bus emulation in the core.
- Framework-enforced thread serialization of opaque vECUs.
- Run-fleet orchestration / result database.
- L4 (ISS) vECUs in v1.

## Open questions (not yet decided)
- Which environment tool gets the first reference adapter.

## Resolved since (2026-07-07)
- **Schema/code-gen (#9):** custom minimal generator. `tools/silschema.py`
  emits packed C structs; `sil.schema` packs the identical layout in Python
  (little-endian, declared field order, no padding). FlatBuffers/Cap'n Proto
  passed over — overweight for fixed POD layouts.
- **Manifest format (#12):** canonical JSON (sorted keys, compact
  separators), SHA-256 over the exact file bytes. Strict validation mirrored
  in the Python builder and the kernel loader (defense in depth).
- **Virtual-time API for POSIX vECUs (#6):** explicit API only in v1 — the
  step protocol delivers `t`/`dt` with every activation. A clock shim
  remains open (above).
- **Project name:** sil.
- **Recording-format seam (#13, #24):** the container is chosen from the output
  extension behind a format-neutral sink (write side) and reader (read side);
  MCAP is the only v1 format, an unrecognized extension is a config error
  (exit 2) that rejects before any participant starts. **A recording format
  must preserve, or explicitly store, the total publish order** — for messages
  sharing a timestamp the replayer's tie-break is the recording's stored order,
  so any format that does not keep write order intact must record an explicit
  sequence. MCAP satisfies this via FileOrder reads of an in-order, uncompressed
  write.
- **POSIX vECU clock shim (#4):** preload interposition, resolving the open
  question in favor of `LD_PRELOAD` (Linux) / `DYLD_INSERT_LIBRARIES` with a
  `__DATA,__interpose` table (macOS). Link-time wrapping is rejected — opaque
  binaries cannot be relinked; explicit-API-only is rejected — opaque code will
  not call a new API. The shim interposes `clock_gettime` (monotonic-class IDs
  → virtual `t`; realtime-class → epoch + `t`), `gettimeofday`, `time`,
  `clock_getres` (reports 1 ns for those IDs), and the sleep family (`nanosleep`,
  `clock_nanosleep`, `usleep`, `sleep`); CPU-time clock IDs pass through.
  **Amended by #52 — sleep policy:** the sleep family is declared per shimmed
  participant with `sleep` in the hashed manifest. Neither policy can block:
  virtual time is frozen for the whole step and the kernel is synchronously
  waiting for the step response, so a sleep that waited for `t` to move would
  deadlock against the only thing that could move it. `immediate` keeps the
  original recorded behavior — return success as if the full duration had
  elapsed — and is what an absent field selects, so every manifest written
  before #52 keeps its hash and its meaning. `reject` fails the call instead,
  with `ENOSYS`, so that a retry loop ends rather than spinning the CPU for the
  rest of the step. ENOSYS and not EINTR: EINTR is the one errno every correct
  caller retries on, which is the spin the policy exists to end. `sleep()` is
  the exception the policy cannot cover — POSIX gives it no error return, so
  reject reports the full duration as unslept and sets `ENOSYS` for callers
  that look, and a caller ignoring the return value cannot tell the policies
  apart. Per #62 the Python builder always emits the field and defaults to
  `reject`, so the compatibility behavior is reachable by an existing document
  but never by authoring a new one. A cooperative virtual wake-up protocol,
  where a sleep would yield the step and resume at a later `t`, stays out of
  scope: it would change Activation ordering, which #46 preserves.
  **Amended by #76 — one classification, pass-through default:** which IDs are
  virtualized is one table every interposed call asks, so `clock_gettime`,
  `clock_getres` and `clock_nanosleep` cannot disagree. Two classes are
  virtualized — monotonic (`CLOCK_MONOTONIC`, `CLOCK_BOOTTIME` and their raw,
  coarse, approximate and alarm variants) and realtime (`CLOCK_REALTIME`, its
  coarse and alarm variants, and `CLOCK_TAI`, which reads as realtime because
  a Manifest declares one `epoch` and the model has no TAI-UTC offset) — and
  **every other ID passes through to the real libc**, including the CPU-time
  IDs and any unknown or future one. Pass-through is the default rather than realtime because an
  unnamed clock has no known class. The region can only answer with an
  epoch-based wall-clock value, and the clock may measure neither wall time
  nor this process. A CPU-time ID measures consumed CPU, so
  virtualizing it would report the participant using no CPU at all. The same
  table also puts the coarse, approximate and alarm variants into the class of
  the clock each approximates, where the two-way split had read every one of
  them as realtime.
  **Compatibility (per #62): a straight correction, not a declared field.**
  #62 governs new hash-covered fields, and its selection test — does the prior
  behavior still exist and is it worth a default — has no answer here. What a
  CPU-time ID returned was never CPU time: within a step the delta read zero,
  because virtual time is frozen, and across steps it read the virtual wall
  clock. Neither is a CPU-time semantics, so there is none to preserve. A field
  would instead enshrine the deviation as a supported mode for the life of
  Manifest version 1. No Manifest field ever
  declared CPU-clock virtualization, so no hash changes and no document needs
  regenerating. The reclassified variants ride on the same call for the same
  reason: this document already assigned every wall clock to its class, so an
  ID answered from the wrong one was never a declared semantics either, and
  those reads stay deterministic — only their class is corrected. Expected consequence: a participant that publishes its own CPU
  time into a Channel now records a nondeterministic value — CPU-time reads
  sit outside the deterministic envelope by this decision, and restoring that
  boundary changes no Activation order and no virtual-clock-derived Message.
  **Frozen-step semantics:** every *virtualized* clock read during one step
  returns the same `t`; time advances only between steps.
  **Time transport:** the kernel writes the current virtual time into a small
  fixed-layout region shared with the child (a memory-mapped file whose path is
  handed over in `SIL_CLOCK_REGION` at spawn) before each step; the shim maps it
  read-only and answers every read from it, so it has no knowledge of the step
  protocol and adds no per-read syscall. The shim is opt-in per process
  participant (manifest `shim` flag) with a manifest-declared realtime `epoch`;
  both live in the hashed manifest. Boundary (documented, out of scope):
  statically linked binaries and direct-syscall/vDSO clock users bypass the
  shim.
- **Shared-memory channel transport (#9, #35):** a channel opts in with
  `transport: "shm"` in the hashed manifest; `inline` (base64 inside the JSON
  step line) stays the default and is omitted from the canonical document, so
  pre-shm manifests keep byte-identical hashes. **Amended by #74:** a shared-
  memory Channel declares `slots`; the Python builder always emits it and
  defaults to 2, while absence means the legacy single slot. Two covers the
  measured burst and a subscriber missing one Step, at the bounded cost of one
  extra schema-sized payload per Arena. The kernel maps one Arena per
  (participant, Channel), with payload storage sized `byte_size * slots`; each
  slot repeats the original `seq`, `len`, payload layout so slot zero remains
  protocol-1 compatible. The participant-facing copy stays, so this is *not*
  zero-copy into user code (out of scope per the PRD). Messages fill slots in
  Publish order and only excess Messages use the inline fallback. Each shared-
  memory Message carries `shm_slot` and a per-write `shm_seq` freshness marker;
  every Message states in the Step line how it travelled. A
  receiver never infers that from the channel's declared transport. This keeps
  the arena an optimization that correctness never depends on, and is why the
  transport can stay invisible to participant code. Protocol 2 is offered on
  `init` when any participating Channel has more than one slot. The child echoes
  support on `ready`; an absent echo means protocol 1, and the kernel then uses
  only slot zero over the same Manifest. Slot allocation resets only after the
  synchronous Step response, when every slot written by the kernel has been
  consumed. A participant that both subscribes and publishes the same shared-
  memory Channel remains rejected: the Arena path intentionally has one writer
  and the current `init.channels` shape names only one mapping per Channel;
  lifting this requires separate input and output Arenas, not phase-order
  assumptions in third-party participants. A
  run that cannot create or map an Arena is an environment/Manifest error (exit
  2), distinct from a test failure (exit 1). The transport never reaches
  participant code — the step API is the same field-dict/`bytes`/`list` either
  way — and native participants are unaffected, staying on the pointer-based
  C ABI data plane. Boundaries (out of scope): native-participant shm beyond
  that pointer ABI, cross-machine transport, compression, and arena-size or
  backpressure tuning. The complete line and payload contract is specified in
  `docs/step-protocol.md`.
- **Compiled interceptor plan (#40):** the manifest loader compiles each
  channel's declared interceptors into an encapsulated plan. It resolves kind tags,
  window bounds, mutable `drop_nth` counters, and override offsets plus
  little-endian bytes before the run. The plan's sole runtime entry point is
  `apply(now_ns, bytes)`, with fixed ordering: drop/drop_nth, delay, the
  half-open `[0, duration)` truncation, then override. The engine retains
  ownership of global and channel publish order, including the rule that a
  suppressed message advances only the global order. Focused arithmetic is
  covered by a CTest target; run-boundary tests remain the source of truth for
  routing and recording behavior. Override constants retain JSON's unsigned,
  signed, or floating representation until compilation: integer fields require
  an integral value within their exact Schema range, while floating fields
  accept integer or floating JSON numbers only when finite, and f32 also
  requires magnitude no greater than the largest finite binary32. A floating
  JSON number is parsed as binary64; an integer JSON number is converted once
  to binary64 and may round beyond its 53-bit exact-integer range (for example,
  9007199254740993 becomes 9007199254740992). An f64 stores that binary64 value;
  an f32 narrows it once more to IEEE-754 binary32. Both are emitted as
  pre-encoded little-endian bytes, so runtime application remains one bounded
  byte replacement without lookup or numeric conversion.
- **Native Channel declarations (#49):** a native participant declares its
  Channel contract in the manifest — `subscribes` and `publishes` lists, the
  same shape a process participant already had — and both are always emitted
  by the builder, so the contract is covered by the manifest hash. The C ABI
  gains no registration call: the manifest is authoritative and the runtime
  `subscribe`/`publish` calls only prove conformance to it. This makes every
  live publisher known before any participant is loaded or spawned, so the
  replay/live-publisher collision check runs once, over native and process
  declarations alike, and names the conflicting publisher. A repeated channel
  within one declaration is rejected at load, for every participant type: it
  would otherwise silently double a participant's delivery or production. At
  runtime, subscribing to an undeclared input is a config error (exit 2) and
  publishing an undeclared output aborts the run (exit 1); both diagnostics
  name the participant, the channel, and the declared direction. Live-publisher
  cardinality is untouched — two live publishers on one channel stay legal, and
  that policy belongs to #64. Native `subscribes` and `publishes` are required:
  pre-#49 declarations had no contract enforcement at all, and that behavior is
  gone, so an absent list must fail at load rather than load as an empty contract
  and abort after participants are up. Process participants remain tolerant of
  absent lists because absent-means-empty genuinely was their prior behavior.
- **Overflow-safe Virtual-time addition (#50):** Virtual time is unsigned and
  only ever advances, so every addition to it goes through one primitive,
  `sil::virtual_time_after`, which answers with nothing when the instant
  cannot be represented. Each of the three sites reads that as its own end of
  the line, refining the decisions above. **Scheduling (#5):** a Task whose
  next Activation is unrepresentable, or falls at or beyond the half-open Run
  Duration, is complete; its `next_ns` stays at the Activation just executed,
  so Slot selection never sees an earlier instant than the one it left. A
  period of zero remains a configuration error at registration.
  **Delivery (#8):** a Message whose post-Latency visibility is
  unrepresentable is never delivered. A visibility at or beyond the Run
  Duration needs no separate test — a Slot only opens for an Activation or a
  replay timestamp below the Duration — so the predicate stays a pure
  comparison against the current Slot. **Interceptors (#40):** delays keep
  their saturating semantics by reading the empty result as UINT64_MAX, where
  the plan's existing Run-Duration truncation suppresses the Message.
  Recording timestamps stay post-Interceptor, never subscriber-specific
  visibility times. Unchanged Manifests keep byte-identical Recordings.
- **Manifest and Step-protocol evolution policy (#62):** one Manifest hash
  means one Run semantics. A hash may map to a contextual rejection, but it
  must never map to two different *successful* Runs across kernel versions.
  Every new hash-covered field therefore takes one of three shapes, chosen by
  a single test — does the prior kernel behavior still exist, and is it the
  default the builder wants? **Omit-default:** the field's default is exactly
  the prior behavior and the builder wants that default, so the builder omits
  it and existing documents keep byte-identical hashes (`epoch_ns`,
  `transport`, `shim`). **Always-emit with a legacy-absent default:** the
  builder's default differs from the prior behavior, but the prior behavior
  still exists, so the builder always emits the field and the loader reads an
  absent field as the legacy behavior; existing documents keep their hashes
  and their semantics, while regenerated ones get a new hash that states the
  new default. **Required:** the prior behavior no longer exists, so no
  default can preserve it and the loader fails at load naming the field —
  rejecting an old document rather than reinterpreting it. There is no strict
  mode: a kernel switch outside the Manifest would let one Manifest produce
  two behaviors, which decision 12 forbids. The Python builder is the strict
  authoring path and the kernel loader is the compatible path; that asymmetry
  is what makes the always-emit shape safe. **Manifest version 2** is required
  only when a legacy-absent default is removed or an existing field changes
  meaning, never for an additive field, so none is planned. The `sil_manifest`
  integer plus closed objects already stop a version-1 kernel from misreading
  a later document. **Applied to the open work:** Native `subscribes` and
  `publishes` (#49) are *required* — pre-#49 declarations had no contract
  enforcement at all, and that behavior is gone, so an absent list must fail
  at load instead of loading as an empty contract that aborts after the
  participants are up. Process participants stay tolerant of absent lists,
  because absent-means-empty genuinely was their prior behavior. The Clock
  shim `sleep` policy (#52) is always-emit: the builder defaults to `reject`
  and also accepts an explicit `immediate`, so compatibility is expressible in
  hashed bytes, and an absent field keeps today's immediate-success behavior.
  Bounded-route `capacity` and `overflow` (#75, split from #55) are
  always-emit; a route with both fields absent is unbounded, while a partial
  pair is invalid. The Clock shim's CPU-time pass-through (#76) takes none of
  the three shapes: it corrects shipped behavior back to this document's
  recorded decision rather than declaring a field, on the grounds recorded
  with that decision above. Removing the legacy unbounded behavior later is the one
  change that would trigger version 2. The superseded BufferPool
  transport (#58) would have
  taken a new transport name. Its replacement (#74) instead extends `shm` with
  always-emitted `slots`; absence retains the original single-slot behavior and
  hash.
  **Step protocol:** the `init` line gains a `protocol` integer, derived from
  the Manifest's transport set and so covered by the Manifest hash
  transitively; a child may echo it on `ready` and an absent echo means 1. A
  child below a required semantic protocol level is a setup failure (exit 2).
  Multi-slot Arena support is an optimization-only exception: protocol 1
  negotiates down to slot zero, preserving identical Run semantics. The per-
  Channel `transport` in `init` distinguishes the payload representation at
  run time. **Support floor:** the inline Step transport and
  the copied Native ABI v1 data plane are supported for the life of Manifest
  version 1; the leased data plane does not deprecate them. **Expected
  consequence:** each always-emit field changes the hash of every regenerated
  Manifest once. That is correct — the declared semantics did change — and CI
  matrices should expect it.
- **Bounded subscriber routes (#75):** each newly authored `subscribes` entry
  is an object naming its `channel`, a positive Message `capacity`, and an
  `overflow` policy. The builder requires capacity rather than guessing one
  for a workload and always emits both policy fields; `overflow` defaults to
  `fail`. The loader accepts both the pre-#75 string entry and a route object
  with neither policy field as unbounded; it rejects an object containing only
  one of the two fields. An existing Manifest therefore keeps both its exact
  bytes and its successful Run semantics. A full bounded route fails the Run
  with Channel, publisher, subscriber, capacity, current depth, and policy in
  the diagnostic, or explicitly `drop_newest`s that delivery. Blocking is rejected at load:
  the sequential scheduler cannot activate the consumer from inside a blocked
  publication. Capacity is checked after Interceptors, so a suppressed Message
  still consumes its global Publish order but no route slot. Live and replay
  publication meet at the same fan-out path. Current depth, high-water depth,
  drop count, and overflow-failure count exist only in the separately compiled
  test-instrumented runner; #63 still owns any external metrics surface.
- **Exception containment at the C ABI seam (#65):** the seam runs in both
  directions and neither carries an exception. A kernel frame the participant
  calls into must not throw back across the ABI — the participant may be built
  against a different C++ runtime, so an unwind across the boundary is
  undefined — and participant code the kernel calls into must not unwind kernel
  frames. Every `sil_api_v1` service call that can allocate,
  `sil_participant_init`, and every registered Task activation therefore catches
  both `std::exception` and anything else, and records it through one `noexcept`
  helper that builds the diagnostic inside its own guard. The two exceptions are
  `now_ns`, which reads one member and carries no error value to report through,
  and `fail` itself, which can only discard: it is already the reporting path. Containment is a safety
  net, not a diagnostic: `fail` keeps the *first* failure, so a participant that
  said why it is stopping keeps its own reason and the throw only stops it. An
  init throw is a setup failure (exit 2) naming the Participant; a Task throw
  aborts the Run (exit 1) naming the Participant and the Task. Recording the
  failure rather than rethrowing is what keeps the Participant in the Engine so
  its library is still closed, and what lets the scheduler leave its in-Task
  state normally. This is containment only: no ABI revision, no prefix
  negotiation, no lifecycle states (those stay with #51).
