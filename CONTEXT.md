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

**Replay participant**:
A participant that publishes messages from an existing recording as stimulus,
at their recorded virtual times.
_Avoid_: player, source, injector

**Test participant**:
A participant whose job is to publish stimuli and evaluate assertions at
defined virtual times; its failure aborts the run.
_Avoid_: test harness, checker, monitor

**vECU**:
A piece of vehicle software under test, brought into a run as a participant.
Opaque when the framework cannot see or change its internals.
_Avoid_: ECU, SUT, model, target

**Step protocol**:
The line-based contract over a process participant's stdin/stdout by which the
kernel initializes it, steps it, and shuts it down.
_Avoid_: IPC protocol, RPC, message protocol

**Clock shim**:
The library preloaded into an opaque participant that answers every POSIX clock
read from virtual time, making it deterministic without modifying it. Reads are
frozen within a step: they only change between steps.
_Avoid_: clock hook, time patch, LD_PRELOAD hack, interposer

### Data routing

**Channel**:
A named, typed stream of messages that participants publish to and subscribe
to. The only path data takes between participants.
_Avoid_: topic, signal, bus, stream, port

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
is what makes results independent of execution order within a slot.
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
boundary, sized from the schema.
_Avoid_: buffer, segment, ring, shm

**Burst**:
Several Messages published on one Channel inside one Slot. An Arena holds one
payload, so every Message after the first falls back to the inline transport.
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
be attributed to a configuration and deduplicated.
_Avoid_: config id, fingerprint, version

**Recording**:
The output artifact of a run: every published message with its virtual
timestamp, plus the schemas and manifest hash needed to interpret it.
_Avoid_: log, trace, output file, mcap

**Determinism**:
The guarantee that the same artifacts on the same machine class produce a
bit-identical recording. Scoped deliberately: cross-platform bit-exactness is
not claimed.
_Avoid_: reproducibility, repeatability, stability

**Determinism check**:
Running one manifest twice and bit-comparing the recordings — the mechanism
that makes a determinism violation visible rather than assumed away.
_Avoid_: repro test, bit-diff test, sanity run
