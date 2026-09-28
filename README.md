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

The pytest helper takes the same option as a keyword. Install the `sil` wheel
into the test environment, then give the helper the runner path:

```python
from sil.testing import run_simulation

def test_regression(tmp_path):
    result = run_simulation(
        build_manifest(),               # your sil.manifest.Manifest
        runner="/opt/sil/bin/sil-run",
        workdir=tmp_path,
        participant_timeout_ms=5000,
    )
    assert result.messages("ticks")
```

The helper forwards the value unchanged as `--participant-timeout-ms`. It
accepts only an `int` from 1 to 2^63−1 and raises `TypeError` or `ValueError`
before it starts the run. A missed deadline raises `RunFailure` with
`exit_code` 1 and the runner diagnostic as its message. The runner stops the
participant and its descendants at Run shutdown, as for a direct `sil-run`.
When you omit the keyword, the wait stays unlimited and the Manifest hash, exit
codes, and Recording bytes do not change.

The deadline is not a bound on the whole job. It covers only the response wait
of each Process participant. Native participants run in the runner process, so
a Native callback that does not return is not bounded by this option. Give the
CI job its own overall timeout (for example `timeout-minutes` in GitHub
Actions). That timeout bounds this case and all other waits.

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

## Replay recorded input into one FMU

`sil-fmu-replay` writes the Manifest of a Run that replays a converted
Recording into one FMU. An authoring document states every choice of the
Run. The command checks the document against the FMU before anything runs,
then writes an ordinary canonical Manifest: a Replay participant, and one
Process participant whose command is the
[FMI importer](#fmi-30-co-simulation-importer)'s. The helper adds no
contract. Every choice is visible in the Manifest, and a hand-written
Manifest with the same bytes is the same Run.
[examples/fmu-replay/](examples/fmu-replay/) is the worked example:

| File | Role |
| --- | --- |
| `ego_motion.c`, `modelDescription.xml`, `package.py` | `EgoMotion`, a scalar FMU with units: input `acceleration` (m/s2), parameters `initial_speed` (m/s) and `initial_position` (m), outputs `speed` (m/s) and `position` (m) |
| `recorded.csv`, `mapping.json` | the recorded acceleration in cm/s², and the `sil-csv` mapping that converts it to m/s² |
| `authoring.json` | the authoring document |
| `reference.csv`, `reference-mapping.json`, `contract.json` | the closed-form trajectory, computed by hand, and the `sil-compare` contract |

With the staged installation on `PATH`, from the checkout root:

```sh
workdir=$(mktemp -d "$HOME/sil-fmu-replay.XXXXXX")
cc -shared -fPIC -O2 -o "$workdir/EgoMotion.so" examples/fmu-replay/ego_motion.c
python examples/fmu-replay/package.py "$workdir/EgoMotion.so" \
    -o "$workdir/EgoMotion.fmu"
sil-csv examples/fmu-replay/mapping.json examples/fmu-replay/recorded.csv \
    -o "$workdir/recorded.mcap" --receipt "$workdir/recorded.receipt.json"
sil-csv examples/fmu-replay/reference-mapping.json \
    examples/fmu-replay/reference.csv -o "$workdir/reference.mcap"
sil-fmu-replay examples/fmu-replay/authoring.json "$workdir/EgoMotion.fmu" \
    --recording "$workdir/recorded.mcap" -o "$workdir/fmu-replay.json" \
    --receipt "$workdir/authoring.receipt.json"
sil-run "$workdir/fmu-replay.json" -o "$workdir/run-1.mcap"
sil-run "$workdir/fmu-replay.json" -o "$workdir/run-2.mcap"
cmp "$workdir/run-1.mcap" "$workdir/run-2.mcap"
sil-compare examples/fmu-replay/contract.json "$workdir/run-1.mcap" \
    "$workdir/reference.mcap"
```

`make example-fmu-replay` runs the same sequence from the source tree, and
authors the Manifest twice to show that the two are byte-identical.

The authoring document is JSON. Every key is required, and nothing has a
default:

```json
{
  "sil_fmu_replay": 1,
  "step_period_ns": 10000000,
  "duration_ns": 1000000000,
  "schemas": {"ego.Acceleration": {"fields": [{"name": "accel_mps2", "type": "f64"}]},
              "ego.Motion": {"fields": [{"name": "speed_mps", "type": "f64"},
                                        {"name": "position_m", "type": "f64"}]}},
  "channels": {
    "ego.accel": {"schema": "ego.Acceleration", "direction": "in",
                  "latency_ns": 0, "route": {"capacity": 1, "overflow": "fail"}},
    "ego.motion": {"schema": "ego.Motion", "direction": "out",
                   "latency_ns": 10000000}
  },
  "bind": [{"channel": "ego.accel", "field": "accel_mps2",
            "variable": "acceleration", "unit": "m/s2"}, "..."],
  "start": [{"variable": "initial_speed", "value": "20", "unit": "m/s"}, "..."],
  "hold": []
}
```

- `step_period_ns` is the FMU's communication step, and `duration_ns` is the
  Run's Duration.
- Each Channel states its `latency_ns`. Each `in` Channel also states its
  bounded subscriber `route`. The Recording replays every `in` Channel, and
  the FMU publishes every `out` Channel.
- Each `bind` entry becomes one `--bind <channel>:<field>=<variable>`
  argument. Each `start` entry becomes one `--start <variable>=<value>`
  argument, with the value text in the importer's spelling: a decimal
  number, `true` or `false`, or hexadecimal for a Binary. `unit` is the unit
  the value is in, or `null` for no unit.
- `hold` names the FMU inputs that keep the start value the FMU declares
  for the whole Run.

The command writes the FMU path as an argument of its own, resolved to an
absolute path, so `sil-run` resolves it and records its SHA-256 in the Run's
provenance side-car. The Recording is named by its absolute path and its
SHA-256, as `Manifest.add_replay` names it.

Before it writes a Manifest, the command rejects (exit 2) with the reason:

- an FMU the importer cannot drive;
- a mapping the importer would reject: an unknown variable, a variable of
  the other direction, a field type that does not carry the variable's type,
  a variable type the importer does not map, a start value that does not
  parse, and a Binary field or Binary start value above the variable's
  `maxSize`. These are the checks
  of [`sil-fmi-inspect`](#inspecting-an-fmu-before-a-run). The importer makes
  the same checks when it initializes. The command does not load the FMU's
  binary;
- a start value for a variable that is not an input or a parameter;
- a stated unit that is not the unit the FMU variable declares. The
  importer converts no unit. Convert the recorded unit at the edge, with
  `sil-csv`'s `scale` and `offset`, and state the FMU's unit. The example
  records cm/s² and converts with `"scale": 0.01`;
- an FMU input that is not bound, not given a start value and not held;
- a Recording that does not carry an `in` Channel, or carries it with a
  different schema;
- a missing Latency or route, a route on an `out` Channel, an unknown or
  duplicate key, and every value the Manifest builder refuses.
- a Manifest or receipt path that is the document, the FMU or the
  Recording, and a receipt path that is the Manifest path. The command
  compares the resolved paths.

The receipt (on standard output, or at `--receipt`) records the command's
version, the SHA-256 of the document, the FMU, the Recording and the
Manifest, and each binding, start value and held input with the FMU's type,
causality and unit. The receipt carries `"sil_fmu_replay_receipt": 1`.

What the Run does at the edges of the recorded input:

- **Input at zero.** Until the first recorded Message reaches the FMU, an
  input has its start value: the FMU's declared start, or the document's
  `start`. With `latency_ns` 0, a Message recorded at 0 reaches the first
  Step, which covers `[0, P]`. With a Latency of one Period `P`, it reaches
  the second Step, and the first Step uses the start value. The example
  declares 0; with one Period, its first speed is 20 m/s instead of
  20.015 m/s, and the comparison fails.
- **Between Messages.** A Step writes the newest Message of each input
  Channel. The FMU keeps that value until a newer Message arrives. Nothing
  is interpolated. A Message recorded between two Steps reaches the FMU at
  the next Step after it is visible.
- **Outputs.** The importer publishes at the start of a Step the values at
  its end, so the contract states `"actual_offset_ns"` of one Period.

Supported limits:

- **Fixed period.** The kernel steps the FMU on one fixed Period. The
  importer does not choose communication points: an FMU that asks for an
  event between two Steps fails the Run. Choose a Period that divides every
  instant the FMU must stop at.
- **Event profile.** One FMU, driven by the importer's single-FMU profile.
  A Clock is carried only as a clocked Binary payload of a `triggered`
  Clock. The command does not author a group of connected FMUs.
- **Types.** The importer's mapped types: `Float64`, `Boolean` and
  `Binary`. Other types can be held at their declared start.

## Couple FMUs through Channels

`sil-fmu-couple` writes the Manifest of a Run of FMUs that are coupled
through Channels. Each FMU is its own Process participant, and each
connection is a field of an ordinary Channel. A coupling document states
every choice. The command checks the document against the FMUs before
anything runs, then writes an ordinary canonical Manifest in which each FMU's
command is the [FMI importer](#fmi-30-co-simulation-importer)'s. The kernel
and the importer do not change. A hand-written Manifest with the same bytes
is the same Run. A connection that carries an instant between two Slots, such
as a bus model's transmission time, is not a Channel field. For that, use
a [group](#connected-fmus-one-participant-one-bus) (ADR 0001).

[examples/fmu-coupling/](examples/fmu-coupling/) holds two documents:

| File | Run |
| --- | --- |
| `acc.json` | the ACC controller/plant loop of `proofs/acc-fmi` (#148): one Period of Latency in each direction |
| `feedback.json` | a delayed feedback loop between two `Feedthrough` instances |

From the checkout root, with the staged installation on `PATH`:

```sh
workdir=$(mktemp -d "$HOME/sil-fmu-couple.XXXXXX")
fmu=tests/fixtures/reference-fmus/3.0/Feedthrough.fmu
sil-fmu-couple examples/fmu-coupling/feedback.json \
    --fmu left "$fmu" --fmu right "$fmu" \
    -o "$workdir/feedback.json" --receipt "$workdir/feedback.receipt.json"
sil-run "$workdir/feedback.json" -o "$workdir/run-1.mcap"
sil-run "$workdir/feedback.json" -o "$workdir/run-2.mcap"
cmp "$workdir/run-1.mcap" "$workdir/run-2.mcap"
```

`make example-fmu-coupling` runs the same sequence from the source tree.

The coupling document is JSON. Every key is required, and nothing has a
default:

```json
{
  "sil_fmu_coupling": 1,
  "duration_ns": 5000000000,
  "fmus": {
    "plant": {"step_period_ns": 10000000, "priority": 0, "start": [],
              "hold": ["lead_accel_mps2", "initial_lead_position_m"]},
    "controller": {"step_period_ns": 10000000, "priority": 1, "start": [],
                   "hold": []}
  },
  "channels": {
    "sensing": {
      "publisher": "plant", "latency_ns": 10000000,
      "fields": [{"name": "gap_m", "type": "f64", "variable": "gap_m",
                  "unit": "m"}, "..."],
      "subscribers": {
        "controller": {"capacity": 2, "overflow": "fail",
                       "bind": {"gap_m": "gap_m", "...": "..."}}
      }
    },
    "...": {}
  }
}
```

- `--fmu <name> <path>` gives the archive of each FMU the document names.
  The command writes the path as an argument of its own, resolved to an
  absolute path, so `sil-run` records its SHA-256 in the Run's provenance.
- Each FMU states its `step_period_ns` and its `priority`. Each FMU has its
  own priority, so a lower priority runs first in a Slot, and a name never
  decides the order. To change the order, change a priority.
- Each Channel has one `publisher` and states its `latency_ns`. Its schema has
  the Channel's name and its `fields`. Each field names the publisher's output
  `variable` and states its `unit`, or `null` for no unit.
- Each subscriber states its bounded route and `bind`s fields of the Channel
  to its inputs. One bound field is one connection.
- `start` gives an input or a parameter a start value, with its unit. A
  connected input holds its start value until its first delivery. `hold`
  names the inputs that keep the start value the FMU declares for the whole
  Run.

The Manifest is canonical: the command sorts FMUs, Channels, routes and start
values, so a document that declares them in another order gives the same
bytes.

Before it writes a Manifest, the command rejects (exit 2) with the reason:

- an FMU the importer cannot drive, or a mapping the importer would reject.
  These are the checks of
  [`sil-fmi-inspect`](#inspecting-an-fmu-before-a-run). The command does not
  load an FMU's binary;
- an FMU with no `--fmu` archive, and an archive for no declared FMU;
- a publisher or subscriber that is not a declared FMU, and a bound field
  that the Channel does not carry;
- a connection that does not start at an output of its publisher or end at
  an input of its subscriber, or whose two ends declare different types or
  dimensions;
- a stated unit that is not the publisher's unit, and a connection between
  two different units. The importer converts no unit. Author the conversion
  explicitly: add a converting FMU to the document, between the two;
- an input that two connections feed, an input that is not connected, not given a
  start value and not held, a connected input that has no start value, and a
  held input whose FMU declares no start value;
- a missing or `null` `latency_ns`;
- two FMUs with the same priority;
- a cycle of zero-Latency connections. The diagnostic names the cycle, for
  example `left -[left.value]-> right -[right.value]-> left`;
- a zero-Latency connection whose publisher does not run before its
  subscriber. The diagnostic names an order that the connections allow;
- a route that holds more Messages than its `capacity` under the `fail`
  policy (see the plan below);
- a `duration_ns` that is not a multiple of the `step_period_ns` of each FMU
  (see [Timing across several periods](#timing-across-several-periods));
- an unknown or duplicate key, and every value the Manifest builder refuses.

**The zero-Latency check is a conservative authoring profile.** It reads the
declared connections only. It does not find or solve an algebraic loop in the
models' equations, and it refuses a structural cycle even where the models
would not form an algebraic loop. A loop with a Latency above zero on at
least one of its Channels is an ordinary feedback loop, and it stays
supported: `feedback.json` is one.

The command prints the plan of the Run on standard output. The receipt (at
`--receipt`, `"sil_fmu_coupling_receipt": 1`) holds the same plan, each
connection with its type and unit, and the SHA-256 of the document, each FMU
and the Manifest. For `feedback.json`, the plan starts:

```text
Duration 50 ms: Slots at 0 <= t < 50 ms; the last Step of each FMU ends on the Duration
execution order in each Slot (lowest priority first):
  1. left  priority 0, period 10 ms, 5 Steps
  2. right  priority 1, period 10 ms, 5 Steps
same-Slot connections: none
Channels (a Message published at t holds its publisher's outputs at t + the publisher's period):
  left.value, published by left with Latency 10 ms: value = Float64_continuous_output
    to right: route capacity 2 (fail), at most 2 Messages in the route
      right.Float64_continuous_input holds 0 (FMU start) until the first delivery
      at 0 ms: no Message yet, holds the start value
      at 10 ms: takes the Message published at 0 ms (values at 10 ms)
    last Message published at 40 ms (values at 50 ms): no activation takes it; only the Recording holds it
```

- **Publication.** The importer publishes at the start of a Step the values
  at its end. A Message published at `t` holds the publisher's outputs at `t`
  plus the publisher's period.
- **Delivery.** A Message is visible one Latency after its publication. The
  subscriber takes it at its first activation at or after that time, and
  writes it before its Step. A zero-Latency Message reaches a subscriber in
  the same Slot, because its publisher runs first.
- **Activations.** For each route, the plan lists the input that each
  activation of the subscriber steps on, until the pattern repeats. The
  pattern repeats after one common period of the two FMUs, counted from the
  first time a Message can be visible. The plan lists at most 12
  activations of a route and states how many more come before the pattern
  repeats. The first activations hold the start value. The plan states whether the start value comes from the document or
  from the FMU.
- **Route bound.** A route holds a Message from its publication to its
  delivery. When the publisher runs first in a Slot, its new Message is in
  the route before the subscriber takes the previous one. The plan states
  the most Messages each route holds in the Run. In `acc.json`, the sensing
  route holds two, and a sensing Latency of two Periods would make it three.
  Under `drop_newest`, a route refuses a Message that finds it full. The plan
  lists the deliveries that the kernel makes and the dropped publications.

Supported limits:

- **Channel fields only.** A connection carries a `Float64`, `Boolean` or
  `Binary` value of one output, as the single-FMU importer maps it. Clocks,
  network terminals and FMI-LS-BUS stay in a group.
- **Fixed periods.** Each FMU steps on its own fixed Period, from 0, and
  the Duration is a multiple of each Period. There is no adaptive step, no
  rollback and no change of a Period during the Run.
- **FMUs only.** The Run holds the coupled FMUs. The proof in
  [proofs/acc-fmi/](proofs/acc-fmi/README.md#authored-coupling-187) runs the
  rendered ACC Manifest and compares it with the independent FMPy trajectory.

### Timing across several periods

The FMUs of one document can have different periods. The rules of the plan
above apply to each route. This section states the timing contract for such
a Run. The tests (`TestSeveralPeriods` in `tests/test_fmu_coupling.py`) use
this Run: `ball` (`BouncingBall`) publishes its height, and two `Feedthrough`
instances, `fast` and `slow`, publish the height they step on. Each FMU
states its start value in the document.

| FMU | Period | Priority | Steps in 120 ms | Takes | Latency of the route |
| --- | --- | --- | --- | --- | --- |
| `ball` | 20 ms | 0 | 6 | nothing | |
| `fast` | 10 ms | 1 | 12 | `ball` | 10 ms |
| `slow` | 30 ms | 2 | 4 | `fast` | 10 ms |

All three FMUs have an activation in the Slots at 0 and 60 ms. In such a
Slot, they run in the order of their priorities.

**Four times of one Message.** The contract keeps them apart:

| Time | What it is | For a Message of `ball` published at 20 ms |
| --- | --- | --- |
| Sample time | The virtual time that the values describe. For an FMU output, this is the end of the Step, the FMU endpoint: publication Slot + the publisher's period. | 40 ms |
| Publication Slot | The Slot of the Step that publishes the Message. The Recording stores this time. | 20 ms |
| Visible time | Publication Slot + the Channel's Latency. | 30 ms |
| Delivery time | The first activation of the subscriber at or after the visible time. The subscriber writes the value before its Step over `[delivery, delivery + its period]`. | 30 ms (`fast`) |

A replayed Message has a sample time too. The Replay participant publishes
it at its recorded time, and a
[comparison contract](#compare-a-trajectory-against-a-reference) states the
offset from its publication to its sample time.

**Which input each activation steps on.** Each activation writes the
Messages it takes in Publish order, so it steps on the newest one. An
activation that takes no new Message holds the last value. Before the first
delivery, the input holds its start value. The plan for this Run states:

```text
  ball, published by ball with Latency 10 ms: h = h [m]
    to fast: route capacity 1 (fail), at most 1 Message in the route
      fast.Float64_continuous_input holds 1.25 (document start) until the first delivery
      at 0 ms: no Message yet, holds the start value
      at 10 ms: takes the Message published at 0 ms (values at 20 ms)
      at 20 ms: no new Message, holds the one published at 0 ms (values at 20 ms)
    last Message published at 100 ms (values at 120 ms): fast takes it at 110 ms
  fast, published by fast with Latency 10 ms: h = Float64_continuous_output [m]
    to slow: route capacity 4 (fail), at most 4 Messages in the route
      slow.Float64_continuous_input holds 2.5 (document start) until the first delivery
      at 0 ms: no Message yet, holds the start value
      at 30 ms: takes 3 Messages and steps on the newest, published at 20 ms (values at 30 ms)
    last Message published at 110 ms (values at 120 ms): no activation takes it; only the Recording holds it
```

`fast` takes a new height at every second activation and holds it in
between. `slow` takes three Messages of `fast` at once, and the two older
ones have no effect.

**Initialization.** Each FMU initializes alone, at virtual time 0, before
the first Slot. The importer sets the start values, then enters and exits
the initialization mode of that one FMU. No Message is delivered during
initialization, and no FMU sees the initial outputs of another FMU. There is
no joint initialization and no solver for the initial values of the coupled
FMUs. Thus each connected input needs a start value, from the document or
declared by the FMU, and the plan states which one. A Recording holds no
initial output: the first Message of an FMU, published at 0, holds its
values at the end of its first Step.

**Duration.** A Run has the Slots `0 <= t < Duration`, a half-open interval.
The kernel steps an FMU at each of its Slots, and each Step covers one full
period `[t, t + period]`. Thus the last Step of an FMU starts at
`Duration - period` and ends on the Duration only if the Duration is a
multiple of the period. `sil-run` does not clip the last Step: for a
hand-written Manifest whose Duration is not a multiple, the FMU steps past
the Duration, and that behavior does not change. This authoring profile
refuses such a Duration before it writes the Manifest. The diagnostic names
each FMU, the last Step and a Duration that fits, for example:

```text
sil-fmu-couple: error: Duration 100 ms is not a multiple of the period of FMU 'slow' (period 30 ms). The last Step of 'slow' would start at 90 ms and end at 120 ms, after the Duration. [...] such as 60 ms or 120 ms
```

**The final samples.** The last Message of each FMU holds its values at the
Duration. A subscriber takes it only when it is visible before the
Duration and a later activation of the subscriber exists. For each Channel,
the plan states who takes the last Message. In this Run, `fast` takes the
last height of `ball` at 110 ms. The last Messages of `fast` (visible at
120 ms) and `slow` (no subscriber) reach no participant.

An in-run check, such as a Test participant, sees only what it takes in a
Slot before the Duration. It can see a final sample: a Test participant
scheduled like `fast` takes the last height of `ball` at 110 ms. The Run
has no Slot at the Duration, and shutdown does not step a participant.
Thus an in-run check cannot see:

- a Message that becomes visible at or after the Duration, such as the last
  Message of `fast`;
- a Message published in the checker's own last Slot or after it, with a
  Latency above zero.

Only the Recording holds these Messages.

Check the final samples post-hoc. `sil-compare` observes each Message at
its sample time: `actual_offset_ns` is the publisher's period. With an
observation grid that ends on the Duration, the final sample is compared
like each other sample. The tests compare the Recording of this Run with a
reference computed from the hand-written consumption tables. The same
comparison with a wrong final sample of `slow` fails, and the first
divergence is at 120 ms.

## Replay recorded input into a shared library

An existing library often has its own C interface: an init, a cyclic step,
an output read and a terminate call. It does not export
`sil_participant_init`. [examples/library/](examples/library/) runs such a
library as a Process participant without changing it:

| File | Role |
| --- | --- |
| `speed_filter.h`, `speed_filter.c` | the library under test: its own API, global state, prints to stdout |
| `binding.py` | the per-library binding: symbols, C types, error codes |
| `adapter.py` | the Process participant: Step protocol, lifecycle, routes, failures |
| `filter_test.py` | the Test participant: computes every output independently |
| `signals.csv`, `mapping.json` | the recorded input and its `sil-csv` mapping |
| `manifest.py` | the Run: Replay, two library instances, Test participant |

With the staged installation on `PATH`, from the checkout root:

```sh
workdir=$(mktemp -d "$HOME/sil-library.XXXXXX")
cc -shared -fPIC -O2 -o "$workdir/speed_filter.so" examples/library/speed_filter.c
sil-csv examples/library/mapping.json examples/library/signals.csv \
    -o "$workdir/signals.mcap" --receipt "$workdir/signals.receipt.json"
python examples/library/manifest.py "$workdir/library.json" \
    --recording "$workdir/signals.mcap" --library "$workdir/speed_filter.so"
sil-run "$workdir/library.json" -o "$workdir/run-1.mcap" \
    --participant-timeout-ms 10000
sil-run "$workdir/library.json" -o "$workdir/run-2.mcap" \
    --participant-timeout-ms 10000
cmp "$workdir/run-1.mcap" "$workdir/run-2.mcap"
```

`make example-library` runs the same sequence from the source tree.
`--participant-timeout-ms` gives each library answer 10 s of wall-clock
time. A library that hangs then fails the Run instead of stopping it. The
deadline is not Manifest data, so it does not change the Recording. For the
deliberately incorrect case, build the library with `-DSPEED_FILTER_DEFECT`
and build the Manifest over that library. The Test participant then fails
the Run (exit 1) at the first output that differs:

```text
filter.fast at t=0 ns: filtered_speed_mps 4.0 differs from the independently computed 2.666666666666667
```

What the Manifest makes explicit:

- **Period.** Every participant steps every 10 ms. The adapter gets the same
  value as `--period-ns` and passes it to the library's init. The init line
  does not carry the Period, so the adapter checks `dt` at each Step and
  fails the Run when it is different.
- **Initial values.** `--parameter initial_speed_mps=…` is the library's
  state before its first cycle. `--initial speed_mps=…` is the input before
  the first recorded Message is visible. After that, each input is held
  until a newer Message replaces it.
- **Latency.** `ego.speed` declares `latency_ns` of one Period, so a
  recorded Message is visible at the first Step after its publication. The
  output Channels declare `latency_ns=0`, and the Test participant has a
  higher `priority` value than the library instances, so it runs after them
  in each Slot. It checks every output in the Slot that publishes it, the
  output of the last Step included.
- **Finite routes.** Every subscriber route fails on overflow. `ego.speed`
  routes have capacity 2: a Message waits one Period, and the next one can
  arrive before it is taken. Output routes have capacity 1.
- **Parameters.** `--parameter time_constant_s=…` configures the library.
  The two instances have different parameters and publish on different
  Channels.

**Lifecycle and isolation.** Each Step runs one library cycle and publishes
the result at the Step's time. `shutdown` calls the library's terminate. The
library keeps its state in globals and refuses a second init, but each
instance is a separate process: each has its own globals and each starts
with cycle 1. A new Run starts new processes.

**Failures.**

| What goes wrong | Result |
| --- | --- |
| the library does not load, for example a missing dependency | Manifest error (exit 2): `cannot load library '<path>': <loader message>` |
| the library does not export a symbol the binding calls | Manifest error (exit 2): `library '<path>' does not export '<symbol>'` |
| the library rejects its configuration | Manifest error (exit 2): the library path, the parameters, and the library's error code with its meaning |
| a cycle returns an error code | Run failure (exit 1) with the virtual time and the error |
| the library crashes | Run failure (exit 1): `participant '<name>' exited unexpectedly` |
| the library hangs | Run failure (exit 1) at the `--participant-timeout-ms` response deadline; without the option, the runner waits |
| the output is incorrect | Run failure (exit 1) from the Test participant |

On every path, the runner reaps the adapter process and its cooperative
descendants, and removes the Run working directory and the mapped regions
([Process participant descendants](#process-participant-descendants)).

**stdout.** The Step protocol owns stdout. Before the library loads, the
adapter moves the protocol to a private descriptor and points descriptor 1
at stderr. What the library prints stays visible on the runner's stderr and
does not corrupt the protocol. This is the lesson of the esmini proof.

### Binding another library

Replace `binding.py`. Keep its names: `Binding(library)`, `init(period_s,
parameters)`, `step(inputs)`, `output()`, `terminate()`, and the
`PARAMETERS`, `INPUTS` and `OUTPUTS` tuples. Write the `ctypes` declarations
from your library's header: every argument type, every result type, every
struct field in header order. ctypes assumes `int` for an undeclared result,
which silently truncates a `double`. Raise `BindingError` with the library's
own error code and its meaning. Do not `print` from the binding: Python's
`sys.stdout` is the protocol descriptor. Write diagnostics to `sys.stderr`.

`adapter.py` does not change. It checks that the command line gives exactly
the binding's parameters and initial inputs, and that the input and output
Channels' Schema fields are exactly `INPUTS` and `OUTPUTS`. Then declare your
Schemas, Channels and adapter commands in your Manifest. The adapter binds
one input Channel and one output Channel.

Nothing is discovered: the binding states the ABI because only the library's
header states it. There is no symbol inference.

### Library dependencies

The adapter loads the library with `dlopen`, through `ctypes.CDLL`. The
dynamic loader resolves the library's own dependencies:

- Prefer a run path in the library, for example `-Wl,-rpath,'$ORIGIN'` with
  the dependencies beside it.
- Otherwise, set `LD_LIBRARY_PATH` for `sil-run`. Each child inherits the
  runner's environment, and the kernel does not change it. Use absolute
  entries: a child starts in its own Run working directory.
- In the Example image, install the dependencies in the image, as the esmini
  proof does.

The runner resolves a relative library path in the command against the
Manifest's directory. The example's `manifest.py` writes the absolute
path. The library bytes are not in the Manifest hash, but
the Run's provenance side-car records the SHA-256 of the library, because
the command names it. The side-car does not record the library's
dependencies or the environment: pin them with the image.

The adapter itself needs `python3` with the `sil` wheel on `PATH`.

### When the Clock shim applies

Add `shim=True` to the adapter's `add_process` when the library reads the
wall clock (`clock_gettime`, `gettimeofday`, `time`) or sleeps. For a library
that sleeps, also choose the `sleep` policy: the default `"reject"` fails
each sleep with `ENOSYS`, and `"immediate"` returns at once. The shim is
preloaded into the adapter process, so it also answers the loaded library's
calls. The example library reads no clock and needs no shim. The shim's
boundaries apply: a statically linked clock read, a direct syscall, or the
vDSO is not virtualized.

### Native participant alternative

A library can also export `sil_participant_init` from
[include/sil/participant.h](include/sil/participant.h) and run in the
kernel's own process. That removes the process boundary and the per-Step
protocol line, but has limits:

| | Process participant (this adapter) | Native participant |
| --- | --- | --- |
| API | the library's own; bind it in `binding.py` | must export `sil_participant_init` and register tasks |
| crash | the child exits; Run failure; the runner cleans up | the crash ends the runner: the Recording is not finished and the runner cannot clean up |
| hang | Run failure at the response deadline | the runner hangs; the response deadline applies only to Process participants |
| global state | one set per process, so any number of instances | one set per runner process: at most one instance per Run unless state is behind the `user` pointer |
| stdout | moved off the protocol by the adapter | shared with the runner |
| cost | one protocol round trip per Step | a function call per task |

Use the Native ABI for a library you build against SiL and trust not to
crash or hang. Use this adapter for an existing library with its own API.

## Replay a selected window after a warm-up

A Run starts at Virtual time zero. To evaluate a part of a long recording,
do not seek a stateful vECU into it: its state would be wrong. Select the
window with `sil-window`, which writes a new Recording that starts at the
window. The vECU runs through a warm-up first, and only the rest is
evaluated. There is no state snapshot and no seek into a running vECU.

The worked example is in [examples/library/](examples/library/):
`history.csv` is 3 s of recorded speed, `window.json` selects 0.5 s to 2.5 s
with a 1 s warm-up, and `window-no-warm-up.json` is the negative control:
the same evaluation interval with no warm-up. `window_contract.py` writes the
`sil-compare` contract from the `sil-window` receipt. With the staged
installation on `PATH`, from the checkout root:

```sh
workdir=$(mktemp -d "$HOME/sil-window.XXXXXX")
cc -shared -fPIC -O2 -o "$workdir/speed_filter.so" examples/library/speed_filter.c
sil-csv examples/library/mapping.json examples/library/history.csv \
    -o "$workdir/history.mcap" --receipt "$workdir/history.receipt.json"
sil-window examples/library/window.json "$workdir/history.mcap" \
    -o "$workdir/window.mcap" --receipt "$workdir/window.receipt.json"
python examples/library/manifest.py "$workdir/full.json" \
    --recording "$workdir/history.mcap" --library "$workdir/speed_filter.so" \
    --duration-ns 2500000000
python examples/library/manifest.py "$workdir/windowed.json" \
    --recording "$workdir/window.mcap" --library "$workdir/speed_filter.so" \
    --duration-ns 2000000000
sil-run "$workdir/full.json" -o "$workdir/full.mcap" --participant-timeout-ms 10000
sil-run "$workdir/windowed.json" -o "$workdir/windowed-1.mcap" --participant-timeout-ms 10000
sil-run "$workdir/windowed.json" -o "$workdir/windowed-2.mcap" --participant-timeout-ms 10000
cmp "$workdir/windowed-1.mcap" "$workdir/windowed-2.mcap"
python examples/library/window_contract.py "$workdir/window.receipt.json" \
    -o "$workdir/contract.json"
sil-compare "$workdir/contract.json" "$workdir/windowed-1.mcap" "$workdir/full.mcap"
```

`make example-window` runs the same sequence from the source tree. The full
history Run is the reference. Its Duration is the window's `end_ns`, because
the CSV origin is 0. The windowed Run's Duration is the receipt's
`duration_ns`. The comparison passes: after 100 warm-up Steps, the filter
state no longer depends on where the Run started. Repeat the sequence with
`window-no-warm-up.json` and `--duration-ns 1000000000`: the comparison
fails at the first evaluated Step, because the filter starts from its
initial state.

The window document is JSON. All times are integer nanoseconds on the source
Recording's time axis:

```json
{
  "sil_replay_window": 1,
  "source_origin_ns": 500000000,
  "replay_start_ns": 500000000,
  "evaluation_start_ns": 1500000000,
  "end_ns": 2500000000,
  "channels": ["ego.speed"],
  "max_gap_ns": 10000000
}
```

| Key | What it states |
| --- | --- |
| `source_origin_ns` | the source time of Virtual time zero: Virtual time = source time − origin |
| `replay_start_ns` | the first selected instant, and the start of the warm-up |
| `evaluation_start_ns` | the end of the warm-up and the start of the evaluation interval |
| `end_ns` | the end of the window, exclusive. It becomes the Duration: `end_ns` − origin |
| `channels` | the source Channels to carry. Other Channels are not in the output |
| `hold_initial` | optional: Channels that get a held initial value (see below) |
| `source_time_fields` | optional: per Channel, the `u64` or `i64` fields that hold nanoseconds on the source time axis |
| `max_gap_ns` | optional: the longest interval a Channel may go without a Message |

The instants must obey `source_origin_ns` ≤ `replay_start_ns` ≤
`evaluation_start_ns` < `end_ns`. An origin before the replay start keeps
that offset: the first Message is then after Virtual time zero.

What the window does, and what it does not do:

- **Selection.** The Messages in [`replay_start_ns`, `end_ns`) are selected.
  A Message at the replay start is in the warm-up; a Message at the
  evaluation start is evaluated; a Message at `end_ns` is not selected.
- **Order and values.** Messages keep their stored order, so Messages at one
  timestamp keep their Publish order. Payload bytes are copied unchanged, and
  the schemas are copied from the source.
- **Source-time fields.** A payload field that holds a source time is not
  changed unless `source_time_fields` names it. A named field is rebased like
  the log time. A result outside the field type's range is rejected. A held
  Message keeps the source time in its payload, so its rebased field is
  earlier than its log time.
- **Coverage.** Each selected Channel must cover the window on its own. A
  Channel whose first Message is after the replay start is missing history.
  A Channel whose last Message is before the window's last instant
  (`end_ns` − 1) is insufficient coverage. With `max_gap_ns`, a last Message
  up to `max_gap_ns` before `end_ns` covers the end, so a periodic source
  can end one Period early. A selected Channel with no Message in the
  window is an empty selection, also when it is in `hold_initial`: a held
  value does not fill it. All three are rejected. A Channel with
  Messages only in the warm-up is accepted; the receipt reports its
  evaluation coverage as 0 Messages.
- **Gaps.** A gap stays a gap. The receipt states each Channel's longest
  interval without a Message, counted from the replay start to `end_ns`.
  With `max_gap_ns`, a longer interval is rejected.
- **Held initial value.** A Channel in `hold_initial` with no Message at the
  replay start gets its latest earlier Message, published at the replay
  start before the window's own Messages, also before other Channels'
  Messages at that instant. A Channel without an earlier Message is
  rejected as missing history. The receipt names the source time
  of each held Message. Nothing else is held and nothing is interpolated.
- **Warm-up.** The window does not initialize the vECU. The vECU runs
  through the warm-up like any other part of the Run. Choose the warm-up
  from the vECU's memory: the example's slower filter keeps 5/6 of its
  state difference per Step. Choose `source_origin_ns` so that the Steps
  land on the source Steps you compare with.

The receipt names the preparer, the SHA-256 of the source Recording, the
window document and the output; the warm-up and evaluation intervals in
source and Virtual time; per Channel its span in the source, the message
count and the first and last Virtual time in each interval, the held Message
and the longest gap; the `duration_ns` for the replaying Manifest; and the
`evaluation_window` in Virtual time, both ends included, for a comparison
contract. Exclude the warm-up from every metric: use that evaluation window
in the contract, as `window_contract.py` does. An in-run KPI of a Test
participant must also start at the evaluation window; the window document
does not reach the participants. The example's Test participant checks
every output against its own model, from Virtual time zero, which is
correct in the warm-up too; it is not a comparison with the full history. The output Recording carries
the source and window digests as MCAP metadata. The same inputs give a
byte-identical Recording, and a changed window gives a different Recording
and so a different Manifest hash.

`sil-window` exits 0 on success and 2 when the window or the source is
rejected; nothing is written then.

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

## Public shared-library acceptance

[proofs/libsafety/](proofs/libsafety/) replays a public vehicle CAN recording
into a public shared library. It uses the supported workflow: `sil-csv`,
`sil-window`, a Process participant adapter over the library's own C API, and
`sil-compare`. The library is opendbc's safety logic. The recording is one
commaCarSegments segment. [proofs/public-workloads/](proofs/public-workloads/)
pins both, and an independent reference. All 6000 observations match exactly,
and two Runs are byte-identical. A timing, a time-unit, a calibration, a crash
and a hang control each fail for their own reason.

```sh
proofs/public-workloads/run-proof.sh   # the pinned bundle; needs docker and network
proofs/libsafety/run-proof.sh build/public-workloads/bundle
```

## Public single-FMU acceptance

[proofs/openacc-controller/](proofs/openacc-controller/) replays a public
recorded car-following window into one FMU with the supported workflow:
`sil-csv`, `sil-fmu-replay` and `sil-compare` against an independent FMPy
execution. The recording is a 50 s JRC OpenACC window. The FMU is
`AccController`, a PythonFMU3 export written in this repository, not a
third-party model. [proofs/public-workloads/](proofs/public-workloads/) pins
both, and the reference. All 501 commands agree within 1e-10, the final one
at 50.1 s included, and two Runs are byte-identical. A changed-input, a
wrong-binding and a one-period-shift control each fail where the control law
predicts before the Run.

```sh
proofs/public-workloads/run-proof.sh   # the pinned bundle; needs docker and network
proofs/openacc-controller/run-proof.sh build/public-workloads/bundle
```

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
examples/fmu-replay/
                   the recorded-data FMU example: converted input authored
                   by sil-fmu-replay into one FMU, compared with a reference
examples/library/  the shared-library example: an adopter's C library with
                   its own API, bound through a Process participant adapter
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
proofs/libsafety/  public shared-library acceptance: a recorded CAN segment
                   replayed into opendbc's safety library and compared
                   with its independent reference
proofs/openacc-controller/
                   public single-FMU acceptance: a recorded OpenACC window
                   replayed into the ACC controller FMU and compared with FMPy
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
