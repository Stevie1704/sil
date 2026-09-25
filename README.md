# SiL Framework

Deterministic software-in-the-loop kernel for ADAS/AD regression testing:
**deterministic scheduling + typed data routing**, nothing else in the core.
Same artifacts + same machine class ⇒ bit-identical recordings. See
[DESIGN.md](DESIGN.md) for the decision record.

Apache-2.0 licensed. Read [SUPPORT.md](SUPPORT.md) before you build a process
on SiL: it names the supported machine class, the exact bound on the
determinism guarantee, and the fact that SiL is not safety-qualified.

## Milestone 1 — walking skeleton + determinism proof

One run = `sil-run manifest.json -o out.mcap`:

- **Kernel** (C++20): central virtual-time master, two-tier scheduling
  (periodic tasks for native code, `step(t, Δt)` for opaque vECUs), typed
  pub/sub channels with explicit per-channel latency (default: unit delay —
  in-slot execution order cannot change outputs).
- **Native participants**: shared libraries against a small stable C ABI
  ([include/sil/participant.h](include/sil/participant.h)), loaded via the
  manifest.
- **Out-of-process participants**: any executable speaking a JSON-lines
  step protocol on stdin/stdout; `sil.participant` provides the Python side.
  The normative line and payload contract is in
  [docs/step-protocol.md](docs/step-protocol.md).
- **Recording**: uncompressed MCAP, virtual timestamps only, manifest hash
  embedded; bit-diff of two runs = determinism check.
- **Manifest**: canonical JSON, SHA-256 hashed, the single execution input.
  Built and validated with the `sil.manifest` Python builder.
- **Test API**: tests are scheduled participants (`sil.testing` +
  `sil.participant`) run under pytest; an in-simulation assertion failure
  aborts the run non-zero and fails the pytest test with that message.
- **Determinism check**: `sil-check manifest.json --runner build/sil-run`
  runs twice and bit-compares (exit 3 on violation). Enforced in CI.
- **Declared memory footprint**: `python -m sil.footprint manifest.json` reports
  the worst-case payload memory a manifest promises, from route capacities and
  arena slot counts alone — no run, no benchmark. It also names any route that
  declares no capacity, whose worst case is unbounded.

Exit codes: `0` ok, `1` run/test failure, `2` Manifest error, `3` determinism
violation (sil-check).

For a bounded CI wait, add the optional run-boundary guard:

```sh
sil-run manifest.json --participant-timeout-ms 5000 -o out.mcap
```

The value is a positive wall-clock duration in milliseconds. It applies
independently to each Process participant's complete `ready` and `step_done`
response wait. A missed deadline is a Run failure (exit 1), not a Manifest
change: the option is absent from the Manifest and its hash. Omitting it keeps
the existing unlimited-wait behavior.

The determinism check takes the same option and hands it to both of the runs
it compares, so a stalled participant fails the check instead of hanging it:

```sh
sil-check manifest.json --runner build/sil-run --participant-timeout-ms 5000
```

The check then reports the run's own exit code and diagnostic — a missed
deadline is a run failure (exit 1), not a determinism violation. The
recorded bytes of a run that answers in time are the same either way, so a
bounded check and an unbounded one report the same digest.

### Process participant descendants

Each Process participant becomes the leader of its own process group before it
starts. At Run shutdown, the kernel gives the group the normal SIGTERM grace
period and escalates the group to SIGKILL if needed, including when the direct
child has already exited. This bounds the lifetime of cooperative descendants
and releases the participant's Run working directory, Arenas, and protocol
resources with the Run.

