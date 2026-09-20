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

**This is an interoperability fixture, not a conformance claim and not ADAS
validation.** It covers two CAN demo FMUs of one layered standard, at one
revision, on one machine class.

## What this gate establishes

| Question | Answer |
| --- | --- |
| Are the upstream CAN FMUs reproducibly available on the supported machine class? | Yes — built from pinned sources, with two documented compile-time definitions and no edit to any upstream file |
| Does upstream ship a built artifact to pin instead? | No. No release, no tag, no binary: the demos are C sources plus a packaging script that produces a source-code FMU |
| Do the artifacts implement the stable BUS 1.0.0 CAN subset? | The CAN operation bytes are v1.0.0's, verified against the header history; the artifacts' own metadata says `1.0.0-beta.1`, and no upstream revision says otherwise |
| Is there a runnable reference exchange? | Yes — the node FMU driven by an independent FMI 3.0 importer, matching payloads and event times stated from the sources beforehand |
| What does current SiL do with the same FMU? | It fails, twice and for two different reasons — both recorded, both in the Importer, neither in the kernel contract |

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

Not driven by this gate; #140 connects two nodes through it. Its description
is published here so that issue starts from a stated contract rather than from
a reading of the sources:

- `isBusSimulationFMU` is `true` in its layered-standard manifest, which is
  also at `1.0.0-beta.1`;
- two network terminals, `Node1` and `Node2`, each grouping a `Binary`
  `Rx_Data` input and `Tx_Data` output of `maxSize` 2048 with a `Rx_Clock` and
  a `Tx_Clock`;
- **the bus FMU's `Tx_Clock`s are `input` clocks with `intervalVariability`
  `countdown`**, not the `triggered` output Clocks the node carries. Driving
  it means reading a countdown interval and activating the Clock, which is a
  larger Clock profile than #139 needs;
- one `Float64` `BusErrorProbability` input, discrete, start `0.0` — the only
  scalar either FMU exposes as more than the independent variable;
- it declares `providesEvaluateDiscreteStates` `true`, which the node does
  not.

Full declaration in [`evidence/profile.txt`](evidence/profile.txt).

### What is outside the profile

CAN FD and CAN XL transmit operations, `Confirm`, `ArbitrationLost`,
`BusError`, `Status` and `Wakeup` operations, FlexRay, rollback,
early return, intermediate update, and any FMU state serialisation. The
decoder in [`can_operations.py`](can_operations.py) names each of those by its
standard name and rejects it rather than skipping it, so a payload that leaves
the profile fails loudly.

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
the same quantity**, and a later Channel publication time is a third. #139 and
#140 inherit that distinction from here.

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
  that lifecycle is implemented.
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

## Reproducing

```sh
proofs/fmi-ls-bus/run-proof.sh            # needs docker and network
```

The image build fetches the pinned upstream revisions and compiles them; every
step after that runs with `--network none`. The evidence directory defaults to
`evidence/` beside this README, and the Manifests land in a temporary
workspace. [`.github/workflows/proof-fmi-ls-bus.yml`](../../.github/workflows/proof-fmi-ls-bus.yml)
runs the same script on `ubuntu-latest`.

The committed evidence was produced by that script on `linux/amd64` — the
supported machine class — in an emulated container on a `Darwin arm64` host,
which [`evidence/identity.txt`](evidence/identity.txt) states. The workflow
reproduces it on a native amd64 runner.

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
| [`run-proof.sh`](run-proof.sh) | All of it, in order, into `evidence/` |
