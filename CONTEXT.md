# SiL

A deterministic software-in-the-loop kernel for ADAS/AD regression testing. It
does two things — deterministic scheduling and typed data routing — so that the
same artifacts on the same machine class always produce bit-identical output.

## Language

### Time and scheduling

**Virtual time**:
The only clock the simulated world has: nanoseconds since the start of a run,
advanced by the kernel, never by the OS.
_Avoid_: sim time, simulated time, model time, wall clock

**Slot**:
One instant of virtual time at which at least one activation is due. All work
in a slot happens at the same timestamp.
_Avoid_: tick, cycle, frame, timestep

**Activation**:
A single execution of one participant's code inside a slot.
_Avoid_: invocation, callback, trigger, fire

**Task**:
A unit of periodic work a native participant registers, with a period, an
offset, and an order-priority the kernel uses to order activations.
_Avoid_: job, thread, runnable

**Step**:
The activation of a participant that is opaque to the kernel, carrying the
current virtual time `t` and the elapsed `dt` since its previous step.
_Avoid_: tick, update, advance

**Duration**:
The length of virtual time a run covers, after which the kernel stops.
_Avoid_: runtime, horizon, end time

**Period**:
The elapsed virtual time between successive periodic activations of a
participant. It is the reciprocal of frequency; use period when describing the
time between activations.
_Avoid_: rate, interval when naming a participant's recurring schedule

**Observation grid**:
The authored set of virtual times at which a trajectory is compared. It is a
comparison policy, not a participant's Period and not a resampling operation.
_Avoid_: sample rate, interpolation grid, timestep

### Participants

**Participant**:
Anything the kernel schedules and routes messages to or from. Every actor in a
run — production code, environment model, replayer, test — is one.
_Avoid_: node, component, module, actor, block

**Native participant**:
A participant compiled as a shared library against the stable C ABI and run
in the kernel's own process, registering tasks and subscriptions at init.
_Avoid_: plugin, library, in-process module

**Process participant**:
A participant that runs as a separate executable and is driven over the step
protocol.
_Avoid_: external participant, subprocess, worker

**Process group**:
The kernel-created POSIX process group led by one Process participant's child.
It bounds the lifetime of cooperative descendants during Run cleanup; it is a
lifetime boundary, not a sandbox or a resource quota.
_Avoid_: sandbox, container, worker group

**Replay participant**:
A participant that publishes messages from an existing recording as stimulus,
at their recorded virtual times.
_Avoid_: player, source, injector

**Test participant**:
A participant whose job is to publish stimuli and evaluate assertions at
defined virtual times; its failure aborts the run.
_Avoid_: test harness, checker, monitor

**Maneuver**:
An authored time-varying input applied by one or more Participants to exercise
the behavior under test. A Maneuver is part of the reproducible Run contract.
_Avoid_: scenario, stimulus when naming the authored behavior

**vECU**:
A piece of vehicle software under test, brought into a run as a participant.
Opaque when the framework cannot see or change its internals.
_Avoid_: ECU, SUT, model, target

**FMU**:
A vendor model or vECU packaged to the FMI standard: one archive holding a
machine-readable description of its variables and a shared library per
platform. What a supplier or a modeling tool actually hands over.
_Avoid_: FMI model, functional mockup unit, black box

**Importer**:
The adapter that brings a model in a foreign standard into a run as an
ordinary participant, translating that standard's stepping contract to this
one. It lives at the edge; the kernel never learns the standard exists.
_Avoid_: wrapper, bridge, master, co-simulation master

**FMU group**:
Several FMUs one Importer drives inside a single Process participant, connected
to each other by their own standard's terminals rather than by Channels. It
exists where the connection carries something a Channel cannot: an instant the
models compute between two Slots. The kernel sees one participant.
_Avoid_: co-simulation network, federation, cluster, sub-system

**Step protocol**:
The line-based contract over a process participant's stdin/stdout by which the
kernel initializes it, steps it, and shuts it down.
_Avoid_: IPC protocol, RPC, message protocol

**Clock shim**:
The library preloaded into an opaque participant that answers every wall-clock
POSIX read from virtual time, making it deterministic without modifying it.
Such reads are frozen within a step: they only change between steps. Clock IDs
outside the wall-clock classes — the CPU-time IDs and any the shim does not
name — pass through to the real libc.
_Avoid_: clock hook, time patch, LD_PRELOAD hack, interposer

### Data routing

**Channel**:
A named, typed stream of messages that participants publish to and subscribe
to. The only path data takes between participants.
_Avoid_: topic, signal, bus, stream, port

**Publisher**:
The one participant that publishes a channel, declared in its manifest
contract. A channel has at most one, and a replay participant is the publisher
of every channel it replays, so a channel's publisher is a static fact of the
manifest rather than something the run discovers.
_Avoid_: producer, source, writer, owner

**Subscriber route**:
One Participant's bounded FIFO delivery path from one Channel. Its Manifest
declaration owns the queue capacity and overflow policy because subscribers on
the same Channel can have different criticality and drain rates.
_Avoid_: subscriber queue, consumer buffer

**Message**:
One published payload on a channel, laid out exactly as its schema declares.
_Avoid_: sample, frame, event, packet, record

**Schema**:
The declaration of a message's fields and their fixed, packed byte layout —
the single typed contract shared by kernel, participants, and recording.
_Avoid_: type definition, IDL, message format, struct

**Latency**:
The declared delay, per channel, between publishing a message and it becoming
visible to a subscriber. The default is the subscriber's next activation, which
is what makes results independent of execution order within a slot. A declared
zero is the one value that delivers inside the publishing slot, which a
subscriber that has to act on a message before the instant it names depends on.
_Avoid_: delay, lag, deadline

