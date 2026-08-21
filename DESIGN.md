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
  `clock_getres` (reports 1 ns), and the sleep family (`nanosleep`,
  `clock_nanosleep`, `usleep`, `sleep`), which return immediately with success;
  CPU-time clock IDs pass through. **Frozen-step semantics:** every clock read
  during one step returns the same `t`; time advances only between steps.
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
  pre-shm manifests keep byte-identical hashes. The kernel maps one arena per
  (participant, channel) at startup, sized from the schema `byte_size`, as a
  memory-mapped temp file shared with the child; the step line carries only a
  `shm_seq` freshness marker. **Refined from the decision text:** a single-slot
  arena, not a ring buffer, and the participant-facing copy stays — so this is
  *not* zero-copy into user code (out of scope per the PRD). One slot holds one
  payload, and a slower subscriber can see several messages on a channel in one
  step, so the first message rides the arena and the rest fall back to the
  inline encoding. Every message states in the step line how it travelled; a
  receiver never infers that from the channel's declared transport. This keeps
  the arena an optimization that correctness never depends on, and is why the
  transport can stay invisible to participant code. Because one slot carries
  one direction, a participant that both subscribes and publishes the same
  channel over shared memory is rejected at load rather than silently racing
  its own writes. A
  run that cannot create or map an arena is an environment/config error (exit
  2), distinct from a test failure (exit 1). The transport never reaches
  participant code — the step API is the same field-dict/`bytes`/`list` either
  way — and native participants are unaffected, staying on the pointer-based
  C ABI data plane. Boundaries (out of scope): native-participant shm beyond
  that pointer ABI, cross-machine transport, compression, and arena-size or
  backpressure tuning.
- **Compiled interceptor plan (#40):** the manifest loader compiles each
  channel's declared interceptors into a private plan. It resolves kind tags,
  window bounds, mutable `drop_nth` counters, and override offsets plus
  little-endian bytes before the run. The plan's sole runtime entry point is
  `apply(now_ns, bytes)`, with fixed ordering: drop/drop_nth, delay, the
  half-open `[0, duration)` truncation, then override. The engine retains
  ownership of global and channel publish order, including the rule that a
  suppressed message advances only the global order. Focused arithmetic is
  covered by a CTest target; run-boundary tests remain the source of truth for
  routing and recording behavior.
