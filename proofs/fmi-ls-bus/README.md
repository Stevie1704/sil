# Acceptance fixture: the FMI-LS-BUS CAN demo FMUs

The external integration target for the [CAN FMU communication and replay
milestone](https://github.com/Stevie1704/sil/milestone/5), pinned, built,
inspected, executed, and measured against the SiL release that exists today.
This directory is the whole evidence gate for [issue
#137](https://github.com/Stevie1704/sil/issues/137).

Nothing here is part of the SiL product, and nothing here implements the
milestone. It fixes what the implementation issues (#138 – #141) are aimed at,
so that each of them is measured against artifacts and expectations that were
written down before the implementation was.

Three steps are the other way round, and they are the last ones: since [issue
#139](https://github.com/Stevie1704/sil/issues/139) the proof also drives the
same pinned node with **the checkout's** FMI Importer and judges the Recording
against the same expected exchange; since [issue
#140](https://github.com/Stevie1704/sil/issues/140) it connects two instances of
that node through the pinned **bus simulation FMU** and judges that Recording
too; and since [issue
#141](https://github.com/Stevie1704/sil/issues/141) it removes one of those two
nodes, **replays its Recording** into the terminal it fed, and compares what
everything that stayed produced with what it produced live. The gate measures
the release; those steps measure the tree, and [say which
tree](evidence/identity.txt).

**This is an interoperability and replay proof, not full FMI-LS-BUS
conformance and not ADAS behavior validation.** It covers two CAN demo FMUs of
one layered standard, at one revision, on one machine class, over the CAN
operations and the Clock profile [published below](#the-supported-bus-profile-as-exercised).
Other buses, general rollback support, and any performance claim these Runs do
not exercise are outside it.

## What this gate establishes

| Question | Answer |
| --- | --- |
| Are the upstream CAN FMUs reproducibly available on the supported machine class? | Yes — built from pinned sources, with two documented compile-time definitions and no edit to any upstream file |
| Does upstream ship a built artifact to pin instead? | No. No release, no tag, no binary: the demos are C sources plus a packaging script that produces a source-code FMU |
| Do the artifacts implement the stable BUS 1.0.0 CAN subset? | The CAN operation bytes are v1.0.0's, verified against the header history; the artifacts' own metadata says `1.0.0-beta.1`, and no upstream revision says otherwise |
| Is there a runnable reference exchange? | Yes — the node FMU driven by an independent FMI 3.0 importer, matching payloads and event times stated from the sources beforehand |
| What does current SiL do with the same FMU? | It fails, twice and for two different reasons — both recorded, both in the Importer, neither in the kernel contract |
| What does the checkout do with the same FMU? | It drives it: both step grids of the expected exchange, matched whole, in a Run whose Recording reproduces bit for bit |
| What does the checkout do with both FMUs connected? | It runs them as one group: two nodes exchanging frames through the bus FMU, every payload, count and event time as stated beforehand, and bit-identical across two Runs |
| Can one of those nodes be replaced by its own Recording? | Yes. With the live Publisher gone and its Channel replayed into the terminal it fed, every retained participant produces the same operations, in the same order, at the same event times — on both step grids |

## The artifacts

### Upstream

| Field | Value |
| --- | --- |
| Project | [modelica/fmi-ls-bus-examples](https://github.com/modelica/fmi-ls-bus-examples) — the Modelica Association's demo FMUs for FMI-LS-BUS |
| Revision | `cc42cacd26c7f5edbb20959b0e0c56922c2f0cc2` (`main`, 2026-06-10) |
| Demos used | `can-node-triggered-output` → `DemoCanNodeTriggeredOutput.fmu`; `can-bus-simulation` → `DemoCanBusSimulation.fmu` |
| FMI-LS-BUS headers | [modelica/fmi-ls-bus](https://github.com/modelica/fmi-ls-bus) `468127f21f4c4076b796e3ad40bfc7959ed0a174`, the revision upstream's own `PackFmu.py` names |
| FMI version | 3.0, co-simulation only |
| License | BSD-2-Clause for both repositories; the notice is packed into every built FMU at `documentation/licenses/LICENSE.txt`. Recorded in [THIRD-PARTY-NOTICES.md](../../THIRD-PARTY-NOTICES.md) as built by a proof and shipped in no SiL artifact |

Upstream publishes **no release and no tag**, so a version number is not
available to pin: the revision is the pin. The packaged FMU is a *source-code*
FMU — sources, descriptions, and headers, with no `binaries/` at all — so the
fixture build compiles it as well.

### Toolchain

| Field | Value |
| --- | --- |
| Base image | `python:3.13.7-slim-bookworm`, pinned to its **linux/amd64** manifest digest `sha256:78144946…6db7` |
| Compiler | Debian bookworm `build-essential` and `cmake`, as installed in that image |
| FMU compiler and FMI bindings | [FMPy](https://github.com/CATIA-Systems/FMPy) `0.3.32` |
| Machine class | `linux/amd64`, the one the SiL release supports |

The platform is part of the base image pin rather than taken from the host: a
fixture built for a developer's own architecture would not be the fixture CI
executes.

### What the build changes, and what it does not

Two compiler definitions, recorded in
[`build-fixture.sh`](build-fixture.sh) and visible in
[`evidence/fixture-build.log`](evidence/fixture-build.log). No upstream
source, description, or header file is edited.

| Definition | Why |
| --- | --- |
| `FMU_IDENTIFIER_H` | Upstream's `FmuIdentifier.h` defines `FMI3_FUNCTION_PREFIX` unconditionally, so the FMU would export `DemoCanNodeTriggeredOutput_fmi3InstantiateCoSimulation`. `fmi3Functions.h` reserves that for source and static-library distribution: "For FMUs compiled in a DLL/sharedObject, the 'actual' function names are used and 'FMI3_FUNCTION_PREFIX' must not be defined." Defining the header's include guard makes it a no-op |
| `_strdup=strdup` | Upstream's `Fmu.c` calls MSVC's `_strdup` unconditionally. A non-MSVC toolchain *links* a shared object with `_strdup` undefined and only fails at load time: `undefined symbol: _strdup`. The definition maps that one call onto the standard function |

Both are consequences of the same upstream assumption — that these demos are
built by MSVC, or by an importer that compiles sources into its own process —
and both are build-time only. Neither changes what the FMU computes.

One file is *added* rather than changed, and only for the rejected revision
built beside the fixture: upstream's own `LICENSE.txt`, taken from the merged
revision that carries it, placed where that revision's packaging script looks
for it. The selected revision finds the notice in its own checkout and the
added copy goes unused. It is packaging, not behavior — the notice ends up
inside the built FMU at `documentation/licenses/LICENSE.txt`, which is where
upstream's script puts it either way.

### Identity

The FMU archives are built rather than downloaded, so the fixture is pinned
twice: once by what upstream contributed, and once by what this image built
out of it.

**Fixture digest** — one digest over every archive member that comes from
upstream, `binaries/` excluded, computed by `inspect_fixture.py`. It says
nothing about a compiler, and it changes when anything inside an FMU's
sources, descriptions or headers does:

```
9974aa8e4c9e1b50e43d7433bdaef72e14b5d9e23997e1600a9725c92c5148e3
```

**Archive digests** — the built FMUs as executed, binaries included:

```
58ff71a28bb2a9b6021bbf7d7d286f0a219056ad772ead7ecef1e2ba2bca71c1  DemoCanNodeTriggeredOutput.fmu
c475f76c0530d5a5aaf38a5bfde0ad31d2bfb2b48a0cf1ea6dd9bb38649fa041  DemoCanBusSimulation.fmu
```

Pinning those is only worth doing because they reproduce: the image build
builds the selected revision a second time into another directory and
compares the archives byte for byte
([`evidence/repeat-build.log`](evidence/repeat-build.log)). Two things make
that possible — the archives are packed with one fixed timestamp per member,
and the pinned toolchain compiles the same sources into the same bytes even
though each build runs in a different temporary directory.

`run-proof.sh` checks both pins and stops if either differs. Every member
digest is in [`evidence/profile.json`](evidence/profile.json).

## Why this revision, and what "BUS 1.0.0" means here

The stable layered standard is **FMI-LS-BUS v1.0.0**, released 2025-07-18.
Targeting it was the instruction; verifying what the artifacts actually
declare was the work. Three separate facts, each checked rather than assumed:

**1. No upstream CAN artifact declares 1.0.0 in its layered-standard
manifest.** Both `main` and the open pull request ship
`extra/org.fmi-standard.fmi-ls-bus/fmi-ls-manifest.xml` with
`fmi-ls:fmi-ls-version="1.0.0-beta.1"`, and an XSD location pinned to a 2023
commit. The pull request updates the Binary variables' `mimeType` to
`version="1.0.0"` while leaving that manifest at `1.0.0-beta.1`, so it
declares both versions at once.

**2. The CAN operation bytes are v1.0.0's.** The headers upstream pins
(`468127f2`) are an ancestor of the `v1.0.0` tag, and the difference between
them, for everything that decides a byte, is nil:

| Header | `468127f2` → `v1.0.0` |
| --- | --- |
| `fmi3LsBusCan.h` | comments only — wording and copyright years. Every operation code and structure identical |
| `fmi3LsBus.h` | comments, plus one added `fmi3LsBusBoolean` typedef. Operation header unchanged: 8 bytes, little-endian `opCode` and `length` |
| `fmi3LsBusUtilCan.h` | comments only |
| `fmi3LsBusUtil.h` | comments, plus two internal buffer-writing macros factored out. Same serialisation |

So the fixture exercises the stable 1.0.0 CAN operation layout, while the
artifacts' metadata says `1.0.0-beta.1`. **A profile contract keyed on the
declared version string would reject these artifacts; one keyed on the CAN
operation layout accepts them.** Downstream issues get the second, and the
declared strings are published below so a check on them is a decision rather
than a surprise.

**3. The obvious alternative was built and driven, not argued about.** The
head of the open pull request [Fix and update CAN
FMUs](https://github.com/modelica/fmi-ls-bus-examples/pull/11)
(`de019a6efbad810795835f2fd9bbf9e62eb451b9`) is the only upstream state that
labels the CAN demos `1.0.0`. It also fixes `_strdup` in source. It is built
beside the fixture and driven by the same reference exchange, and it fails:

```
Trace/0: Get TX clock of 1
Error/3: Getting clocked binary variable in current state is not allowed
logStatusError/3: fmi3GetBinary: Invalid call with value reference 1
```

The Clock reads active, and the read of the buffer it gates is then refused.
Its added state check on the clocked Binary variable requires the output Clock
to still be active when `fmi3GetBinary` is called, while its `fmi3GetClock`
clears that Clock on read — so the read order FMI 3.0 prescribes, Clock first
and clocked variable second, cannot succeed. Kept as
[`evidence/alternative-exchange.txt`](evidence/alternative-exchange.txt) and
[`evidence/alternative-fmu.log`](evidence/alternative-fmu.log), with its build
log and profile beside them. `run-proof.sh` fails if that revision
ever starts passing, so this selection cannot quietly go stale.

**Selected: `main`, with the metadata gap recorded.** A merged revision whose
FMU can be driven beats an unmerged one whose FMU cannot, and the version
label is the lesser claim: the bytes are 1.0.0's either way.

## The supported profile

What an importer must do to drive this fixture, from
[`evidence/profile.txt`](evidence/profile.txt).

### The CAN node — `DemoCanNodeTriggeredOutput`

| Declaration | Value |
| --- | --- |
| Interface | Co-simulation, FMI 3.0, `modelIdentifier` `DemoCanNodeTriggeredOutput` |
| `hasEventMode` | `true` — and mandatory: instantiating with `eventModeUsed` false is refused |
| `canHandleVariableCommunicationStepSize` | `true` |
| `canReturnEarlyAfterIntermediateUpdate`, `providesIntermediateUpdate` | `false` — no early return, no intermediate update |
| `canGetAndSetFMUState`, `canSerializeFMUState` | `false` — **no rollback**, so an importer cannot retract a step it took too far |
| `DefaultExperiment` | `startTime` 0, `stepSize` 0.001 |
| `CanChannel.Rx_Data` | `Binary`, input, discrete, `maxSize` 2048, clocked by `CanChannel.Rx_Clock`, `mimeType` `application/org.fmi-standard.fmi-ls-bus.can; version="1.0.0-beta.1"` |
| `CanChannel.Tx_Data` | `Binary`, output, discrete, `maxSize` 2048, clocked by `CanChannel.Tx_Clock`, same `mimeType` |
| `CanChannel.Rx_Clock` | `Clock`, input, `intervalVariability` `triggered` |
| `CanChannel.Tx_Clock` | `Clock`, output, `intervalVariability` `triggered` |
| `org.fmi_standard.fmi_ls_bus.Can_BusNotifications` | `Boolean` structural parameter, fixed, start `false` — left at its default here |
| `time` | `Float64`, independent |
| Terminal | `CanChannel`, kind `org.fmi-ls-bus.network-terminal`, matching rule `org.fmi-ls-bus.transceiver`, grouping the four variables above as members `Rx_Data`, `Rx_Clock`, `Tx_Data`, `Tx_Clock` |

The only `Float64` variable is `time`, which is the independent variable. **An
importer that maps `Float64` inputs and outputs finds nothing to map here**:
everything this FMU communicates is a Binary buffer gated by a Clock.

### The required lifecycle

1. `fmi3InstantiateCoSimulation` with `eventModeUsed` **true**. False is
   refused: the FMU logs `Event mode is must be supported by the importer to
   use this FMU.` and returns no instance.
2. `fmi3EnterInitializationMode` / `fmi3ExitInitializationMode`. With Event
   Mode in use, initialization ends **in Event Mode**, not in Step Mode.
3. Handle that first event — the node's bus configuration is already waiting
   in it — then `fmi3EnterStepMode`.
4. Per step: `fmi3DoStep`. When it reports `eventHandlingNeeded`,
   `fmi3EnterEventMode`, handle the event, `fmi3EnterStepMode`.
5. In an event: `fmi3GetClock(CanChannel.Tx_Clock)`; if active,
   `fmi3GetBinary(CanChannel.Tx_Data)`; then `fmi3UpdateDiscreteStates` until
   it stops asking for another discrete-state update. The Clock reads active
   exactly once per activation — the FMU clears it on the read — and the
   buffer is defined only for that activation.
6. To receive, the mirror: `fmi3SetBinary(CanChannel.Rx_Data)` and
   `fmi3SetClock(CanChannel.Rx_Clock)` in Event Mode. Stated from upstream's
   `App.c`, and **not exercised by this gate**: nothing here sends the node a
   frame. #140 is where that half is first driven.

`fmi3UpdateDiscreteStates` reports **no next event time** on this FMU, and
`fmi3DoStep` never returns early. An operation is therefore observable only at
the communication point of the step that produced it, and an importer cannot
be told when to step next. Both halves are checked rather than asserted: every
event in the reference exchange carries the next event time the FMU declared,
and every expected event declares it absent.

### The bus simulation FMU — `DemoCanBusSimulation`

Not driven by the gate itself; the last three steps connect two nodes through
it, and then replace one of them with its own Recording.
Its description is published here so that work started from a stated contract
rather than from a reading of the sources:

- `isBusSimulationFMU` is `true` in its layered-standard manifest, which is
  also at `1.0.0-beta.1`;
- two network terminals, `Node1` and `Node2`, each grouping a `Binary`
  `Rx_Data` input and `Tx_Data` output of `maxSize` 2048 with a `Rx_Clock` and
  a `Tx_Clock`;
- **the bus FMU's `Tx_Clock`s are `input` clocks with `intervalVariability`
  `countdown`**, not the `triggered` output Clocks the node carries. Driving
  it means reading a countdown interval and activating the Clock, which is a
  larger Clock profile than #139 needs — and the interval it states,
  `(44 + dataLength) / baudRate` seconds, falls between two Slots of any step
  period a Manifest here declares;
- one `Float64` `BusErrorProbability` input, discrete, start `0.0` — the only
  scalar either FMU exposes as more than the independent variable;
- it declares `providesEvaluateDiscreteStates` `true`, which the node does
  not.

Full declaration in [`evidence/profile.txt`](evidence/profile.txt).

### What is outside the profile

CAN FD and CAN XL transmit operations, `ArbitrationLost`, `BusError`, `Status`
and `Wakeup` operations, FlexRay, rollback, early return, intermediate update,
and any FMU state serialisation. The decoder in
[`can_operations.py`](can_operations.py) names each of those by its standard
name and rejects it rather than skipping it, so a payload that leaves the
profile fails loudly.

`Confirm` left that list when the nodes were connected to the bus: the bus
simulation FMU answers a transmitting node with one, so it is a feature the
fixture actually exercises rather than a claim about the standard. Two
behaviors of the bus FMU stay outside it and are recorded here instead of
tested: the `DiscardAndNotify` arbitration-lost behavior, which nothing in this
fixture configures, and the `BusError` operation upstream draws from `rand()`,
which is why every Manifest below sets `BusErrorProbability` to `0.0` rather
than leaving a Run's result to a pseudo-random sequence.

## The supported BUS profile, as exercised

Everything above, stated once in the vocabulary a consumer needs before
pointing this at their own FMUs. Every row is a fact the Runs below actually
exercise, not a capability the standard defines.

| What | Exercised |
| --- | --- |
| **FMI version** | 3.0, co-simulation, `hasEventMode` true and `eventModeUsed` required |
| **Layered standard** | `org.fmi-standard.fmi-ls-bus`. The artifacts' `fmi-ls-manifest.xml` and `mimeType` both declare `1.0.0-beta.1`; the CAN operation bytes are v1.0.0's, [checked header by header](#why-this-revision-and-what-bus-100-means-here). The profile is keyed on the media type `application/org.fmi-standard.fmi-ls-bus.can`, not on the version string |
| **CAN operations** | `Configuration` (`CanBaudrate`, `ArbitrationLostBehavior`), `CanTransmit` (11-bit ID, no IDE, no RTR), `Confirm`. Every other operation the standard defines is [rejected by name](#what-is-outside-the-profile) |
| **Clock profile** | `Rx_Clock` input `triggered`; `Tx_Clock` either output `triggered` (the node raises it) or input `countdown` (the group raises it at the instant the interval ends). A countdown interval is read as the exact fraction the FMU states and must be a whole number of nanoseconds. No `periodic` Clock |
| **Topology** | A star with one `isBusSimulationFMU=true` end per connection and one node end; both ends transceiver terminals of the same layered-standard version declaring the identical `mimeType`. Two nodes and one bus here. A terminal takes its frames from a connected peer **or** from an in-direction Channel, never both |
| **Payload limits** | Every Binary variable of both FMUs declares `maxSize` 2048; every Channel carries 2048 payload bytes beside a `u16` length and a `u64` event time. A payload above a receiving variable's `maxSize` aborts the Run rather than being truncated |
| **Timing semantics** | Three times, never folded into one another: the **FMI event time** the Message states, the **publication Slot** the Recording stamps, and the **delivery time** one Latency later. The group stops at communication points between Slots — 480 us for a four-byte frame at 100 000 bit/s. A replayed activation is raised at the instant its Message states, which a boundary Channel of `latency_ns` 0 makes reachable |
| **Determinism** | One Manifest, run twice, bit-compared, per Manifest. Two Runs of two Manifests are compared over their shared Message streams instead |
| **Not exercised** | CAN FD, CAN XL, FlexRay, `ArbitrationLost`, `BusError`, `Status`, `Wakeup`, `DiscardAndNotify`, rollback (`canGetAndSetFMUState` is false on both FMUs), early return, intermediate update, FMU state serialisation, and any performance claim |

## The expected exchange

[`expected.json`](expected.json) states what the node emits **before any Run**,
from upstream's `App.c` and the FMI-LS-BUS headers:

- at `t = 0`, in the initialization event: one `Configuration` operation
  setting the CAN baud rate to 100 000, and one setting the arbitration-lost
  behavior to `BufferAndRetransmit` — 23 bytes,
  `400000000d00000001a0860100400000000a0000000401`;
- every 300 ms thereafter: one `CanTransmit` operation, ID `0x1`, no IDE, no
  RTR, payload `01 02 03 04` — 20 bytes,
  `1000000014000000010000000000040001020304`.

Two cases are run, and the difference between them is the point:

| Case | Step | Events observed at |
| --- | --- | --- |
| `aligned` | 100 ms | 0 ms (configuration), 300 ms, 600 ms, 900 ms |
| `quantised` | 250 ms | 0 ms (configuration), 500 ms, 750 ms, 1000 ms |

The node generates its frames at 300 ms, 600 ms and 900 ms in **both** cases —
its own log says so, in
[`evidence/reference-fmu.log`](evidence/reference-fmu.log):
`Transmitting CAN frame with ID 1 at internal time 0.300000`. With a 250 ms
step they are observable only at the end of the step that crossed those
instants. **FMI event time and the time an importer can see the event are not
the same quantity**, and a later Channel publication time is a third. #139
carries that distinction into the Channel itself — a clocked Message states
the FMI event time it belongs to beside the payload — and the connected
exchange below inherits it from here.

## The expected connected exchange

[`connected_expected.json`](connected_expected.json) states what **two nodes
through the bus FMU** produce, written from both FMUs' sources before any Run.
The topology is the one the bus declares: `node1.CanChannel = bus.Node1` and
`node2.CanChannel = bus.Node2`, two instances of one node archive attached to
the two terminals of one bus.

What the bus adds to the node's own behavior is arbitration and a transmission
time:

- each node's configuration reaches the bus in the initialization event, and
  the bus enables communication only once **both** terminals configured the
  same baud rate — so nothing is transmitted at `t = 0`;
- a frame handed to a terminal is queued, and the bus states its next
  transmission as a countdown Clock interval of `(44 + dataLength) / baudRate`
  seconds: 48/100 000 s, which is **480 000 whole nanoseconds**;
- when that interval ends, the queued frame of lowest CAN ID wins. Its
  originator is answered with a `Confirm` operation and every other terminal
  is handed the frame itself. The next queued frame is then scheduled, one
  transmission time later.

Both nodes offer CAN ID `0x1` at the same instant, so the exchange is:

| Event time | `node1.CanChannel` | `node2.CanChannel` | `bus.Node1` | `bus.Node2` |
| --- | --- | --- | --- | --- |
| 0 ms | `Configuration` ×2 | `Configuration` ×2 | — | — |
| 300 ms | `CanTransmit` | `CanTransmit` | — | — |
| 300.48 ms | — | — | `Confirm` | `CanTransmit` |
| 300.96 ms | — | — | `CanTransmit` | `Confirm` |

**480 us is no Slot of either step grid.** That is the point of this case: an
arrangement of separately stepped participants would deliver both frames at the
subscriber's next activation — one instant, with nothing left to tell the two
transmissions apart. The group stops at instants the kernel's Slot grid does
not contain, and what reaches the Recording is a Message published in the Slot
the importer was activated in, stating the instant the FMUs stood on. The
boundary decision and its evidence are in
[`docs/adr/0001-connected-fmus-in-one-process-participant.md`](../../docs/adr/0001-connected-fmus-in-one-process-participant.md).

The same two grids are run, and the `quantised` one ends on an honest edge: its
last Step ends at the Run's Duration, so the frames the bus queues at that
instant would be transmitted 480 us after the Run is over, and never are.

## Results

| Acceptance | Evidence |
| --- | --- |
| The pinned revision builds into a loadable FMU on the supported machine class | both FMUs carry `binaries/x86_64-linux/…so`, each loaded with every symbol resolved and `fmi3InstantiateCoSimulation` looked up during the build — [`fixture-build.log`](evidence/fixture-build.log) |
| The fixture is identified by content, not by build time | fixture digest `9974aa8e…48e3`, checked by `run-proof.sh` — [`profile.json`](evidence/profile.json) |
| Descriptions, BUS manifests and terminals are published | [`profile.txt`](evidence/profile.txt), [`profile.json`](evidence/profile.json) |
| A runnable reference exchange matches expectations written from upstream's sources, not captured from a Run | `aligned: 4 events, all as expected` / `quantised: 4 events, all as expected` — [`reference-exchange.txt`](evidence/reference-exchange.txt), trace in [`reference-exchange.json`](evidence/reference-exchange.json) |
| The rejected upstream revision is rejected on evidence | `fmi3GetBinary failed with status 3 (error)`, on the buffer whose Clock `fmi3GetClock` had just reported active — [`alternative-exchange.txt`](evidence/alternative-exchange.txt), [`alternative-fmu.log`](evidence/alternative-fmu.log) |
| Current SiL cannot drive the FMU, and says why | exit 1, `participant 'importer' failed: … fmi3InstantiateCoSimulation returned no instance for 'DemoCanNodeTriggeredOutput'`, under the FMU's own `Event mode is must be supported by the importer to use this FMU.` — [`sil-no-channels.txt`](evidence/sil-no-channels.txt) |
| Current SiL cannot map the FMU's Binary variables, and says why | exit 2, `Channel 'can.Tx' declares schema field 'CanChannel.Tx_Data_length', which is no output variable of FMU 'DemoCanNodeTriggeredOutput'` — [`sil-binary-channel.txt`](evidence/sil-binary-channel.txt) |
| The gap is in the Importer, not in the kernel's Channel contract | the same Manifest's bounded CAN frame Channel is accepted and sized: `observer <- can.Tx  cap 2 x 2050 B` — [`footprint.txt`](evidence/footprint.txt) |
| The checkout's Importer drives the node on both step grids | `aligned: 4 events, all as expected` / `quantised: 4 events, all as expected`, judged on the Recording — [`clocked-aligned.txt`](evidence/clocked-aligned.txt), [`clocked-quantised.txt`](evidence/clocked-quantised.txt) |
| A clocked Run reproduces | `deterministic: 6c85facd…2f81`, two Runs of one Manifest bit-compared — [`clocked-identity.txt`](evidence/clocked-identity.txt) |
| The checkout's Importer connects two nodes through the bus FMU | `aligned: 20 events on 4 terminals, all as expected` / `quantised: 16 events on 4 terminals, all as expected`, judged on the Recording — [`connected-aligned.txt`](evidence/connected-aligned.txt), [`connected-quantised.txt`](evidence/connected-quantised.txt) |
| A frame reaches its peer a transmission time later, not a Slot later | `bus.Node1 event 300480000 ns published 300000000 ns 12 B Confirm` beside `bus.Node2 … 20 B CanTransmit`, and the losing frame at `300960000 ns` — [`connected-aligned.txt`](evidence/connected-aligned.txt) |
| A connected Run reproduces | `aligned deterministic: b37f02e7…b24a` and `quantised deterministic: c71aaa20…e13b`, one Manifest at a time, bit-compared — [`connected-identity.txt`](evidence/connected-identity.txt) |
| One live source can be replaced by its own Recording | `stimulus can.node1.Tx 4 Messages carried whole; retained can.node2.Tx 4, can.bus.Node1 6, can.bus.Node2 6 — every Message, order and event time as the live Run recorded them`, on all three step grids — [`replay-aligned.txt`](evidence/replay-aligned.txt), [`replay-quantised.txt`](evidence/replay-quantised.txt), [`replay-coarse.txt`](evidence/replay-coarse.txt) |
| A replayed activation carrying several operations is arbitrated as the live one was | four frames of one CAN ID at instant 1000 ms, transmitted at 1000.48 / 1000.96 / 1001.44 / 1001.92 ms in the live Run and the replay Run alike — [`replay-coarse.txt`](evidence/replay-coarse.txt) |
| The equivalence check fails on a missing operation | `can.node1.Tx: live recorded 4 Messages and the replay Run 3`, and the bus's arbitration reverses — [`replay-aligned-dropped.txt`](evidence/replay-aligned-dropped.txt) |
| The equivalence check fails on an altered event time | the frame and the transmission it causes both 50 ms early — [`replay-aligned-retimed.txt`](evidence/replay-aligned-retimed.txt) |
| Every replay Manifest reproduces, the faulted ones included | six lines, one per Manifest, each bit-compared on its own — [`replay-identity.txt`](evidence/replay-identity.txt) |
| What every compared Run depended on is retained | Manifest hashes, Recording digests, FMU archive digests, declared configuration, and the replayed Recording's committed hash — [`replay-provenance.txt`](evidence/replay-provenance.txt) |

## What current SiL does, and where the gap is

Two Manifests, built by [`manifest.py`](manifest.py) and differing in one
thing: whether a Channel asks the importer for the node's bus data. Both
declare the released `python3 -m sil.fmi` importer as an ordinary Process
participant driving the fixture's CAN node.

| Manifest | Hash | What it asks for | What happens |
| --- | --- | --- | --- |
| `no-channels` | `180b353a…f23b` | nothing — the Run reaches the FMU itself | exit 1, `ParticipantFailure: fmi3InstantiateCoSimulation returned no instance for 'DemoCanNodeTriggeredOutput'` |
| `binary-channel` | `2bf32b33…8ef5` | one bounded CAN frame Channel, schema field names = the FMU's Binary variable names | exit 2, `ManifestError: Channel 'can.Tx' declares schema field 'CanChannel.Tx_Data_length', which is no output variable of FMU 'DemoCanNodeTriggeredOutput'` |

Both failures are the Importer's, and they are different capabilities:

- **Event Mode.** `sil.fmi` instantiates with `eventModeUsed` false and
  `earlyReturnAllowed` false, and this FMU requires the first. #139 is where
  that lifecycle is implemented, and the measured steps of this proof are where
  the implementation is held to the same FMU.
- **Binary variables and Clocks.** `sil.fmi` reads only `Float64` elements out
  of `modelDescription.xml`, so the node's `Binary` and `Clock` variables are
  not unsupported — they are invisible. The diagnostic names the Channel's
  schema field rather than the variable's type, which is the *other* thing
  #138 has to fix: an unsupported type should be reported as one.

**Neither failure is a kernel-contract limitation.** The `binary-channel`
Manifest's Channel carries a bounded CAN buffer as a `u16` length beside a
2048-byte `u8` array — a schema the kernel accepts, hashes, and costs without
complaint, as [`footprint.txt`](evidence/footprint.txt) shows. The Step
protocol, the Manifest, the Arena and the route model are not what is missing;
an Importer that can see a Binary variable and drive a Clock is. That is the
distinction #118 asked every capability proposal to make, and this gate makes
it with the release in hand rather than by inspection.

## What this checkout does

The last three steps of the proof answer the same question about the working
tree, and they are the only ones that read the checkout at all.
[`Dockerfile.measured`](Dockerfile.measured) starts from the same published
runner image, by the same digest, and puts the tree's `sil` package ahead of
the installed one on `PYTHONPATH`: the kernel, the loader and the runner are
the release's, and the Importer is the checkout's. The FMU is copied out of
the fixture image rather than built again, so it is the archive whose digest
was checked above.

Two Manifests, built by [`clocked_manifest.py`](clocked_manifest.py), one per
step grid of the expected exchange. Both declare `python3 -m sil.fmi` bound to
the node's clocked `CanChannel.Tx_Data`, publishing one bounded CAN frame
Channel that carries the activation and the FMI event time it belongs to:

| Manifest | Hash | Step | What happens |
| --- | --- | --- | --- |
| `clocked-aligned` | `57b2696a…7f83` | 100 ms | exit 0, four events, matched whole |
| `clocked-quantised` | `1984c0bd…c629` | 250 ms | exit 0, four events, matched whole |

[`clocked_exchange.py`](clocked_exchange.py) reads the Recording each Run
produced and compares it with [`expected.json`](expected.json) — the same file
the independent importer is judged against, in the same event shape. The
quantised case is where the three times come apart:

```
--- case quantised ---
  event          0 ns  published          0 ns   23 B  Configuration, Configuration
  event  500000000 ns  published  250000000 ns   20 B  CanTransmit
  event  750000000 ns  published  500000000 ns   20 B  CanTransmit
  event 1000000000 ns  published  750000000 ns   20 B  CanTransmit
quantised: 4 events, all as expected
```

The node's own log in [`clocked-quantised.txt`](evidence/clocked-quantised.txt)
still says `Transmitting CAN frame with ID 1 at internal time 0.300000`: the
frame is generated at 300 ms, becomes observable at the communication point
500 ms, is published in the Slot at 250 ms, and reaches the observer one
Latency later. Four different instants, none of them quantised into another,
and the Message states the one the FMU stood on.

That this is the Importer's doing and not a Recording artifact is what the
aligned case adds: the same node, the same Manifest but for the Step period,
and every event at the communication point that carries its own internal
transmit time.

### Two nodes through the bus FMU

Two more Manifests, built by
[`connected_manifest.py`](connected_manifest.py), one per step grid of the
connected expected exchange. Each declares **one** process participant for all
three FMUs — `--instance node1=`, `--instance node2=`, `--instance bus=`, two
`--connect` arguments pairing the terminals, one `--bus-profile`, four
`--bind` arguments naming the terminals to observe, and
`--start bus.BusErrorProbability=0.0`:

| Manifest | Hash | Step | What happens |
| --- | --- | --- | --- |
| `connected-aligned` | `b9e955aa…37ef` | 100 ms | exit 0, 20 events on 4 terminals, matched whole |
| `connected-quantised` | `f868e70c…c716` | 250 ms | exit 0, 16 events on 4 terminals, matched whole |

[`connected_exchange.py`](connected_exchange.py) reads each Recording and
compares every observed terminal with
[`connected_expected.json`](connected_expected.json), per Channel and in the
Recording's own order. The aligned case is where the bus's own time shows:

```
--- case aligned ---
  node1.CanChannel   event   300000000 ns  published  200000000 ns   20 B  CanTransmit
  node2.CanChannel   event   300000000 ns  published  200000000 ns   20 B  CanTransmit
  bus.Node1          event   300480000 ns  published  300000000 ns   12 B  Confirm
  bus.Node2          event   300480000 ns  published  300000000 ns   20 B  CanTransmit
  bus.Node1          event   300960000 ns  published  300000000 ns   20 B  CanTransmit
  bus.Node2          event   300960000 ns  published  300000000 ns   12 B  Confirm
aligned: 20 events on 4 terminals, all as expected
```

Both nodes offer a frame at 300 ms. Node 1's wins arbitration and is
transmitted 480 us later — a `Confirm` back to its sender, the frame itself to
node 2 — and node 2's follows 480 us after that, the other way round. All four
of those Messages are published in the Slot at 300 ms, because 300.48 ms and
300.96 ms are not Slots: they are the group's own communication points, and
what the Recording holds is the instant each activation stood on rather than a
timestamp rounded onto the grid.

### One node replaced by its own Recording

The last step is the regression workflow the milestone exists for. The live
Runs above recorded what both nodes published;
[`replay_manifest.py`](replay_manifest.py) declares the **same composition with
`node1` gone**, and a Replay participant handing `bus.Node1` the Messages the
removed node published on `can.node1.Tx`. Nothing else moves: the receiving
node, the bus simulation FMU, `BusErrorProbability`, the Channels, the schema,
the route capacities and the step grid are the live Manifest's.

Two things make that Channel the boundary rather than a second composition:

- **`latency_ns` 0.** An activation is published in the Slot the Step that
  observed it began in, so its instant lies inside that Step. Delivered in the
  Slot it was published in, a Message therefore arrives in the Step its own
  instant belongs to. Under the default next-activation delivery every Message
  would arrive one Step *after* the instant it names, and the Importer refuses
  that rather than raising the activation somewhere else.
- **The activation is raised at the instant the Message states.** That is what
  [ADR 0002](../../docs/adr/0002-a-replayed-terminal-lands-on-its-own-instant.md)
  settles, and it is the whole reason the receiver behaves identically: the bus
  computes its transmission time from the instant a frame arrived.

Three step grids are replayed. Two are the expected exchange's, so they are
judged twice over — against an expectation written before any Run, and against
the live Run they replay. The third is added here, at 500 ms, **coarser than
the node's own 300 ms transmit period**, and it is the only one that reaches
three things: one activation carrying several CAN operations, several
activations of the boundary Channel in one Slot, and four frames of one CAN ID
offered at a single instant. It states no expected exchange — it was not
written before a Run — so what judges it is the live Run it replays, which is
the claim this step is about.

| Manifest | Hash | Step | Duration | What happens |
| --- | --- | --- | --- | --- |
| `live-coarse` | `1febf8a4…0bf8` | 500 ms | 1500 ms | exit 0, the live composition of the coarse grid |
| `replay-aligned` | `2105764d…771f` | 100 ms | 1000 ms | exit 0, every retained stream as the live Run recorded it |
| `replay-quantised` | `71982bd0…d4c1` | 250 ms | 1000 ms | exit 0, the same |
| `replay-coarse` | `57d9df0b…8c48` | 500 ms | 1500 ms | exit 0, the same |
| `replay-aligned-dropped` | `bd0deac4…89bf` | 100 ms | 1000 ms | exit 0, and the equivalence check fails: one `CanTransmit` is missing |
| `replay-aligned-retimed` | `356aa3a2…6017` | 100 ms | 1000 ms | exit 0, and the equivalence check fails: one `CanTransmit` moved to another instant |

[`replay_equivalence.py`](replay_equivalence.py) reads the live Recording and
the replay Recording and compares four things per Channel — the count of
Messages, their order, the FMI event time each states, and the payload bytes.
Each Channel is reported whole, because a Channel is one terminal's activations
and nothing else may appear on it; this is the first Message of each, out of
[`replay-aligned.txt`](evidence/replay-aligned.txt):

```
--- case aligned ---
  boundary can.node1.Tx, replayed into the terminal it fed
  can.node1.Tx      0  event           0 ns   23 B  Configuration, Configuration
  can.node1.Tx      1  event   300000000 ns   20 B  CanTransmit
  ...
  can.node2.Tx      1  event   300000000 ns   20 B  CanTransmit
  ...
  can.bus.Node1     0  event   300480000 ns   12 B  Confirm
  can.bus.Node1     1  event   300960000 ns   20 B  CanTransmit
  ...
  can.bus.Node2     0  event   300480000 ns   20 B  CanTransmit
  can.bus.Node2     1  event   300960000 ns   12 B  Confirm
  ...
aligned: stimulus can.node1.Tx 4 Messages carried whole; retained can.node2.Tx 4, can.bus.Node1 6, can.bus.Node2 6 — every Message, order and event time as the live Run recorded them
```

The verdict states the two halves apart on purpose. The boundary says the
Replay participant carried the stimulus whole and in order; only the retained
streams say the participants that stayed behaved the same, and one count
covering both would let the first flatter the second.

The removed node is not in that Run at all. Every event time below `can.node2.Tx`
and both `can.bus.*` Channels is therefore produced by the participants that
stayed, out of a stimulus that arrived on a Channel — and it is the live Run's,
down to the 480 us the bus counts from the instant each frame reached it.

**Whole Recordings are deliberately not compared.** Two Runs of two Manifests
are two Manifest hashes: the documents differ, the participant set differs, and
the replay Run carries a Publisher the live one does not. What has to match is
the Message streams the two Runs share — the replayed boundary, which says the
stimulus was carried whole and in order, and the three streams of the
participants the replay did not replace, which is what a replaced source has to
reproduce.

What the three grids cover between them, with the grid that reaches each:

| Case | Where | Covered |
| --- | --- | --- |
| Start boundary | all three | the configuration Message states instant 0 and is published in the Slot at 0, so it is replayed at the instant its Step begins on |
| End boundary | all three | every `CanTransmit` states the instant its Step ends on — 300/600/900 ms aligned, 500/750/1000 ms quantised, 500/1000/1500 ms coarse |
| Multiple operations in one outer Step | coarse | the node accumulates into its transmit buffer between communication points, so the Message at 1000 ms is **40 bytes carrying two `CanTransmit` operations**, and the bus queues two frames out of one activation |
| Several activations in one outer Step | coarse | the Slot at 0 carries two Messages of the boundary Channel — the configuration at instant 0 and the first frame at instant 500 ms — and each is raised at its own instant |
| Same-time ordering | coarse | four frames of CAN ID 1 are offered at instant 1000 ms, two of them out of one replayed Message. The bus transmits them at 1000.48, 1000.96, 1001.44 and 1001.92 ms, and the replay Run reproduces that sequence — the two replayed frames still winning the first two slots |
| A missing operation | `replay-aligned-dropped` | an Interceptor drops the first `CanTransmit` from the boundary. The check reports `live recorded 4 Messages and the replay Run 3`, then the first Message that differs on each affected Channel — including the arbitration reversing, because node 2's frame now meets no competitor |
| An altered operation | `replay-aligned-retimed` | an Interceptor rewrites that Message's `data_event_time_ns` to 250 ms. The Run still succeeds — 250 ms is inside the Step the Message arrives in — and the check reports the event time, and the bus's transmission 480 us after it, both 50 ms early |

The coarse grid's own report, out of
[`replay-coarse.txt`](evidence/replay-coarse.txt):

```
  can.node1.Tx      2  event  1000000000 ns   40 B  CanTransmit, CanTransmit
  ...
  can.bus.Node1     2  event  1000480000 ns   12 B  Confirm
  can.bus.Node1     3  event  1000960000 ns   12 B  Confirm
  can.bus.Node1     4  event  1001440000 ns   20 B  CanTransmit
  can.bus.Node1     5  event  1001920000 ns   20 B  CanTransmit
```

Both failing variants are **declared faults rather than edited artifacts**: an
Interceptor is part of the hashed Manifest, so a Run that proves the check can
fail reproduces like the Run that passes. `run-proof.sh` fails if either of
them ever stops failing.

The Determinism check is run **per Manifest**, never across two of them:
[`evidence/connected-identity.txt`](evidence/connected-identity.txt) and
[`evidence/replay-identity.txt`](evidence/replay-identity.txt) hold one line per
Manifest, the faulted ones included — a Run that proves the check can fail has
to reproduce like the Run that passes, and this is where that is measured
rather than asserted. A replay Run's stimulus is a file its Manifest names and
hashes, so it reproduces on the same terms as a Run that computes its own.

[`evidence/replay-provenance.txt`](evidence/replay-provenance.txt) states, per
Manifest: its own hash, the Recording it produced, the FMU archives its command
names, the configuration it declares — bus profile, connection, bindings, start
values, Channel Latency, Interceptors — and, for a replay Manifest, the
Recording it replays with the hash the Manifest committed to, checked against
the file. `sil-run` writes a provenance side-car carrying the same facts, and
the released runner this proof pins predates it; the record is therefore
assembled from the Manifests and the artifacts they name. Every Recording
either side of every comparison is retained beside it — `connected-aligned`,
`connected-quantised`, `live-coarse` and the five `replay-*` ones — so the
comparison can be re-made on the artifacts rather than on a rebuild of them.

## Reproducing

```sh
proofs/fmi-ls-bus/run-proof.sh            # needs docker and network
```

The image build fetches the pinned upstream revisions and compiles them; every
step after that runs with `--network none`. The evidence directory defaults to
`evidence/` beside this README, and the Manifests land in a temporary
workspace. [`.github/workflows/proof-fmi-ls-bus.yml`](../../.github/workflows/proof-fmi-ls-bus.yml)
runs the same script on `ubuntu-latest`, for a pull request that touches this
directory or the Importer.

Every step but the last three is made of pinned artifacts, so it answers the
same on any machine class the fixture supports. Those three are made of the
working tree as well, and `evidence/identity.txt` names the commit they
measured — including when that tree carried changes the commit does not.

The committed evidence was produced by that script on `linux/amd64` — the
supported machine class — in an emulated container on a `Darwin arm64` host,
which [`evidence/identity.txt`](evidence/identity.txt) states. The workflow
reproduced it on a native amd64 runner, down to the archive digests above:
the same pinned image builds the same FMU bytes whether the host executes
x86-64 or emulates it.

The decoder has its own tests, which need no fixture and no docker:

```sh
.venv/bin/python -m pytest proofs/fmi-ls-bus/test_can_operations.py
```

## Files

| File | What it is |
| --- | --- |
| [`Dockerfile`](Dockerfile) | The fixture image (upstream sources, toolchain, independent importer) and the proof image (released SiL runner by digest, plus the fixture) |
| [`build-fixture.sh`](build-fixture.sh) | Check out one pinned revision and pack each demo with upstream's own script |
| [`pack_fixture.py`](pack_fixture.py) | Compile each packed source-code FMU, prove it loads, and write the archive back reproducibly |
| [`inspect_fixture.py`](inspect_fixture.py) | Read the descriptions, manifests and terminals; digest the archives |
| [`can_operations.py`](can_operations.py) | Decode CAN bus operations out of a Binary payload; reject everything outside the profile |
| [`test_can_operations.py`](test_can_operations.py) | The decoder's tests, written from the standard's headers |
| [`expected.json`](expected.json) | The expected exchange, stated before any Run |
| [`reference_exchange.py`](reference_exchange.py) | Drive the node through an independent FMI 3.0 importer and compare |
| [`manifest.py`](manifest.py) | The two Manifests that put the released SiL Importer in front of the same FMU |
| [`observer.py`](observer.py) | The consumer-side subscriber the second Manifest declares |
| [`Dockerfile.measured`](Dockerfile.measured) | The released runner with the checkout's `sil` package ahead of it — the one image here that is not made of pinned artifacts alone |
| [`clocked_manifest.py`](clocked_manifest.py) | The two Manifests that put the checkout's clocked Importer in front of the node, one per step grid |
| [`clocked_observer.py`](clocked_observer.py) | The consumer-side subscriber those Manifests declare |
| [`clocked_exchange.py`](clocked_exchange.py) | Judge one Run's Recording against the expected exchange |
| [`connected_expected.json`](connected_expected.json) | The expected exchange of two nodes through the bus FMU, stated before any Run |
| [`connected_manifest.py`](connected_manifest.py) | The two Manifests that drive all three FMUs as one group, one per step grid |
| [`connected_exchange.py`](connected_exchange.py) | Judge one connected Run's Recording, per observed terminal |
| [`replay_manifest.py`](replay_manifest.py) | The Manifests that replace one live node with its own Recording, faultless and faulted, plus the coarse grid's own live composition |
| [`replay_equivalence.py`](replay_equivalence.py) | Compare a replay Run's Message streams with the live Run's |
| [`replay_provenance.py`](replay_provenance.py) | State what produced each compared Run, by digest |
| [`run-proof.sh`](run-proof.sh) | All of it, in order, into `evidence/` |