**Publish order**:
The total order in which messages were published across all channels in a run;
the tie-break that keeps same-timestamp messages reproducible.
_Avoid_: sequence, ordering, insertion order

**Transport**:
How a channel's payload crosses the kernel↔process boundary — encoded in the
step line, or through shared memory. Invisible to participant code either way.
_Avoid_: encoding, serialization, wire format

**Mapped region**:
A temp file the kernel maps and shares with a participant process, named to the
child by path. It exists for the length of the run and leaves nothing behind.
An arena is one; the clock shim's time region is another.
_Avoid_: shared memory, mapping, segment, shm

**Arena**:
The mapped region carrying one channel's payloads across the kernel↔process
boundary, with a Manifest-declared number of schema-sized payload slots.
_Avoid_: buffer, segment, ring, shm

**Burst**:
Several Messages published on one Channel inside one Slot. Messages fill the
Channel's declared Arena slots in Publish order; only excess Messages fall back
to the inline transport.
_Avoid_: batch, salvo, backlog

**Interceptor**:
A manifest-declared modification of a channel's message stream over a time
window — dropping, delaying, or rewriting messages. How fault injection is
expressed, so faults stay part of the reproducible configuration.
_Avoid_: fault, filter, hook, middleware, mutator

### Runs and reproducibility

**Run**:
One execution of one manifest from time zero to its duration, producing an exit
code and a recording.
_Avoid_: simulation, session, execution, job

**Manifest**:
The single declarative input to a run: its schemas, channels, participants, and
duration. Nothing outside it influences the result.
_Avoid_: config, scenario, setup, spec file

**Manifest hash**:
The digest of a manifest's exact bytes, archived with every run so results can
be attributed to a manifest and deduplicated.
_Avoid_: config id, fingerprint, version

**Manifest error**:
A run rejected before any participant is stepped, because its manifest — or
the environment that manifest names — cannot be honoured. Exit code 2. Nothing
ran, so there is no behavior to attribute it to.
_Avoid_: config error, configuration error, setup error, startup error

**Run failure**:
A run that started and then went wrong: a participant aborted, a participant's
shutdown reported an error, or a KPI failed. Exit code 1. The distinction from
a manifest error is what was at fault, not when it was caught.
_Avoid_: runtime error, crash, test failure

**Response deadline**:
The wall-clock time one process participant gets to complete a single `ready`
or `step_done` answer. Chosen at the run boundary, never in the manifest, and
started afresh for every request. Missing it is a run failure; meeting it says
nothing about how fast the run was, and a run that answers in time records the
same bytes with or without one.
_Avoid_: step budget, watchdog, latency budget, whole-run timeout

**Run working directory**:
The kernel-owned directory tree for one Run. The kernel creates its root beneath
the runner's invocation directory and gives every process participant its own
directory beneath that root. It starts each child there and removes the entire
tree after reaping the children on both the passing and failing paths.
Run-scoped files a participant needs on disk — an imported FMU's extracted
archive, say — live in its directory. Generated names are runtime details and
never part of the Manifest or Manifest hash.
_Avoid_: cwd, temp dir, scratch dir

**Recording**:
The output artifact of a run: every published message with its virtual
timestamp, plus the schemas and manifest hash needed to interpret it. Whether
a run writes one, and where, is chosen at the run boundary rather than in the
manifest, so the same manifest hash covers a run with and without one. What the
run computes is identical either way; only the artifact differs.
_Avoid_: log, trace, output file, mcap

**KPI**:
A property of a Run's behavior that decides whether it passed. It is evaluated
in-run by a test participant at defined virtual times, post-hoc over a
recording, or both — the same property, measured on the live Run or on its
artifact.
_Avoid_: metric, score

**Determinism**:
The guarantee that the same artifacts on the same machine class produce a
bit-identical recording. Scoped deliberately: cross-platform bit-exactness is
not claimed.
_Avoid_: reproducibility, repeatability, stability

**Determinism check**:
Running one manifest twice and bit-comparing the recordings — the mechanism
that makes a determinism violation visible rather than assumed away.
_Avoid_: repro test, bit-diff test, sanity run

**Reference result**:
The trajectory an FMU ships for its own default experiment, declared inside
the archive under the FMI-LS-REF layered standard. Checking a recording
against it answers what the determinism check cannot: whether the importer is
correct, not whether the run reproduces. A tolerance check and never a bit
comparison — the vendor produced it on another machine class.
_Avoid_: golden file, expected output, baseline, ground truth

**Sensitivity envelope**:
A predeclared bound on observed differences in a sensitivity experiment. It
states what deviations the experiment accepts; it is not fitted from the
results and does not imply monotonic convergence.
_Avoid_: tuned tolerance, convergence bound

**Acceptance bundle**:
The pinned artifacts one preparation step produces — models, audits, authored
configurations, independently recorded trajectories — with a digest for each
one. Every later acceptance Run consumes that bundle offline and adds nothing
to it, so a Run is attributable to exactly the artifacts it was prepared with.
_Avoid_: release, package, test data, fixtures

**Example image**:
The production runtime image plus the consumer material of one adoption
example. It carries the model's runtime dependencies and no exporter, no
independent importer and no toolchain, so what an acceptance Run exercises is
what an adopter installs.
_Avoid_: test image, dev image, all-in-one image

**Archive reproducibility**:
Whether one exporter produces the same archive twice — the same bytes, the
same member timestamps and the same instantiation token. Judged during
preparation, separately from the determinism check, because an archive that
changes identity between controlled builds makes every later comparison
meaningless whether or not the Runs are deterministic.
_Avoid_: reproducible build, deterministic build, stable artifact
