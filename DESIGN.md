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
None open.

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
  MCAP is the only v1 format, an unrecognized extension is a Manifest error
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
  runtime, subscribing to an undeclared input is a Manifest error (exit 2) and
  publishing an undeclared output aborts the run (exit 1); both diagnostics
  name the participant, the channel, and the declared direction. Live-publisher
  cardinality was left untouched here and decided separately in #64 below,
  which folded this collision check into one publisher rule. Native
  `subscribes` and `publishes` are required:
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
  period of zero remains a Manifest error at registration.
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
  test-instrumented runner. #63, decided below, keeps them there: it is the
  whole counter surface, with no export path and no stability promise. The
  route diagnostic above is the part that stays always on, because it is what
  repairs the Manifest that failed.
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
- **Channel publisher cardinality (#64):** a Channel has at most one publisher.
  The baseline this replaces is that several live Participants could publish
  one Channel, ordered deterministically by Task order and global Publish
  order. Determinism is therefore not the reason to restrict it — both
  policies are deterministic. Ownership is. Every publisher already declares
  its outputs in the Manifest, native, process, and replay participants alike,
  so one name-sorted pass over the declarations rejects a second publisher
  before any Participant is loaded or spawned, and a Channel's publisher
  becomes a static fact of the Manifest. Three things follow. Open-loop replay
  stops being a special case: a replay participant *is* the publisher, so the
  bespoke replay-versus-live collision check is now the same
  duplicate-publisher check, and replay versus replay, which that check never
  covered, is closed by the same rule. Message provenance derives from the
  Manifest alone, so no per-Message publisher field is ever needed to
  attribute one. And an Interceptor, a route-capacity diagnostic, and a fault
  window each name exactly one publisher. The rule is *at most* one, not
  exactly one: a declared but undriven Channel still loads. The diagnostic
  names both participants and their kinds, because a replay/live collision and
  a live/live collision are repaired differently even though one rule now
  finds both. **That merge has a cost to remember:** the replay/live safety
  rule is now a corollary of cardinality, so relaxing cardinality later would
  silently take the safety check with it and must restore it separately.
  **Compatibility (per #62): a rejection, not a declared field.** #62 governs
  new hash-covered fields; this adds none. A Manifest that declares two
  publishers now fails at load with exit 2, which is the mapping #62
  explicitly allows — one hash may map to a contextual rejection, and only two
  different *successful* Runs are forbidden. No Channel field, no hash change,
  no document to regenerate, and no way to turn the rule off: a kernel switch
  outside the Manifest would let one Manifest produce two behaviors, which
  decision 12 forbids. This is not the #76 shape, despite both being
  fieldless. #76 corrected a behavior this document had never recorded as
  supported; here the previous behavior *was* recorded as legal, in the #49
  entry above, and is being withdrawn deliberately. Declaring cardinality per
  Channel instead was rejected as configurability without a use case: it would
  spend a hash-covered field to keep a topology nothing in the project needs.
  **What it forfeits.** The Interceptor kinds are `drop`, `drop_nth`, `delay`,
  and `override`; none injects. So a test Participant can no longer add a
  phantom Message to a Channel the system under test also publishes. The
  natural replacement would be an `inject` Interceptor kind rather than a
  second publisher, because injection at the publish choke point keeps the
  Interceptor ordering and suppression contract that a second publisher
  bypasses — a direction, not yet a scoped decision. Bus-style fan-in, several
  ECUs on one diagnostic or log Channel, becomes one Channel per ECU plus a
  bus Participant, which native bus emulation in the core already being a
  non-goal points to.
- **Metrics surface for pool and route counters (#63):** there is no external
  metrics surface. Every counter #54, #55, and #59 ask for is test
  instrumentation in `sil-run-instrumented`, reported as one JSON object to the
  path in `SIL_COPY_COUNTERS_OUT` after the Run. The production runner writes
  exactly one artifact, the Recording. **The classification rule is not
  per-counter but per-purpose.** A value needed to *repair the configuration
  that failed* is a **diagnostic**: always on, in the production runner, named
  in the failure message at the failure site, owned by the code that fails the
  Run. #75's overflow abort naming Channel, publisher, subscriber, capacity,
  current depth, and policy is the model. A value that is merely
  **observational** is a counter, and lives only in the instrumented runner.
  Nothing is an external contract. **The reason is determinism, not absence of
  a consumer.** A consumer does exist: a bounded route (#75) aborts a
  production CI Run, and the person setting capacities wants the high-water
  depth of *every* route, not only the one that overflowed. The usual argument
  for always-on telemetry — that the failure cannot be reproduced — is the one
  argument this project does not have. The same Manifest hash on the same
  machine class re-runs exactly, so `sil-run-instrumented` answers that
  question for the price of one Run, and every other Run keeps a routing path
  with no counter on it. That is why the production path carries no counter,
  no branch, and no reporting code, and why a second binary is the shape rather
  than a runtime switch. **A `--metrics <path>` flag is rejected**, despite
  looking like the precedented `-o` / `--no-recording` run-boundary switch: a
  Recording writes what the Run *computed*, while a metrics flag adds code to
  the *computing*. Metrics on stderr are rejected for mixing observational
  values into the channel that carries the authoritative failure.
  **Owner and lifetime:** each counter is owned by the subsystem that owns the
  resource it counts — the BufferPool for capacity, free slots, high-water
  slots, allocation failures, outstanding leases, and stale-handle attempts
  (#54); the route for current depth, high-water depth, drop count, and
  overflow failures (#75, shipped); the Process-participant ownership registry
  for outstanding leases by Participant, abandoned writes, forced releases,
  forced crash reclamations, and stale post-crash descriptor attempts (#59).
  Lifetime is one Run: a counter is never reset mid-Run and is reported once,
  at exit, from a static destructor after every other kernel object is gone.
  **Wall-clock values cannot enter a deterministic counter.** #55's oldest
  outstanding lease age per route is dropped rather than reclassified: under
  FIFO with no cross-step leases it collapses to the queue depth already
  reported. #59's oldest lease age is kept but defined as a virtual-time delta
  from the publication time #54 specifies for the envelope — deterministic,
  and therefore assertable. The report's wall-clock and RSS
  values remain, separated from the counters as observational, so repeated-run
  expectations compare the deterministic counters wholesale and never the
  timings. **Nothing from the counters reaches the Recording**, not even an
  MCAP metadata record: the report carries rusage, and one metadata record
  would break bit-identity. **Failure and partial Runs:** the exit code and the
  first stderr diagnostic stay authoritative. The report is written after
  `main` returns and can change neither; a report that cannot be written says
  so on stderr and still cannot change the exit code; a hard crash writes no
  report at all, which is unambiguous. The report states the Run's exit code so
  a partial report from a failed Run cannot be read as a clean one.
  **The re-run argument is checked, not claimed (#82).** Every determinism
  fixture in `tests/test_determinism.py` — a clean pipeline, a dropping bounded
  route, a bounded-route overflow abort, and a participant failure — runs under
  both runners and must produce the same exit code and a byte-identical
  Recording. That byte-identity is also what keeps every counter out of the
  Recording, including an MCAP metadata record. The report itself is three
  members: `run_exit_code`, a `deterministic` subtree of counters and route
  state that a repeated-run test compares wholesale, and an `observational`
  subtree holding the wall-clock and RSS values that vary. This turns #46's
  "counters exist, while remaining inert to functional execution" into a
  checked invariant rather than a comment in `CMakeLists.txt`. **No
  stability promise** attaches to the report: field names, nesting, and the
  `SIL_COPY_COUNTERS` name itself may change with the counters they describe,
  because the tests, `tools/bench_routing.py`, and `docs/bench/` are its only
  consumers and all three change together. Deferred, not decided against: an
  exported metrics contract becomes a question again if a Run ever stops being
  exactly reproducible, or if a fleet-level consumer appears — which
  run-fleet orchestration and a result database being explicit non-goals makes
  unlikely in v1.
- **Handle fan-out declined (#85, closing #54, #55 and #57):** one immutable
  payload routed to many subscribers by handle, rather than copied per route,
  is not built. The gate #55 carried after the #61 re-scope accepted two forms
  of evidence — a named workload with six or more subscribers on a sensor
  Channel, or one where peak memory binds. **The memory half is answerable
  without a workload, and the answer is no.** Since #75 every newly authored
  route declares a capacity, so worst-case route memory is arithmetic over the
  hashed Manifest rather than a measurement: capacity times payload size,
  reported by `python -m sil.footprint`. Inverting it asks how much fan-out
  memory would take to bind. At capacity three, the depth the baseline's
  never-draining subscriber actually held, one subscriber costs 7.91 MiB on a
  720p RGB8 frame, 17.80 MiB on 1080p, 71.19 MiB on 4K, and 12.00 MiB on a
  128-by-2048 lidar sweep. This project's CI runner has 7 GB, so binding it
  needs 906, 402, 100 and 597 subscribers respectively. At the six the gate
  itself names, the worst case is 47 MiB to 427 MiB — between 0.7% and 6% of
  the machine. The constraint the feature relieves misses by one to two orders
  of magnitude, and the CPU half was already weak: Recording one camera frame
  costs about nineteen subscriber copies, so removing per-subscriber copies
  stays noise while Recording is on. **The subscriber half is not answerable
  and must not be manufactured.** It requires a named integration, and this
  project has none. Authoring a Manifest with enough subscribers to open the
  gate would choose the number that justifies the feature, which is the
  reasoning #61 exists to prevent; a gate that can only ever open is not a
  gate. **What stays.** The copied publish and take path and ABI v1 remain the
  supported data plane, per the support floor recorded with #62 — this decision
  removes nothing that exists. Bounded routes (#75) already deliver #46's
  deterministic bounded-capacity criterion, and multi-slot Arenas (#74) already
  deliver its burst criterion, both without leases. #57 closes with #55 because
  leased Native operations have nothing left to expose. **Reopening.** #85
  holds the record and the arithmetic. A real integration with six or more
  subscribers on a sensor Channel reopens it, as does a payload and fan-out
  combination that genuinely binds — which the numbers above make easy to test
  before reopening anything. **Expected consequence:** #46's fan-out and
  zero-copy-routing criteria are withdrawn rather than left undelivered, and
  the epic keeps the correctness track it always had.
- **Native ABI revision declined (#86, closing #51):** no second revision of the
  Native participant C ABI is built. #51 asked for compatible-prefix
  negotiation, explicit instance lifecycle, multiple-instance support, capability
  discovery, and destruction, and was explicit that it was not ready as one
  slice; #86 gated it on naming the consumer that justifies it. All three
  candidates the gate listed are rejected, for three different reasons.
  **Multiple instances of one shared library already work in ABI v1.** The
  kernel builds one `NativeParticipant` per Manifest entry and passes
  `api.ctx = this`, from a `sil_api_v1` that is a member of that object, into an
  `sil_participant_init` that also receives that entry's own `name` and
  `config`; nothing in the builder or the loader makes a library unique to one
  Participant. A Participant that keeps its state behind the `user` pointer it
  registers therefore gets two independent instances today, and the handle #51
  wanted to add is `ctx`. What is missing is a fixture and a documented rule,
  not an ABI: every toy in `participants/` held one global instance, so two
  entries on one library would run without any diagnostic while the second
  `init` overwrote the first one's state and both Tasks published through the
  second context. That fixture defect is closed by #88. **Repeated lifecycles
  in one process are a much larger job than the ABI part, and one this design
  argues against.** The candidate was an in-process embedding running many
  Manifests without spawning `sil-run` for each, which is what would genuinely
  require creation, start, reset, stop, and destruction states. But there is no
  kernel library form and no binding — the suite shells out to the `sil-run`
  binary — so the ABI revision is the small tail of building one, and the stated
  payoff is weak: the expensive spawns in a Run are the Python Step
  participants, and an in-process kernel keeps every one of them. Against that,
  reusing one process across Runs means `dlopen`/`dlclose` cycles over supplier
  libraries whose static initializers this project does not control, which is a
  determinism hazard in the one place determinism is the product, and decision
  15 already fixes the CI unit at one Run per container. **No first optional
  capability exists to discover.** #57 was the only candidate and closed with
  the leased data plane under #85, so an extension mechanism designed now would
  be a guess at the shape of something withdrawn. **What stays.** ABI v1, its
  copied publish and take path, and the support floor recorded with #62 are
  unchanged — this decision removes nothing and declares no field, so it needs
  no compatibility shape. Exception containment, the one real defect #51
  carried, is delivered by #65; the declared Channel contract #51 would have
  consumed at instance setup is delivered by #49. **Reopening.** #86 holds the
  record. A supplier Participant that cannot keep its instance state behind
  `user`, a named embedding that must run repeated Runs in one process, or a
  concrete optional capability reopens it — and the first of those should be
  tested against #88's fixture before it reopens anything. **Expected
  consequence:** #46's ABI criterion is amended to what #49 and #65 delivered,
  with multiple-instance support recorded as ABI v1 behavior rather than
  withdrawn.
- **Several Participants per shared library (#88):** one shared library backs
  any number of Participants in one Run, on ABI v1 unchanged. This is what #86
  asserted and did not prove. The kernel already built one `NativeParticipant`
  per Manifest entry and passed it as `api.ctx`, alongside that entry's own
  `name` and `config`; what was missing was a Participant that kept its state
  where that shape requires — behind the `user` pointer it registers,
  allocated from its own `config_json` — and a Run that exercised two of them.
  `toy_producer` and `toy_accumulator` now allocate per `init`, and a
  run-boundary fixture declares two Manifest entries on one library with
  different Channels and periods, asserting each Participant's own Channel,
  its own timestamps, and its own sequence counter. The rule is stated at
  `sil_participant_init` in `include/sil/participant.h`, together with its
  consequence: a library that keeps state in a global supports at most one
  Participant per Run, and the kernel does not diagnose a second — the earlier
  Participant's declared output is simply never published. That silence is the
  reason the rule needs a fixture rather than a check. `toy_thrower` keeps its
  global, because one Participant is all a containment fixture needs, and says
  so against the rule. **Destruction is untouched:** a Participant's state
  lives until the process exits, which is the whole lifecycle ABI v1 has and
  #86 declined to extend.
- **First environment reference adapter (#96):** the internal dynamics model.
  `python/src/sil/examples/acc/plant.py` carries the longitudinal motion of two
  vehicles and
  is stepped over the same step protocol as any other process participant —
  an adapter in exactly the sense #10 means, with nothing simulator-shaped
  added to the kernel to carry it. esmini is deferred, not rejected: the
  question was which comes first, and the internal model came first because it
  can live in this repository, be imported by the example's own tests instead
  of restated by them, and be held to the determinism gate on every push. The
  example is also the evidence — its second variant delays the sensing Channel
  by one declared Interceptor and the vehicles drive a different trajectory for
  it, so the environment half is a closed loop and not a playback.