The group is a lifetime boundary, not a sandbox: it adds no CPU or memory
quota, syscall filter, namespace, or protection against a descendant that
deliberately escapes the group. Use the [container Run](#run-one-manifest-in-a-linux-container)
(issue #115) for stronger isolation.

SIGINT, SIGHUP, and SIGTERM interrupt the Run through the same failure and
group-cleanup path, so terminal or service shutdown does not orphan
participants.

The runner also has independent Step-protocol resource guards:

```sh
sil-run manifest.json \
  --max-protocol-line-bytes 16777216 \
  --max-step-output-messages 1024 \
  --max-step-inline-payload-bytes 67108864 \
  -o out.mcap
```

These positive `size_t` values default to 16 MiB per response line, 1,024
output Messages per Step, and 64 MiB of decoded inline payload per Step. They
are run-boundary arguments, not Manifest data: a Run that stays within them
has the same Manifest hash and Recording bytes. An over-limit Process
participant causes a Run failure (exit 1).

`--max-protocol-line-bytes` is the one to turn down for a tighter memory
ceiling. It bounds the read buffer exactly, but the kernel parses a line that
stays inside it before it can count that line's outputs, and the kernel's
peak resident memory runs to 13-16 times the line limit in the worst case. The inline
payload of a Step is capped at three quarters of the line limit by base64
expansion, so `--max-step-inline-payload-bytes` only bites once the line limit
is raised. [docs/step-protocol.md](docs/step-protocol.md) has the detail.

## Virtual clock shim for opaque POSIX vECUs

An out-of-process participant is expected to derive time from the `step(t, Δt)`
protocol and never read the wall clock. Opaque binaries you cannot change often
break that rule — they call `clock_gettime`, `gettimeofday`, `time`, or sleep.
The **clock shim** makes such a participant deterministic without touching it:
preload a small library that answers every POSIX wall-clock read from virtual
time.

Opt a process participant in per participant via the manifest `shim` flag, and
set the run's realtime `epoch_ns` (calendar time, ns since 1970) that
realtime-class reads are offset from:

```python
m = Manifest(duration_ns=30_000_000, epoch_ns=1_700_000_000_000_000_000)
m.add_channel("readings", schema="toy.Counter")
m.add_process(
    "vecu",
    command=[sys.executable, "vecu.py"],
    step_period_ns=10_000_000,
    publishes=["readings"],
    shim=True,           # this participant sees virtual time; others do not
    sleep="reject",      # default: fail sleeps instead of faking them
)
```

Inside a shimmed child, per step at virtual time `t`:

- monotonic-class reads (`CLOCK_MONOTONIC`, `CLOCK_BOOTTIME` and their raw,
  coarse and approximate variants) return `t` — nanoseconds from run start;
- realtime-class reads (`CLOCK_REALTIME` and its variants, `gettimeofday`,
  `time`) return `epoch_ns + t`;
- `clock_getres` reports 1 ns for those IDs;
- **every other clock ID passes through to the real libc** — the CPU-time IDs
  (`CLOCK_PROCESS_CPUTIME_ID`, `CLOCK_THREAD_CPUTIME_ID`), which measure
  consumed CPU rather than elapsed wall time, and any ID the shim does not
  name, which has no known class to answer from. A participant that profiles
  itself with a CPU clock reads real CPU time and is not deterministic;
- **virtualized reads are frozen within a step** — every such read during one
  step returns the same value; time advances only between steps.

Because a step's time is frozen and the kernel waits for the step response, no
sleep can wait for the clock to move — it would deadlock against the only thing
that could move it. The `sleep` policy decides what a sleep call says instead:

| `sleep` | The sleep family does | Use it for |
| --- | --- | --- |
| `"reject"` (default) | fails with `ENOSYS` | new manifests: a retry loop ends instead of spinning the CPU for the rest of the step |
| `"immediate"` | returns success, as if the whole duration had elapsed | code that treats a failed sleep as fatal, and manifests written before the policy existed |

`sleep()` is the exception: POSIX gives it no error return, so `reject` reports
the full duration as unslept and sets `ENOSYS` for callers that check it. An
older manifest with no `sleep` field keeps `"immediate"`, so its hash and its
behavior are both unchanged.

The shim applies **per participant**: a shimmed vECU and ordinary unshimmed
participants coexist in one manifest and one run. `shim`, `sleep` and
`epoch_ns` are all hashed into the manifest (they change output), so shimmed
and unshimmed variants of a run never collide under hash-based caching, and two
shimmed runs
started at different wall-clock times still produce bit-identical MCAPs — run
`sil-check` on a shimmed manifest to prove it.

**Boundaries (out of scope, documented):** statically linked binaries and code
that reads the clock via direct syscalls or the vDSO bypass the preload and are
not virtualized.

## Shared-memory channel transport

Every channel payload is base64-encoded inside the JSON step line by default
(inline transport). For megabyte-class payloads at sensor rate — a camera frame,
a point cloud — the ~33% base64 penalty plus per-message encode/decode dominates
the run. Declare `transport: "shm"` on such a channel and its payload crosses the
kernel↔process boundary through a per-channel shared-memory arena instead:

```python
m.add_channel("frames", schema="sensor.Frame", transport="shm", slots=2)
```

The kernel sizes and maps one arena per such channel from `byte_size * slots`
at startup and hands the process participant its path. Publishing writes each
payload into its indexed slot and the step line carries the slot plus a
freshness marker; the participant reads the bytes directly. The builder always
emits `slots` for `shm` Channels and defaults it to 2: that covers the measured
two-Message burst at the cost of one additional schema-sized payload per Arena.
A deeper burst falls back to inline only after filling the declared slots — a
detail of delivery that never changes what the participant sees. **The
transport choice never leaks into
participant code** — `on_step` still sees the same field-dict (scalars) and
`bytes`/`list` (array fields) whether the channel is inline or shm. Flip the flag
and rebuild nothing.

`transport` and `slots` are hashed (inline, the default, is omitted; absent
`slots` means the legacy single-slot behavior). A run that cannot create or map
its arena fails at
**startup with exit 2** (a Manifest/environment problem), distinct from a test
failure's exit 1. Native participants are unaffected — they stay on the existing
pointer-based C ABI data plane and need no rebuild.

**Boundaries (out of scope, per PRD):** true zero-copy into user code (a
participant-facing copy stays), native-participant shm beyond the pointer ABI,
cross-machine transport, compression, and arena-size/backpressure tuning.

## Bounded subscriber routes

Every subscriber route authored by the Python builder has an explicit finite
Message capacity. Declare it with `SubscriberRoute`; a full route aborts the Run
by default, while a non-critical subscriber can explicitly discard only its
newest attempted delivery:

```python
from sil import Manifest, SubscriberRoute

m.add_process(
    "detector",
    command=["./detector"],
    step_period_ns=10_000_000,
    subscribes=[SubscriberRoute("frames", capacity=4)],
)
m.add_process(
    "preview",
    command=["./preview"],
    step_period_ns=20_000_000,
    subscribes=[
        SubscriberRoute("frames", capacity=2, overflow="drop_newest")
    ],
)
```

Capacity and overflow policy are part of the canonical Manifest bytes and hash.
Choose capacity from the route's declared burst and drain behavior; the builder
does not guess a workload-specific number. A Manifest route with both fields
absent is unbounded: the loader accepts either a pre-#75 string entry such as
`"subscribes":["frames"]` or `{"channel":"frames"}`. Supplying only one of
`capacity` and `overflow` is invalid. This preserves existing behavior and
byte-identical hashes. Blocking overflow is invalid because the sequential
scheduler cannot activate a consumer while stopped inside its publisher's call.

## FMI 3.0 co-simulation importer

An FMU is a vendor model or vECU packaged to the FMI standard: one archive
containing a machine-readable model description and a shared library for each
target platform. SiL imports an FMU as an ordinary process participant. The
importer extracts it into the participant's kernel-owned working directory
during initialization and drives its FMI 3.0 co-simulation interface. The
extraction is dropped at shutdown and, either way, goes with the tree the
kernel removes after the Run. Channel schema field names map directly to the
FMU's Float64 input and output variables; Channel direction decides whether a
field is written before the FMU Step or published after it. Every other
variable type is bound by name on the command line, below.

The FMU path is just a command argument, so it is part of the hashed Manifest:

```python
import sys

from sil import Manifest, SubscriberRoute

m = Manifest(duration_ns=100_000_000)
m.add_schemas({
    "fmu.In": {"fields": [{"name": "u", "type": "f64"}]},
    "fmu.Out": {"fields": [{"name": "y", "type": "f64"}]},
})
m.add_channel("fmu.In", schema="fmu.In")
m.add_channel("fmu.Out", schema="fmu.Out")
m.add_process(
    "fmu",
    command=[sys.executable, "-m", "sil.fmi", "models/fmu.fmu"],
    step_period_ns=10_000_000,
    subscribes=[SubscriberRoute("fmu.In", capacity=4)],
    publishes=["fmu.Out"],
)
```

[examples/fmu/](examples/fmu/) is that snippet as a Run you can execute:
`Feedthrough`, the Modelica Association's own Reference FMU, driven by one
stimulus participant. `examples/fmu/manifest.py` is the whole Run, and
`tests/test_example_fmu.py` imports that same file rather than restating it.

```sh
make example-fmu                                # run it, record it into build/
```

Because `Feedthrough` copies each input to the output of the same name, the
Recording shows the mapping round-trip directly: an output at `t` is the input
published one Step earlier, which is the Channel's Latency rather than the
FMU's. To point it at your own FMU, change the path, the schemas, and the
stimulus — nothing else.

Any co-simulation call that answers a status other than `fmi3OK` aborts the
Run with exit 1, and the diagnostic names the call, the participant and the
status. `Warning` aborts too: a Run that steps past a status the FMU raised is
not evidence of anything. An FMU that answers `Fatal` is abandoned rather than
terminated, which is what FMI 3.0 requires of its importer.

### Binding the other variable types

A Float64 mapping is *derived*: the schema field names are the variable names,
and the match has to be total in both directions, because a name on one side
and not the other is a typo rather than an intention.

Every other type is *declared*, one binding per Channel field:

```sh
python -m sil.fmi model.fmu \
    --bind can.Rx:data=CanChannel.Rx_Data \
    --bind can.Tx:data=CanChannel.Tx_Data \
    --start BusErrorProbability=0.0
```

A binding names the Channel as well as the field because one schema is
typically carried by more than one Channel — an Rx and a Tx of the same frame
layout, say — so a field name alone names no single end of the mapping.
Declaring one binding declares them all: the bindings are then the whole
mapping, and a Channel field that none of them names is rejected rather than
left as a variable the FMU never sees. `--start` writes a variable once,
before initialization mode is entered: a structural parameter inside
Configuration Mode, where FMI 3.0 has one changed, and every other variable in
the instantiated state. An FMU that declares no structural parameter is never
asked to configure.

Both travel as command arguments, which the Manifest already hashes, so the
whole mapping is inside the hashed Manifest. Pointing at a file instead would
be covered by the Run's provenance side-car, which digests every file a
participant's command names.

The mapped types are `Float64`, `Boolean` and `Binary` — the three the CAN
acceptance fixture declares, and no more; broad type coverage is a later
slice. A `Boolean` is carried by a `u8` with C's own conversion — zero is
false, anything else is true — and what the FMU hands back is 0 or 1.

A binding that names any other type — a `String`, an `Enumeration`, an
integer — is reported before the FMU is stepped, naming the type, as is one
whose field type is not the one its variable's type maps to. A `Clock` is
reported too, and for a different reason: it is driven through the variable it
gates rather than bound to a field of its own, which is the section after
next. A variable is mapped when its declared dimensions amount to one value:
`<Dimension
start="1"/>` is one value written the long way, which is how the fixture's CAN
node declares its Binary input, while anything above one value, or a dimension
sized by another variable, is reported.

### Bounded Binary payloads

A Binary variable is variable-length and a Message is not, so a Channel
carries one in an explicit bounded representation: a `u8` array field holding
the payload, and the `<field>_length` field beside it holding how much of it
is the payload.

```python
m.add_schemas({"can.Frame": {"fields": [
    {"name": "data_length", "type": "u16"},
    {"name": "data", "type": "u8", "count": 2048},
]}})
```

The length field is an unsigned scalar that can still count the payload
field's bound: a `u8` length beside 2048 payload bytes is refused before
stepping, because a full payload could not state its own length.

The bound is the array's `count`, and it is the Run's own declaration rather
than the FMU's. Arbitrary bytes survive, embedded zeros included; on every
published Message the bytes above the length are zero, so two Runs of one
Manifest record the same bytes. On an incoming Message they are ignored: the
length is what says where the payload ends.

Nothing is truncated to fit — a payload above the bound, in either direction,
aborts the Run with exit 1. The two directions are checked differently on
purpose. A Channel bound *above* the `maxSize` an **input** variable declares
is refused before stepping, with exit 2: the FMU would refuse every payload
above it, so no such Run can work. A published Channel narrower than an
**output** variable's `maxSize` is not refused, because `maxSize` is a ceiling
the FMU may never reach — the Run declares what it is prepared to carry, and
only a payload that actually exceeds it aborts.

### Clocked payloads and Event Mode

A Binary variable that declares a Clock is not a value a Step reads. It is
defined only while its Clock is active, and a Clock is active only inside an
event — which is what a bus node's transmit buffer is: the FMI-LS-BUS CAN
demo node communicates nothing else.

Nothing extra is bound for it. The description already says which Clock gates
which variable, so the Channel is declared the way any other bounded Binary
Channel is, with one field more:

```python
m.add_schemas({"can.Frame": {"fields": [
    {"name": "data_length", "type": "u16"},
    {"name": "data", "type": "u8", "count": 2048},
    {"name": "data_event_time_ns", "type": "u64"},
]}})
```

Such a Channel carries **one Message per Clock activation** instead of one per
Step, and it carries that alone: a Channel that bound a second variable beside
it would publish a Step's value on an event's Message. Three times are
involved, and only the first is the FMU's:

| Time | What it is | Where it appears |
| --- | --- | --- |
| FMI event time | the communication point the activation was observed at | `<field>_event_time_ns`, in the Message |
| Publication time | the Slot the importer was activated in | the Recording's timestamp |
| Delivery time | one Latency later | the subscriber's own activation |

A node that transmits every 300 ms, stepped on a 100 ms grid, produces an
activation at the communication point 300 ms — published in the Slot at
200 ms, because the Step from 200 ms to 300 ms is the one it became visible
in. Nothing is quantised to the Slot and no Latency is folded into the event
time. On the incoming half the same rule holds in reverse: a Message's
activation is raised at the communication point the FMU stands on, and the
event time the sender stated is not used. The Clock goes up before the buffer
it gates is written, because a clocked variable may be accessed only while its
Clock is active — the mirror of reading the Clock before the buffer on the way
out.

The FMU's initial time is virtual time zero. With a Clock in the Manifest the
FMU is instantiated with `eventModeUsed`, so initialization ends in Event
Mode; the activations of that first event — a CAN node's bus configuration,
for instance — belong to time zero and are published in the importer's first
Slot. After that the FMU is stepped over contiguous intervals, and each Step
that ends in an event is followed by the event before the next Step begins.

The supported profile is exactly this:

| Declared | Supported |
| --- | --- |
| `hasEventMode` | must be `true`; a Clock is refused without it |
| Clock `intervalVariability` | `triggered` for one FMU on its own. A `countdown` Clock asks the importer to choose the instant it is activated at, which only a connected group can promise — see below. A `periodic` Clock asks it to own a second time grid, and the kernel owns that |
| Clocked variable | one `Binary` variable per Channel, gated by one Clock of its own causality |
| Discrete-state iteration | until the FMU stops asking, bounded at 100 iterations |
| Next event time | an FMU that declares one is asking to be stepped onto it, which this importer cannot promise: when the Slot grid lands on the declared instant the event is taken there, and when a Step would pass it the Run fails rather than step past it |
| Early return | none. `earlyReturnAllowed` is declared false, and an FMU that returns early is reported rather than counted as a completed interval |
| `canHandleVariableCommunicationStepSize` | not read. A participant's Step period is fixed by its Manifest and the kernel never shortens a Step, so the flag has nothing to decide here |

Everything outside it is reported before the Run steps, or the Run fails
saying which call answered what. `fmi3UpdateDiscreteStates` asking for another
update forever ends in a diagnostic naming the bound rather than a Run that
hangs until its response deadline.

What is still unimplemented is the layered standard itself: SiL carries the
CAN operation bytes as an opaque bounded payload and neither decodes nor
validates them.

### Connected FMUs: one participant, one bus

Two bus nodes exchanging frames need the FMU that models the bus between them,
and the three cannot be three participants. The bus states when it has finished
transmitting a frame as a *countdown Clock interval* it computes per frame —
480 us for a four-byte CAN frame at 100 000 bit/s — and a Channel's Latency is
declared in the Manifest, not computed by a model. Declaring every connected
FMU in **one** process participant is what makes that instant reachable:

```python
m.add_process(
    "importer",
    command=[
        sys.executable, "-m", "sil.fmi",
        "--instance", "node1", "models/CanNode.fmu",
        "--instance", "node2", "models/CanNode.fmu",
        "--instance", "bus", "models/CanBusSimulation.fmu",
        "--bus-profile", "application/org.fmi-standard.fmi-ls-bus.can",
        "--connect", "node1.CanChannel=bus.Node1",
        "--connect", "node2.CanChannel=bus.Node2",
        "--bind", "can.node1.Tx:data=node1.CanChannel.Tx_Data",
        "--bind", "can.bus.Node2:data=bus.Node2.Tx_Data",
        "--start", "bus.BusErrorProbability=0.0",
    ],
    step_period_ns=100 * MS,
    publishes=["can.node1.Tx", "can.bus.Node2"],
)
```

A group is declared by `--instance <name> <path>`, and every other argument
spells a variable of it as `<instance>.<variable>`. The path is its own
argument rather than part of a larger one, because that is what the kernel
resolves against the Manifest's directory and digests into the Run's
provenance — the same rule that already covers a single FMU's path. `--connect` pairs two
network terminals: each end's `Tx_Data` becomes the other end's `Rx_Data`, in
the same instant. Each Channel carries one terminal member's activations — an
out-direction Channel observes what a terminal sends, an in-direction Channel is
handed to what it receives — in the same three-field bounded representation a
single clocked FMU uses, event time included.

The group owns **communication points between the kernel's Slots**. It advances
every instance over sub-intervals ending at each instant any instance asked for
— a declared next event time, or a countdown Clock's interval — and at the
Step's own end, then propagates each activation to the terminal it is connected
to and handles the event that arrival causes, until the instant stops producing.
Every instance therefore stands on the same internal communication point at all
times, which is why no rollback is needed: an event is never reported at an
instant a peer has already passed. Messages are still published in the Slot the
importer's activation runs in, and state the FMI event time they belong to, so
the group's finer grid reaches the Recording as a stated time rather than as a
timestamp.

At one instant the Importer handles non-bus FMUs first, then gives each bus
simulation FMU all offered operations in one event. Operations for the same
terminal keep their delivery order inside one Binary buffer. This applies to
every FMU declaring `isBusSimulationFMU=true`: it changes same-instant FMI
callback order so a bus can arbitrate independent requests, as the three-node
fixture in `models/can/` requires. Empty Binary output from a bus simulation's
countdown Clock carries no operation and is consumed at the Importer edge;
empty countdown outputs from other FMUs retain their previous propagation.
The older upstream proof files are unchanged, and the kernel and Channel
contracts are unaffected.

| Declared | Supported |
| --- | --- |
| Terminal | `org.fmi-ls-bus.network-terminal` with matching rule `org.fmi-ls-bus.transceiver`, grouping `Rx_Data`, `Rx_Clock`, `Tx_Data`, `Tx_Clock` |
| Topology | one connection has exactly one `isBusSimulationFMU=true` end and one node end; arbitration and transmission timing stay in the bus FMU |
| Profile | the buffers of every terminal the Run drives declare the `--bus-profile` media type, and both ends of one direction declare the identical `mimeType` and the same layered-standard version. A terminal no `--connect` and no Channel names is left alone — an FMU may carry one of another standard |
| `Rx_Clock` | input, `triggered` |
| `Tx_Clock` | output `triggered` — the FMU raises it — or input `countdown`, which the group raises at the instant the interval ends |
| Countdown interval | read as the exact fraction the FMU states and required to be a whole number of nanoseconds; nothing is rounded onto an instant the FMU did not ask for |
| Propagation | bounded at 100 activations per instant, like the discrete-state iteration of one event |
| Channel source | a terminal takes its frames from a connected peer **or** from an in-direction Channel, never from both |

### Replaying one source at the boundary

An unconnected terminal fed by an in-direction Channel is the replay-input
boundary: a Recording of the observation Channel can stand in for the FMU that
produced it. A Replay participant is what re-publishes it — it becomes the
Publisher of every Channel it replays, and the Recording's content hash goes
into the Manifest, so the Manifest hash covers the Run's stimulus. Take the
group above, drop `node1`, and hand `bus.Node1` the Channel it published:

```python
m.add_channel("can.node1.Tx", schema="can.Frame", latency_ns=0)
m.add_replay("node1", recording="live.mcap", channels=["can.node1.Tx"])
m.add_process(
    "importer",
    command=[
        sys.executable, "-m", "sil.fmi",
        "--instance", "node2", "models/CanNode.fmu",
        "--instance", "bus", "models/CanBusSimulation.fmu",
        "--bus-profile", "application/org.fmi-standard.fmi-ls-bus.can",
        "--connect", "node2.CanChannel=bus.Node2",
        "--bind", "can.node1.Tx:data=bus.Node1.Rx_Data",
        "--bind", "can.bus.Node2:data=bus.Node2.Tx_Data",
    ],
    step_period_ns=100 * MS,
    subscribes=[SubscriberRoute("can.node1.Tx", capacity=4)],
    publishes=["can.bus.Node2"],
)
```

The activation is raised **at the instant the Message states**, not at the
communication point the group happens to stand on, which is what makes the
replayed source equivalent to the live one: the receiving FMU is handed the
same operation at the same instant, so what it computes next is the same.

`latency_ns=0` is what makes that instant reachable, and it is not optional.
An activation is published in the Slot the Step that observed it began in, so
the instant it states lies inside that Step:

    publication Slot  ≤  stated instant  ≤  publication Slot + Step period

Delivered in the Slot it was published in, a Message therefore arrives in the
Step its own instant belongs to. Under the default next-activation delivery it
arrives one Step later, where that instant is behind the group — and the
Importer reports that, naming the Channel, the terminal, the stated instant and
the Step's own bounds, rather than raising the activation somewhere else. An
instant beyond the Step's end is refused for the mirror reason.

Several Messages in one Step are each raised at their own instant; several at
one instant are raised in the order they arrived, which is the Publish order
the Recording holds them in.

The boundary decisions and the evidence behind them are in
[docs/adr/0001-connected-fmus-in-one-process-participant.md](docs/adr/0001-connected-fmus-in-one-process-participant.md)
and [docs/adr/0002-a-replayed-terminal-lands-on-its-own-instant.md](docs/adr/0002-a-replayed-terminal-lands-on-its-own-instant.md).

### Inspecting an FMU before a Run

`sil-fmi-inspect` reports whether this importer can drive an archive, before
any Manifest is written. It unpacks the archive and reads its declarations; it
never loads or executes the FMU binary, so it needs no exporter, no reference
importer and no working binary.

```sh
sil-fmi-inspect model.fmu                       # readable report
sil-fmi-inspect model.fmu --json                # the same report as JSON
sil-fmi-inspect model.fmu --mapping mapping.json
```

The report has four parts:

| Part | What it states |
| --- | --- |
| facts | FMI version, interfaces and co-simulation capabilities, platform binaries, and each variable's type, causality, variability, start, unit, dimensions, `maxSize`, `mimeType` and Clocks; the terminals and the FMI-LS-BUS manifest |
| `unusable` | why no Run can drive the archive: unreadable archive or `modelDescription.xml`, an FMI version other than 3.0, no co-simulation interface, no binary for this platform |
| `unmappable`, per variable; `unsupported`, per terminal | why no Channel can carry the variable, or no group can connect the terminal: integer, String and Enumeration types, arrays, a Clock outside the triggered profile, a terminal outside the BUS profile |
| `unverified` | what only a loaded binary can answer: whether the library and its dependencies load, whether initialization succeeds, a required execution tool, the files read from `resources/` |

A variable no Channel names is never touched, so an `unmappable` variable does
not make the archive unusable. `Feedthrough` declares every FMI type and runs.

`--mapping` checks a proposed single-FMU mapping. The document is the init
line's `schemas` and `channels` and the importer's `--bind` and `--start`
arguments; [examples/fmu/mapping.json](examples/fmu/mapping.json) is the one
`make example-fmu` runs:

```json
{"sil_fmi_mapping": 1,
 "schemas": {"fmu.In": {"fields": [{"name": "u", "type": "f64"}]}},
 "channels": {"fmu.In": {"schema": "fmu.In", "direction": "in"}},
 "bind": ["fmu.In:u=Float64_continuous_input"],
 "start": ["Float64_fixed_parameter=1.5"]}
```

Every verdict is the verdict of the checks initialization runs, not a second
policy: the description is read by the same reader, the mapping is bound by the
same function, and each terminal is built as a group builds it. The diagnostic
is the one the Run would fail with. A Run stops at its first failure and checks
the mapping before it looks for the platform binary; the report states every
finding, so an archive with both faults shows both.

An accepted mapping also lists the variables it leaves `unbound`: an input or
parameter that keeps its start value, and an output that is not published.

| Exit | Verdict |
| --- | --- |
| `0` | `compatible`: the archive is usable, and the mapping, if given, is accepted |
| `1` | `unusable` |
| `2` | usage error: the command line or the mapping document cannot be read |
| `3` | `mapping-rejected`: the archive is usable and the mapping is not |

The JSON report carries `"sil_fmi_inspection": 1`; a change to its keys raises
that number. The inspection is an audit of this importer's profile, not an FMI
conformance certification, and it does not probe dynamic dependencies.

## Large-Message routing baseline

The current data path copies a payload once from the publisher into the kernel.
It copies it again into every subscriber's queue.
[docs/bench/large-message-routing-baseline.md](docs/bench/large-message-routing-baseline.md)
measures what that costs today, before the project commits to a leased buffer
pool. The matrix varies payload size, subscriber fan-out, recording on or off,
native against process participants, inline against shared-memory transport,
and burst delivery.

```sh
make bench                     # regenerate docs/bench/routing-baseline.{json,md}
```

Copy counts come from `sil-run-instrumented`, the same kernel sources compiled
with `SIL_COPY_COUNTERS`. Wall-clock comes from the production `sil-run`, which
carries no instrumentation. Keeping the two apart lets a run report copies as
counts instead of inferring them from timing. These counters are the whole
counter surface (#63): there is no metrics artifact and no metrics flag on
`sil-run`, because a run reproduces exactly, so re-running it instrumented
answers the question for the price of one run — an inertness the determinism
suite asserts per fixture, as the same exit code and the same recording bytes
under both runners. The report states the run's exit code and keeps its
repeatable counters (`deterministic`) apart from its wall-clock and RSS values
(`observational`).

`sil-run --no-recording` runs a manifest and writes no recording. That
separates the cost of routing to subscribers from the cost of recording I/O.

## ACC reference example

[python/src/sil/examples/acc/](python/src/sil/examples/acc/) is one closed loop
end to end: a plant carrying
the longitudinal motion of two vehicles, a shimmed vECU controller, and a test
participant holding the Run to a minimum-gap KPI. The packaged
`sil.examples.acc.manifest` module is the whole Run — three participants, two
Channels, and the Duration — and the pytest suite in `tests/test_example_acc.py`
imports that same module rather than restating it.

```sh
make example                                    # run it, record it into build/
```

It ships in two variants. The second declares one Interceptor — five Steps of
delay on the sensing Channel over a one-second window — and comes from the same
builder:

```sh
PYTHONPATH=$PWD/python/src .venv/bin/python -m sil.examples.acc.manifest \
    build/acc.json
PYTHONPATH=$PWD/python/src .venv/bin/python -m sil.examples.acc.manifest \
    --delayed-sensing build/acc-delayed.json
```

The two hashes differ, which says the Runs are not the same Run;
`tests/test_example_acc.py` is what says the Interceptor is the whole of the
difference, by taking it back out of the delayed Manifest and getting the
nominal one. Late sensing is late braking: the delayed Run drives a measurably
different trajectory, which is what shows the Interceptor doing something
rather than merely being declared. Both variants are in the CI determinism
gate.

## Install and run from a staged prefix

The production runner, virtual Clock shim, and public Native participant
headers can be staged independently of the build tree. The Python wheel
contains the `sil` package, the ACC reference Run, and its command-line entry
points. Build the two parts from the repository:

```sh
uv build --wheel --out-dir dist python
wheel=$(find dist -maxdepth 1 -name 'sil-*.whl' -print -quit)
uv venv .venv-staged
uv pip install -p .venv-staged/bin/python "$wheel"

cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
prefix=$(mktemp -d)
cmake --install build --prefix "$prefix"
```

Add both installation `bin` directories to `PATH`, then run from any directory
outside the checkout. No `PYTHONPATH`, build-tree path, or preload-library path
is needed:

```sh
export PATH="$PWD/.venv-staged/bin:$prefix/bin:$PATH"
workdir=$(mktemp -d)
cd "$workdir"
sil-acc acc.json
sil-run acc.json -o acc.mcap
sil-check acc.json --runner sil-run

sil-acc --delayed-sensing acc-delayed.json
sil-run acc-delayed.json -o acc-delayed.mcap
sil-check acc-delayed.json --runner sil-run
```

The installed runner resolves the Clock shim from the staged prefix's
conventional library directory. The installed headers are under
`$prefix/include/sil`; development fixtures and `sil-run-instrumented` are
not part of the production installation.

## Replay a timestamped CSV recording

`sil-csv` converts one timestamped CSV file into a Recording the Replay
participant accepts, under an explicit mapping document. CSV is the one
starter format; decoding and unit conversion stay in this converter, and the
kernel only sees an ordinary Recording. The worked example is in
[examples/csv/](examples/csv/): `signals.csv`, its `mapping.json`, an
`observer.py` consumer that republishes every Message it receives, and the
`manifest.py` that replays the Recording into it.

With the staged installation on `PATH` (previous section), from the checkout
root:

```sh
workdir=$(mktemp -d)
sil-csv examples/csv/mapping.json examples/csv/signals.csv \
    -o "$workdir/signals.mcap" --receipt "$workdir/signals.receipt.json"
sil-csv examples/csv/mapping.json examples/csv/signals.csv \
    -o "$workdir/signals-2.mcap" --receipt "$workdir/signals-2.receipt.json"
cmp "$workdir/signals.mcap" "$workdir/signals-2.mcap"
python examples/csv/manifest.py "$workdir/replay.json" \
    --recording "$workdir/signals.mcap"
sil-run "$workdir/replay.json" -o "$workdir/run-1.mcap"
sil-run "$workdir/replay.json" -o "$workdir/run-2.mcap"
cmp "$workdir/run-1.mcap" "$workdir/run-2.mcap"
```

`make example-csv` runs the same sequence from the source tree. The two
receipts differ only in the Recording file name they record. The same CSV,
mapping and converter version give a byte-identical Recording, and the
Manifest over it gives byte-identical Run Recordings.

The mapping document is JSON:

```json
{
  "sil_csv_mapping": 1,
  "timestamp": {"column": "time_s", "unit": "s", "origin": 1695640000},
  "schemas": {"csv.Range": {"fields": [{"name": "range_m", "type": "f64"}]}},
  "channels": [
    {"channel": "target.range", "schema": "csv.Range",
     "fields": {"range_m": {"column": "range_raw", "scale": 0.125, "offset": -10}}}
  ]
}
```

- `timestamp` names the time column, its `unit` (`s`, `ms`, `us` or `ns`) and
  an optional `origin` in that unit (default 0). Replay time is
  `(cell − origin) × unit`, computed exactly in integer nanoseconds.
- `schemas` uses the Manifest's schema form with scalar fields only. Declare
  the same schemas in the replaying Manifest: the Replay participant rejects a
  Channel whose recorded schema differs from the declared one.
- Each `channels` entry maps every field of its schema, and only those, to a
  column, with an optional `scale` (default 1) and `offset` (default 0):
  `value = cell × scale + offset`. Integer fields need integer cells, scale
  and offset. Float fields compute in binary64, then round to f32 for an f32
  field; the scale and offset themselves are rounded to binary64 first. One
  column may feed several fields, the timestamp column included.

The conversion rejects, with the row, line and column, rather than guess:

- a missing, empty or repeated column; a mapping with duplicate keys, an
  unmapped or unknown schema field, a duplicate Channel, or an unknown key;
- a timestamp that is not a plain decimal, is not a whole number of
  nanoseconds after the origin, is negative after the origin, overflows u64,
  or descends below the previous row's;
- a value that is not finite, does not fit its field type, or underflows
  from nonzero to zero;
- a row with the wrong number of cells, and a Channel with some but not all
  of its cells empty in one row. All-empty cells mean that row carries no
  Message of that Channel. Nothing is interpolated or filled in.

Messages are written in row order and, within one row, in mapping order;
rows that share a timestamp keep that order when the Replay participant
publishes them. The receipt records the converter name, version, source
revision and MCAP library; the SHA-256 of the CSV, the mapping and the
Recording; each Channel's message count and first and last time; and the time
bounds of the Recording. A converted Recording carries the source and mapping digests as MCAP
metadata instead of a Manifest hash: no Manifest produced it. Choose the
replaying Manifest's Duration after the
receipt's `last_ns`: the Replay participant does not publish Messages at or
after the Duration, and it drops them without an error.

Supported limits: one comma-delimited UTF-8 file with a header row; scalar
fields of the existing schema types; one linear scale and offset per field;
times from 0 to 2^64 − 1 ns after the origin. There is no decoder for MDF,
ROS bags, BLF or DBC, and no array or payload fields.

## Replay a long Recording

A Replay participant does not keep its Recording in memory. Before any
participant steps, it reads the Recording twice: once to compare its SHA-256
with the Manifest, and once to decode every MCAP record. While the Run
advances, it reads the Recording again, one MCAP record at a time. Each read is a full or
partial pass over the file, so a long Recording costs read time, not memory.

- **Payload memory** is one read buffer per Replay participant. It grows to
  the largest MCAP record in the Recording and no further. This is the
  largest chunk, or the largest single Message outside a chunk. Chunks must
  be uncompressed: the kernel has no lz4 or zstd decoder, and it rejects a
  compressed chunk as a record that does not decode. It does not grow
  with the length of the Recording or with the Channels it does not replay.
  `sil-run-instrumented` reports it as
  `deterministic.replay_read_buffer.high_water_bytes`.
- **Metadata memory** is separate from payload memory, and it has no fixed
  limit. It grows with the number of chunks. The MCAP summary holds one chunk
  index per chunk, with one offset for each Channel in that chunk. It also
  holds every Channel and schema record.
- **A damaged Recording is a Manifest error** (exit 2), also when the Manifest
  hash matches it. The kernel rejects a Recording without its footer (cut
  short) or with an MCAP record that does not decode. It does this before
  any participant steps.
- **The validated file is the file that is replayed.** The Replay participant
  opens the Recording once and does all its reads through that open file.
  Renaming, deleting or replacing the path during the Run has no effect on
  the replay. Writing into the file during the Run changes its size or
  modification time; the next read sees that and the Run fails (exit 1) with
  "changed after it was validated". A write that keeps both the size and the
  modification time is not detected.
- **Timestamps are replayed as stored, not sorted.** Some Recordings store a
  Message after a Message with a later time. The Replay participant publishes
  the earlier Message at its own time, after the Messages stored before it.
  Its Slot is then earlier than the Slot before it. A Recording whose times start at or after the Duration publishes nothing.
  `sil-csv` rejects descending timestamps, so its Recordings are in time order.

## Compare a trajectory against a reference

`sil-compare` compares one Recording with a reference Recording under a
comparison contract, and gives a pass/fail report. The contract states each
decision; the command infers nothing from the data. A reference in another
format becomes a Recording through `sil-csv` first.

The worked example compares the retained SiL Recording of the ACC
`plant-accelerate` qualification case with the independent fmpy trace of the
same FMU. [examples/compare/plant-accelerate.reference.csv](examples/compare/plant-accelerate.reference.csv)
is that trace, one row per time and one column group per FMU instance;
[examples/compare/reference-mapping.json](examples/compare/reference-mapping.json)
converts it; [examples/compare/contract.json](examples/compare/contract.json)
is the contract. With the installed wheel on `PATH`, from the checkout root:

```sh
workdir=$(mktemp -d)
sil-csv examples/compare/reference-mapping.json \
    examples/compare/plant-accelerate.reference.csv -o "$workdir/reference.mcap"
sil-compare examples/compare/contract.json \
    proofs/acc-fmi/evidence/plant-accelerate-1.mcap "$workdir/reference.mcap"
sil-compare ... --json                          # the same report as JSON
```

A contract names each compared Channel and states, for that Channel:

| Key | What it states |
| --- | --- |
| `reference_channel` | the reference Channel with the expected values; the default is the same name |
| `actual_offset_ns`, `reference_offset_ns` | observation time = recorded time + offset, per side. The FMU importer publishes at the start of a Step the state at the end of that Step, so its offset is the Step period. An input Channel of the same Run can have offset `0`. No offset is shared between Channels |
| `observations` | the exact observation times: `{"times_ns": [...]}`, or `{"start_ns", "stop_ns", "step_ns"}` with both ends included |
| `fields` | a rule for each field of the actual schema: `"exact"` for an integer field, `{"atol": a, "rtol": r}` for a float field, or `"ignore"` |

The top-level `evaluation` window, `{"from_ns", "to_ns"}` with both ends
included, applies to every Channel. An observation time outside it is not
compared, so a warm-up is outside the window.

The rules:

- A float value passes when `abs(actual - reference) <= atol + rtol *
  abs(reference)`. A NaN or an infinity on either side fails. No rule accepts
  one.
- At each observation time, each side must have exactly one Message. A missing
  Message fails. Two Messages fail as ambiguous. A Message whose observation
  time is not in the contract is not compared. Nothing is interpolated and no
  tolerance is fitted.
- Wrong coverage fails: a schema field that the contract does not name, a
  contract field that the schema does not declare, a rule that does not fit
  the field type, a compared field that the reference Channel does not have,
  a Channel that a Recording does not declare, and a Channel whose every
  field is `"ignore"`. Array fields are compared only as `"ignore"`. A Channel
  that the contract does not name is not compared. A contract that repeats a
  key is refused.
- The final publication is compared like all other publications. With the
  Step-period offset, the last Step's state lands on the Duration, where no
  participant of the Run can observe it.

The report states the verdict, the digest of the contract and of both
Recordings, per Channel the count of observations, of checked, failed,
non-finite, missing and ambiguous Messages, and the total count of
divergences. It also states the first divergence: the earliest observation
time, then the contract's Channel and field order. That entry gives the kind
(`value`, `nonfinite`, `missing-actual`, `missing-reference`,
`ambiguous-actual`, `ambiguous-reference`), the Channel and field, the
observation time, the recorded time on each side (the MCAP log time; a
Recording states no other source time), the actual and expected
values, the tolerance with its allowed error, and the absolute error. For a
missing or ambiguous Message the field is `null`, and the values are an object
of the compared fields; an ambiguous side gives a list of times and values.
The JSON report is strict JSON: a non-finite value is the string `"nan"`,
`"inf"` or `"-inf"`.

A reference comparison is not a determinism check. It tells whether two
trajectories agree within a contract, often across two Manifests or two
engines. It does not tell whether a Run reproduces. `sil-check` bit-compares
two Recordings of one Manifest for that. Each report states this in its
`determinism` key. Domain KPIs stay with the consumer, and the retained proof
results stay attributable to their own contracts.

| Exit | Verdict |
| --- | --- |
| `0` | `pass` |
| `1` | `fail`: a divergence or wrong coverage |
| `2` | usage error: the command line, the contract or a Recording cannot be read |

The contract carries `"sil_comparison": 1` and the JSON report carries
`"sil_comparison_report": 1`; a change to their keys raises that number.

## Run one Manifest in a Linux container

Build the production image from its pinned base-image digest and exact Python
dependency lock. The separate `acceptance` target adds the public FMU reference
artifact for this repository's CI checks; it is not part of the production
image.

```sh
version="$(python3 tools/release.py project-version)"
docker build --target runtime -t sil:local \
  --build-arg SIL_VERSION="$version" \
  --build-arg SIL_SOURCE_REVISION=local .
```

Mount a workspace at `/workspace` and use the host user's numeric identity so
ordinary host-owned directories remain writable. Runtime networking is not
needed. The image entry point is `sil-run`, so its arguments are the installed
runner's normal command-line contract:

```sh
workspace=$PWD/run
mkdir -p "$workspace"
cp manifest.json "$workspace/manifest.json"

docker run --rm --network none \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$workspace,dst=/workspace" \
  sil:local /workspace/manifest.json \
  --participant-timeout-ms 5000 -o /workspace/out.mcap
```

The command prints the Manifest hash, returns `sil-run`'s exit code unchanged,
and writes `out.mcap` directly into the host directory. To run without a
Recording while keeping the same Manifest and Manifest hash:

```sh
docker run --rm --network none \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$workspace,dst=/workspace" \
  sil:local /workspace/manifest.json \
  --participant-timeout-ms 5000 --no-recording
```

A private image can add Participant artifacts without changing the production
runtime surface. Name their image paths in the mounted Manifest:

```dockerfile
FROM sil:local
USER root
COPY --chown=10001:10001 participants/ /opt/participants/
USER 10001:10001
```

The default image user is non-root. Callers may still select their host UID and
GID as above; the installed runner and Python Participant entry points do not
depend on a passwd entry or a writable home directory.

## Consumer adoption proof

[proofs/esmini/](proofs/esmini/) is that derived-image pattern carried through
end to end with a Participant this repository did not write: the third-party
scenario engine [esmini](https://github.com/esmini/esmini) `v3.8.1`, driving a
UN-R157 cut-in scenario as a Process participant. It starts from the published
runner image by digest, declares its own Schema, Channels, route capacities
and Duration, is judged by a domain KPI in-run and post-hoc, reproduces
esmini's own expected trajectory within tolerance, and records bit-identically
across two Runs.

```sh
proofs/esmini/run-proof.sh            # needs docker and network
```

Nothing in that directory is part of the framework. Its README is the evidence:
what worked unchanged, what needed consumer-side adaptation, and what the
current contracts cannot represent.

## ACC acceptance bundle

[proofs/acc-fmi/](proofs/acc-fmi/) also ships the closed-loop FMI 3.0 baseline
as something a consumer can adopt: two source-available ACC FMUs, the Runs that
judge them, and the pinned artifacts those Runs are compared against.

```sh
proofs/acc-fmi/acceptance-bundle.sh prepare     # once; needs docker and network
proofs/acc-fmi/acceptance-bundle.sh run         # offline, from the installed bundle
```

Preparation is the only step that needs the exporter, the independent importer
and a compiler. The acceptance Runs execute in the production runtime image
plus consumer material: the installed wheel and the installed `sil-run`, no
source tree on the import path, no network, and no build or comparison tool in
the image. They re-hash every pinned artifact first, compare a nominal
trajectory and a timing-sensitivity envelope against independently recorded
references, require the deliberate KPI failure to fail, and compare two
Recordings of each Manifest byte-for-byte.

Nothing in that directory is part of the framework either. The example image
derives from the production image and adds only its own consumer material; the
supported container surface stays the one [SUPPORT.md](SUPPORT.md) names.

[proofs/acc-fmi/INSTALL.md](proofs/acc-fmi/INSTALL.md) covers units, time
conventions, the default Channel Latency, the model's limitations, the FMI 3.0
profile this evidence establishes with the capabilities it rejects, and how to
read a reference mismatch or an unsupported FMU.

## FMI-LS-BUS CAN acceptance fixture

[proofs/fmi-ls-bus/](proofs/fmi-ls-bus/) pins the Modelica Association's
FMI-LS-BUS CAN demo FMUs, builds them for the supported machine class from
sources upstream ships no binary for, and publishes the profile an importer
has to meet to drive them: Event Mode, a triggered output Clock, and a Binary
buffer of CAN operations. A reference exchange drives the node through an
independent FMI 3.0 importer and matches payloads and event times written down
before the Run; the released SiL Importer is then pointed at the same FMU, and
what it answers is kept verbatim.

```sh
proofs/fmi-ls-bus/run-proof.sh        # needs docker and network
```

Most of it is an evidence gate rather than an implementation: the *released*
Importer cannot drive this FMU, and the two reasons it cannot are the retained
measurement the CAN milestone is built against. The last two steps are the other
way round — the same released runner with the checkout's `sil` package ahead of
it, driving the pinned node on both of the fixture's step grids, and then
connecting two instances of that node through the pinned **bus simulation FMU**
in one Run. Both judge the Recording against an expected exchange written from
upstream's sources beforehand, event times included: a frame offered at 300 ms
is confirmed to its sender and delivered to its peer at 300.48 ms, and the
frame that lost arbitration follows at 300.96 ms.

## First-party CAN bus model

[models/can/](models/can/) builds a standalone C++20 FMI 3.0 CAN Bus Simulation
FMU for a restricted FMI-LS-BUS 1.0.0 profile. It declares four terminals,
configures one to four as active, and carries 11-bit Classical CAN data frames.
Queued requests arbitrate by identifier after each intermission; per-node FIFO
capacity and buffer or discard behavior are configurable. A transmitting frame
cannot be preempted. Completion and arbitration instants reach the Importer as
countdown Clock intervals in whole nanoseconds. The model answers a corrupt
operation with the standard Format Error. An unsupported one fails the instance. Queues,
reports and same-instant events are bounded per instance. `models/can/run.sh`
builds and qualifies the same Linux x86-64 artifact through independent FMI
calls, sanitized checks of its C entry points and SiL's existing FMU group.
It is released as `SilCanBus` 1.0.0 with published digests and build
identities; [models/can/RELEASE.md](models/can/RELEASE.md) states the supported
platform, compatibility policy, licensing and limits, and
[models/can/example/](models/can/example/README.md) configures a Run without
source edits. `models/can/bundle.sh` validates that example from a clean
installed SiL bundle against an independent FMPy path.

## Build & test

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j
ctest --test-dir build --output-on-failure
uv venv .venv && uv pip install -p .venv/bin/python -e "python/[dev]"
.venv/bin/python -m pytest tests/
```

The pytest suite builds the kernel if needed and includes the milestone exit
criterion (`tests/test_determinism.py`: run twice → bit-identical MCAP).

## Layout

```
kernel/src/        C++20 kernel: manifest, interceptor plan, engine
                   (scheduler+router), recorder, native/process adapters
include/sil/       stable C ABI for native participants; clock-region layout
participants/      toy native participants (walking-skeleton fixtures) and
                   the bench publisher/subscriber fixtures for the
                   routing baseline
shim/              virtual clock shim (preload lib) + probe for POSIX vECUs
schemas/           message schemas for benchmark, FMU, and toy fixtures
tools/silschema.py schema → packed C structs; sil.schema packs the same
                   layout in Python
tools/bench_*.py   routing-baseline driver and its process participants
docs/bench/        routing baseline: procedure, raw results, decision inputs
docs/adr/          architecture decisions and the evidence behind them
python/src/sil/examples/acc/
                   the packaged ACC reference example: one closed-loop Run
                   in a nominal and a delayed-sensing variant
examples/fmu/      the FMU import example: one Reference FMU driven as a
                   process participant
examples/csv/      the CSV replay example: a CSV recording and its mapping,
                   converted by sil-csv and replayed into a consumer
examples/compare/  the comparison example: a contract, and the independent
                   ACC trace a SiL Recording is compared against
proofs/esmini/     consumer-side adoption proof: a third-party scenario
                   engine run on the published release, with its evidence
proofs/acc-fmi/    checkout qualification of source-available ACC FMUs:
                   pinned exporter, independent FMI execution, closed-loop
                   communication-period sensitivity experiment, retained evidence
proofs/fmi-ls-bus/ acceptance fixture for the FMI-LS-BUS CAN demo FMUs:
                   pinned artifacts, the supported profile, what the released
                   Importer does with them, and what the checkout's does
python/src/sil/    manifest builder, step-participant lib, test API,
                   determinism check, declared memory footprint,
                   CSV-to-Recording converter
tests/             behavior tests at the run boundary
```

Primitive schema metadata lives in `python/src/sil/_schema_types.py`: the
Manifest builder, Python codec, and C header generator share its formats,
derived widths and integer bounds. CMake installs that same stdlib-only file
beside `silschema` as `_sil_schema_types.py`; keep both files when moving the
installed tool. Installation copies the source file verbatim, and the installed
conformance test checks that copy against the source. No generated metadata or
regeneration step is needed.
The kernel retains independent type and numeric validation for hand-written
Manifests. `tests/fixtures/schema_conformance.json` states expected bytes and
offsets independently; `tests/test_schema_conformance.py` compiles generated
structs and exercises them through kernel loading and Recording from both the
checkout and installed prefix. Numeric override tests separately pin the
builder/loader conversion policies, including finite f32 limits.

## Notes / deferred (per DESIGN.md)

- Shared-memory zero-copy payloads and bus adapters: later milestones.
- The recorder is fed in global publish order — behaviorally identical to a
  latency-0 subscriber scheduled last in every slot.
- Message layout is packed little-endian; cross-platform bit-exactness is an
  explicit non-goal.

## License, security, and support

- [LICENSE](LICENSE) — Apache License 2.0, SPDX `Apache-2.0`, covering the
  kernel, the public C ABI headers, the Clock shim, the Python distribution,
  and the schema-generation surface a Participant compiles against.
- [NOTICE](NOTICE) — the attribution notice the license propagates.
- [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) — every third-party
  component in a published artifact, with its license.
- [SECURITY.md](SECURITY.md) — how to report a vulnerability privately, what a
  report should contain, what response to expect, and which versions get fixes.
- [SUPPORT.md](SUPPORT.md) — supported machine class, the interfaces under the
  compatibility policy, the interfaces that are implementation details, the
  boundary of the determinism guarantee, and the absence of any safety
  qualification.
- [CONTRIBUTING.md](CONTRIBUTING.md) — the terms a contribution is accepted
  under, and how to get a change reviewed.
- [docs/releasing.md](docs/releasing.md) — how maintainers prepare, publish,
  verify, and recover a versioned release bundle.
