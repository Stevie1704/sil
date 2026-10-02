# FMI importer

[Documentation index](README.md) · [Repository overview](../README.md)

Commands below run from the repository root unless stated otherwise.

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
variable type is bound by name on the command line, below. An FMI 2.0
co-simulation FMU runs through the same participant under a narrower profile;
see [FMI 2.0 co-simulation profile](#fmi-20-co-simulation-profile).

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

[examples/fmu/](../examples/fmu/) is that snippet as a Run you can execute:
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
not evidence of anything. An FMU that answers `Error` is freed without
`fmi3Terminate`, and one that answers `Fatal` is abandoned rather than
terminated, which is what FMI 3.0 requires of its importer. So the diagnostic
names the call that failed, not a cleanup call after it.

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
acceptance fixture declares — and `Float32`, `Int32`, `UInt32`, `UInt64`,
`UInt8` and `Int64`, the numeric profile of the
[C reference product](adas-reference.md) (next section). A `Boolean` is carried by a `u8` with C's own conversion — zero is
false, anything else is true — and what the FMU hands back is 0 or 1.

A binding that names any other type — a `String`, an `Enumeration`, an
`Int8`, `Int16` or `UInt16` — is reported before the FMU is
stepped, naming the type, as is one whose field type is not the one its
variable's type maps to. A `Clock` is
reported too, and for a different reason: it is driven through the variable it
gates rather than bound to a field of its own, which is the section after
next. A variable whose declared dimensions amount to one value is a scalar:
`<Dimension start="1"/>` is one value written the long way, which is how the
fixture's CAN node declares its Binary input. A numeric variable of more than
one value is an array, which has its own section below. A `Binary` variable
of more than one value is refused.

### Numeric scalars

The six numeric types are the ones the C reference product uses: `Float32`
for sensor and control values, `Int32` for signed selected IDs, `UInt32` for
counts, modes and sequence numbers, `UInt64` for Sample times, `UInt8` for
validity flags and `Int64` for signed ages. Each is carried by exactly one
field type, of its own width and kind:

| FMI type | Field type | Start value (`--start`) |
| --- | --- | --- |
| `Float32` | `f32` | a finite decimal, rounded to the nearest Float32 |
| `Int32` | `i32` | a decimal integer in [−2³¹, 2³¹ − 1] |
| `UInt32` | `u32` | a decimal integer in [0, 2³² − 1], with no sign |
| `UInt64` | `u64` | a decimal integer in [0, 2⁶⁴ − 1], with no sign |
| `UInt8` | `u8` | a decimal integer in [0, 255], with no sign |
| `Int64` | `i64` | a decimal integer in [−2⁶³, 2⁶³ − 1] |

A `u8` field carries a `Boolean` too. The variable's declared type decides
the conversion: a `Boolean` reads back as 0 or 1, a `UInt8` keeps every value
in [0, 255].

Nothing is narrowed or coerced. A `UInt64` bound to a `u32`, `i64` or `f64`
field, a `Float32` bound to an `f64`, an `Int32` bound to a `u8` and a
`Boolean` bound to an `i32` are all refused before stepping, with exit 2. So
an integer never crosses a floating-point field, and a `UInt64` above 2⁵³ —
which a double cannot hold — reaches the Recording to its last bit. A
Message's field already holds a value of the variable's own type. What the
FMU hands back fits the field.

A start value is read by the same rule, before anything is loaded:

- An integer start value is an optional `-` and ASCII digits, and nothing
  else: no `+`, spaces, underscores, decimal point, exponent, `0x` or
  `true`/`false`. A value outside the type's range is refused; so is any
  `-` for an unsigned type, `-0` included.
- A `Float32` start value uses the decimal grammar `sil-csv` reads an `f32`
  cell in. It is read as the nearest binary64. Then it is rounded to the
  nearest Float32, with ties to even. `sil-csv` uses the same two steps, so
  one decimal is one Float32 on both paths into a Run. `0.1` is
  `0.100000001490116…`; `16777217` is `16777216`. A value that rounds to
  infinity, a non-zero value that rounds to zero, and `inf` or `nan` are
  refused.
- A `Boolean` start value is `true` or `false`; `1` and `0` are refused.
- A start value of a scalar is one value. An array's start lists all its
  values (next section).

An input or parameter start is written in the instantiated state, before
initialization mode; a structural parameter inside Configuration Mode. A call
that answers other than `fmi3OK` names more than the call: a start value
names its variable and the state it was written in, and a Step names the
Channel and the variables one call carried —
`writing Channel 'sensor.in' into FMU variables 'UInt64_input': fmi3SetUInt64
returned Error`.

[examples/fmu-numeric/](../examples/fmu-numeric/) replays a Recording of all
four types into `Feedthrough` and compares what comes back with a reference
written by hand. `make example-fmu-numeric` runs it twice and `cmp`s the two
Recordings.

### Fixed-size numeric arrays

An FMI array whose dimensions are all literal is carried by one fixed-count
Schema field. This is the profile, and nothing outside it is mapped:

| Property | Supported | Refused before stepping (exit 2) |
| --- | --- | --- |
| Element type | `Float32`, `Float64`, `Int32`, `UInt32`, `UInt64`, `UInt8`, `Int64` | `Boolean` and `Binary` arrays; every type that is not mapped as a scalar |
| Dimensions | each `<Dimension start="N"/>` with a literal N ≥ 1, any number of them | a `<Dimension valueReference=…>` (sized by a structural parameter), a start of 0 or below |
| Value count | the product of the dimensions, above 1 (one value is a scalar) | a count whose buffer of the element type overflows `size_t`, or that the Importer cannot allocate |
| Field | the scalar's field type (table above), with `count` equal to the value count | another type, another count, or a scalar field |

The field holds the values in the order FMI 3.0 defines for an array: row
major, so the last dimension varies fastest. A `[2,3]` matrix `m` is the field
`[m[0][0], m[0][1], m[0][2], m[1][0], m[1][1], m[1][2]]`. The Importer does
not reorder anything: the FMU's flat value buffer is the field. A `[3,2]`
variable and a `[2,3]` variable both have six values, but they are different
shapes. Inspection reports each variable's `dimensions` and `value_count`,
and `sil-fmu-replay` and `sil-fmu-couple` receipts record the dimensions and
value count of each array they bind or start. A connection between two FMUs
must join variables with the same dimensions, as it must join one type and
one unit.

All variables of one type on one Channel are still read or written in one
FMI call. The call counts the variables and the values separately: an
`[8]`, a `[3]` and a scalar `Float32` on one Channel are one
`fmi3SetFloat32` with `nValueReferences` 3 and `nValues` 12. Each buffer is
allocated once, at initialization, with exactly that many values, and a
write with a different number of values is refused.

A start value of an array (`--start`, or `value` in an authoring document)
lists every value in row-major order, separated by single spaces:
`--start "bias=0.5 -1 2.25 100 -0.125 7"` for a `[2,3]` `Float64`. Each
value is read by the scalar grammar of its type (table above). The count
must equal the value count. One value is not broadcast, missing values are
not filled with zeros, and a start with too many values is refused. Spaces
other than one between two values are refused too. The start is written in
the lifecycle state of a scalar start: an input or parameter in the
instantiated state, before initialization mode.

Out of scope: `Boolean`, `Binary` and the unmapped integer types as arrays;
dimensions resolved through a structural parameter's value; changing a
dimension during a Run; variable-length Channels. A Channel field has the same
count for the whole Run.

[examples/fmu-array/](../examples/fmu-array/) replays a Recording of `[8]`
sensor arrays, a `[3]`, two scalars and a `[2,3]` matrix into the test FMU
`tests/fixtures/fmi_array.c` and compares every element with a reference
written by hand. `tests/test_fmi_arrays.py` also compares every element with
an independent execution of the FMU through its C interface, runs the inline
and the shared-memory transports, and runs a coupled matrix feedback loop.

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
[docs/adr/0001-connected-fmus-in-one-process-participant.md](adr/0001-connected-fmus-in-one-process-participant.md)
and [docs/adr/0002-a-replayed-terminal-lands-on-its-own-instant.md](adr/0002-a-replayed-terminal-lands-on-its-own-instant.md).

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
| facts | FMI version, interfaces and co-simulation capabilities, platform binaries, and each variable's type, causality, variability, start, unit, dimensions, value count, `maxSize`, `mimeType` and Clocks; the terminals and the FMI-LS-BUS manifest |
| `unusable` | why no Run can drive the archive: unreadable archive or `modelDescription.xml`, an FMI version other than 3.0 or 2.0, no co-simulation interface, no binary for this platform, and for FMI 2.0 every variable of a type outside the FMI 2.0 profile |
| `unmappable`, per variable; `unsupported`, per terminal | why no Channel can carry the variable, or no group can connect the terminal: String, Enumeration and the unselected integer types, arrays outside the fixed-size numeric profile, a Clock outside the triggered profile, a terminal outside the BUS profile |
| `unverified` | what only a loaded binary can answer: whether the library and its dependencies load, whether initialization succeeds, a required execution tool, the files read from `resources/` |

A variable no Channel names is never touched, so an `unmappable` variable does
not make the FMI 3.0 archive unusable. `Feedthrough` declares every FMI type
and runs. The FMI 2.0 profile is stricter: its `Feedthrough` is `unusable`,
and the reason names each `String` and `Enumeration` variable.

The readable report names the profile the archive is checked against —
`FMI 3.0 co-simulation` or `FMI 2.0 co-simulation` — from the `fmiVersion`
the archive declares. The JSON report states that version in
`facts.fmi_version`. For FMI 2.0, `facts.instantiation_token` holds the
`guid`, and `platform` is the FMI 2.0 directory (`linux64`, or `darwin64` on
a development Mac), also when the archive is refused. On a host that FMI 2.0
has no directory for, `platform` is `null`.

`--mapping` checks a proposed single-FMU mapping. The document is the init
line's `schemas` and `channels` and the importer's `--bind` and `--start`
arguments; [examples/fmu/mapping.json](../examples/fmu/mapping.json) is the one
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

## FMI 2.0 co-simulation profile

The same participant, `python -m sil.fmi model.fmu`, drives an FMU that
declares `fmiVersion="2.0"`. The Importer reads the version from
`modelDescription.xml` and selects the FMI 2.0 interface. Nothing changes at
the Process boundary: the Manifest, the step protocol, the Recording and the
Native ABI are the ones above ([ADR 0001](adr/0001-connected-fmus-in-one-process-participant.md)).
The consumer is `OSMPDummySensor` from osi-sensor-model-packaging `v1.6.0`
(#230), which upstream exports as FMI 2.0 only.

This is the whole profile. It is not general FMI 2.0 conformance.

| Item | Profile |
| --- | --- |
| Interface | Co-Simulation. Model Exchange is refused |
| Platform | Linux x86-64, `binaries/linux64/`. `darwin64` is loaded on a Mac for development only |
| Variable types | scalar `Real`, `Integer` and `Boolean`. An archive with a `String` or `Enumeration` variable is refused, and the reason names each one |
| Parameters | `--start` sets a `parameter` (`fixed` or `tunable`) or an input in the instantiated state, before `fmi2SetupExperiment`. A `calculatedParameter` takes no start value: the FMU computes it in initialization. It is read after initialization: `--bind` it to a field of an output-direction Channel, and each Step publishes its value |
| Lifecycle | `fmi2Instantiate` → start values → `fmi2SetupExperiment` (start 0, no tolerance, no stop time) → `fmi2EnterInitializationMode` → `fmi2ExitInitializationMode` → one `fmi2DoStep` per Step → `fmi2Terminate` → `fmi2FreeInstance` |
| Resources | the extracted `resources/` directory as a `file://` URI, also when the archive has none |
| Step | the fixed communication step of the Manifest. No variable step, even when the FMU declares `canHandleVariableCommunicationStepSize` |
| Status | `fmi2OK` continues. `fmi2Warning` continues and writes `sil.fmi: <call> returned Warning` to stderr. `fmi2Discard`, `fmi2Error`, `fmi2Fatal` and `fmi2Pending` fail the Run and name the call. After a failing status the instance is freed without `fmi2Terminate`; after `fmi2Fatal` no call is made |
| Callbacks | the logger, and the C library's `calloc` and `free` as `allocateMemory` and `freeMemory`. `stepFinished` is NULL |
| Not supported | `fmi2GetFMUstate`/`fmi2SetFMUstate`, directional derivatives, input derivatives, asynchronous `fmi2DoStep`, a group of connected FMUs (`--instance`) |

Each FMI 2.0 type is carried as the FMI 3.0 type of the same width, so the
binding rules above apply unchanged:

| FMI 2.0 type | Carried as | Field type | Start value (`--start`) |
| --- | --- | --- | --- |
| `Real` | `Float64` | `f64` | a decimal number |
| `Integer` | `Int32` | `i32` | a decimal integer in [−2³¹, 2³¹ − 1] |
| `Boolean` | `Boolean` | `u8` | `true` or `false` |

A Real mapping is derived from the field names, as for Float64. An Integer or
Boolean variable, and a `calculatedParameter`, is bound with `--bind`. A diagnostic names the type as it is
carried: a `Real` variable is a `Float64` variable there.

The FMI 2.0 logger is a C variadic function. The Importer prints the message
the FMU passes without formatting its arguments, as FMPy does.

`fmi2Warning` continues the Run. This differs from FMI 3.0 above, where
`Warning` aborts: the FMI 2.0 profile was specified this way in #191, and the
FMI 3.0 behavior is unchanged.

The acceptance evidence is [proofs/fmi2-importer/](../proofs/fmi2-importer/):
the Modelica Reference FMUs `v0.0.41` compared with FMPy 0.3.26, the
`Feedthrough` refusal, and the `OSMPDummySensor` lifecycle. Two
`OSMPDummySensor` instances cannot share one process, because two OSI FMUs
abort in the Protobuf pool
([proofs/osmp-sensor](../proofs/osmp-sensor/README.md#one-process)). That is
a limit of the FMU build, not of the Importer. The OSMP binary variables
(pointers in `Integer` variables) are #244.
