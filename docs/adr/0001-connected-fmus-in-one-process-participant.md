# 1. Connected FMUs are coordinated inside one Process participant

Date: 2026-09-21

Status: accepted

Relates to [issue #140](https://github.com/Stevie1704/sil/issues/140).

## Context

[Issue #139](https://github.com/Stevie1704/sil/issues/139) brought one
FMI-LS-BUS CAN node into a Run: an FMU whose whole output is a Binary buffer
gated by a triggered Clock, driven by the Importer as an ordinary Process
participant. Issue #140 asks for two such nodes exchanging frames **through the
dedicated CAN bus simulation FMU**, and asks the boundary question first: are
the connected FMUs three Process participants wired to each other through
Channels, or one Process participant that coordinates them?

The kernel already routes Messages between participants and never learns that
FMI exists. Three participants would keep it that way with no new code at all,
so the burden is on the group: it has to be shown that Channels cannot carry
what the connection carries.

## Decision

**Coordinate the connected FMUs inside one Process participant.** One
invocation of `python -m sil.fmi` declares every FMU of the group, the
connections between their network terminals, and the Channels that observe or
feed them:

```
python -m sil.fmi --instance node1 CanNode.fmu --instance node2 CanNode.fmu \
                  --instance bus CanBusSimulation.fmu \
                  --bus-profile application/org.fmi-standard.fmi-ls-bus.can \
                  --connect node1.CanChannel=bus.Node1 \
                  --connect node2.CanChannel=bus.Node2 \
                  --bind can.node1.Tx:data=node1.CanChannel.Tx_Data ...
```

FMI-specific event processing stays where issue #139 put it — at the Importer
edge. What the kernel sees is one Process participant publishing bounded
Messages on Channels it declares, and the Manifest hashes every argument above.

## Why Channels cannot carry the connection

Three separate reasons, each from the pinned artifacts rather than from
inspection:

**1. The delivery time is the bus model's output, not a declaration.** The bus
FMU states when it has finished transmitting a frame as a *countdown Clock
interval*: `(44 + dataLength) / baudRate` seconds, which is 480 us for the
fixture's four-byte frame at 100 000 bit/s. A Channel's Latency is declared in
the Manifest and defaults to the subscriber's next activation. No Latency could
be declared for an interval the bus computes per frame, and the default would
deliver every frame at the next Slot instead — one instant, with nothing left
to tell two transmissions 480 us apart from each other.

**2. Two frames offered at the same instant leave the bus at different
instants.** Both nodes offer CAN ID 1 at 300 ms. The bus transmits one at
300.48 ms, answering its originator with a `Confirm` operation and handing the
frame to the other node, and transmits the second at 300.96 ms, the other way
round. The two deliveries are distinguishable, ordered, and 480 us apart.
Under next-activation delivery they would be one Slot's worth of Messages with
no such structure — the arbitration the bus FMU models would be invisible.

**3. An instant is iterated, not scheduled.** A frame handed to the bus is an
activation of the bus's input Clock, and what the bus does with it happens in
that same instant: it reads the buffer, queues the frame, and states the next
transmission time, all inside one event. A Channel puts a Slot between the two
FMUs by construction, so the iteration would be spread over Slots, and the
event times recorded would be the kernel's grid rather than the FMUs'.

## What the group does that a participant cannot

The group owns **communication points between the kernel's Slots**. It advances
every instance over sub-intervals that end at each instant any instance asked
for — a declared next event time, or a countdown Clock's interval — and at the
kernel Step's own end. Nothing is published with an internal instant as its
timestamp: a Message is published in the Slot the importer's activation runs
in, and states the FMI event time it belongs to, which is the distinction
issue #139 established and this inherits.

## Rollback is not needed, and is not available

Both fixture FMUs declare `canGetAndSetFMUState` false, so no importer can
retract a step either of them took. The group does not have to: **every
instance stands on the same internal communication point at all times.** An
event reported at the end of a sub-interval is reported at an instant no peer
has passed, so there is nothing to take a peer back to. The group refuses,
rather than steps past, an FMU that asks to be activated at an instant already
behind it.

## Consequences

- One Process participant is the Publisher of every Channel the group
  observes, which preserves single-Publisher ownership per Channel without a
  new rule.
- The supported topology is a star: each connection has exactly one
  bus-simulation FMU end (`isBusSimulationFMU=true`) and one node end, both
  transceiver terminals of the same layered standard version, and both ends of
  one direction declaring the identical `mimeType`. All of it is checked before
  any FMU is instantiated.
- A terminal that is connected to no peer may be fed by an in-direction
  Channel instead, which is the replay-input boundary: a recorded observation
  Channel can stand in for the FMU that produced it. The Message carries the
  instant it belongs to, and the group does **not** replay it at that instant:
  a Message is published in the Slot the observing activation ran in and
  becomes visible one Latency later, so by arrival the instant it names is
  already behind the group, and no FMU of this profile can be taken back to
  it. Making a replayed terminal land on its own instants is as much a
  question about a Channel's delivery time as about the Importer, and issue
  #141 is where it is answered. What this issue owes it is the information,
  and the Message carries it. **Answered by
  [ADR 0002](0002-a-replayed-terminal-lands-on-its-own-instant.md): the group
  raises the activation at the instant the Message states, and a boundary
  Channel declaring `latency_ns` 0 is what puts that instant ahead of the
  group rather than behind it.**
- A terminal takes its frames from a connected peer **or** from a Channel,
  never from both — one source per input.
- One process now holds several FMU instances, so a failure has to close all of
  them. It does, on the initialization path and on the Step path alike.
- Each FMU's path is its own command argument, so the kernel resolves it
  against the Manifest's directory and digests it into the Run's provenance —
  the rule that already covers a single FMU's path, and one a `name=path`
  spelling would have silently lost.
- The kernel contract is unchanged. Nothing here asked for a new Channel
  capability, a new Latency kind, or a scheduler change.

## Alternatives rejected

**Three Process participants and declared Latency.** Rejected on the evidence
above: the delay is computed per frame by the bus model, and the default
next-activation delivery erases both the instant and the ordering.

**One participant per FMU with a new same-Slot Channel kind.** This would put
FMI's event iteration into the kernel's routing model — the kernel would have
to iterate a Slot until a set of participants stopped producing. That is the
co-simulation master's job, and issue #118 asks every capability proposal to
show the kernel is what is missing. It is not: the group is built entirely on
the Step protocol, the Manifest, and the Channel contract that already exist.
