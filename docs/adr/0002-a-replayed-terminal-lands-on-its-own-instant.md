# 2. A replayed terminal lands on the instant its Messages state

Date: 2026-09-21

Status: accepted

Relates to [issue #141](https://github.com/Stevie1704/sil/issues/141).
Settles the question [ADR 0001](0001-connected-fmus-in-one-process-participant.md)
left open.

## Context

[Issue #140](https://github.com/Stevie1704/sil/issues/140) established the
replay-input boundary: a network terminal that no `--connect` names may be fed
by an in-direction Channel instead, so a Recording of the Channel that observed
a terminal can stand in for the FMU that produced it. It also established what
such a Message carries — the payload, and the FMI event time of the activation
it belongs to.

What it did **not** do is use that event time. The group raised the receiving
Clock at whatever communication point it happened to stand on, and ADR 0001
recorded why: with the default Latency a Message becomes visible one
activation after the Slot it was published in, so by arrival the instant it
names is behind the group, and no FMU of this profile offers the rollback that
would take it back.

Issue #141 asks for the thing that makes a replaced source worth replaying:
remove one live node from the connected composition, replay its Channel into
the terminal it fed, and get the **same behavior out of the receiver**. An
activation raised at the wrong instant does not give that. The bus simulation
FMU computes a transmission time from the instant a frame arrived, so a frame
delivered a Step late is transmitted a Step late, confirmed a Step late, and
arbitrated against a peer it no longer meets.

## Decision

**The group raises the input Clock at the instant the Message states, and the
boundary Channel declares `latency_ns` 0.**

The two halves are one decision. The Importer stops at a replayed instant the
way it stops at any instant an instance asked for — it is one more entry in the
set of sub-interval boundaries the Step is covered in. The Channel is what
makes that instant reachable.

## Why a zero Latency is the whole answer

An activation the group observes is published in the Slot the observing
activation ran in, which is the Slot the Step that observed it **began** in.
So for every Message on an observation Channel:

    publication Slot  ≤  stated instant  ≤  publication Slot + Step period

A Channel delivering in the Slot it was published in therefore hands the
Message to a Step whose interval contains the instant it names. Nothing is
rounded, nothing is held over, and the instant is always ahead of where the
group stands when the Step begins.

Under the default next-activation delivery the same Message arrives one Step
later, which puts its instant at or behind the start of the Step it arrives
in — the case ADR 0001 described. That is now refused with a diagnostic naming
both bounds rather than quietly approximated.

`latency_ns` 0 already exists and already means this. The kernel publishes a
Replay participant's Messages into the router before any activation of the same
Slot, so same-Slot delivery is ordered rather than a race. **No kernel change
was needed, and none was made.**

## What the Importer refuses

- An instant **behind** the Step the Message arrived in. No FMU of this profile
  can be taken back to it.
- An instant **beyond** the Step's own end. Reaching it would mean stepping
  past the end of the Step the kernel asked for.

Both are reported with the Channel, the terminal, the stated instant and the
Step's bounds, because the repair is a Manifest change — usually the Latency —
rather than something the Run can settle.

## Consequences

- A replayed source is equivalent to the live one it stands in for: the
  receiving FMU is handed the same operation at the same instant, so what it
  computes next is the same. `proofs/fmi-ls-bus/` measures exactly that, per
  Message, per Channel, across two Runs of two Manifests.
- Several Messages in one Step are each raised at their own instant, and
  several at one instant are raised in the order they arrived, which is the
  order the Recording holds them in. Ordering at the boundary is the
  Recording's, not the group's.
- An arrived Message goes ahead of the events the same instant caused by
  itself. The group was holding it before it took the Step, and the peer it
  stands in for would have offered it from the same place in the group's
  declaration order.
- Two Runs of two Manifests are two Manifest hashes, so their Recordings differ
  in bytes by construction. Equivalence is therefore stated over the Message
  streams the two Runs share, and the Determinism check stays what it is: one
  Manifest, run twice, bit-compared.
- The Latency is a Manifest decision, which means a Run that gets it wrong
  fails rather than produces a plausible answer.

## Alternatives rejected

**Keep raising the activation where the group stands, and accept the shift.**
This is what ADR 0001 left in place. It cannot be measured against a live Run:
the receiver's transmissions move by a Step, the bus's arbitration between two
frames offered at one instant changes, and the proof would have to compare
against a second expectation written for the replay. An equivalence check that
expects different behavior is not one.

**Hold a Message until the group reaches the instant it names, across Steps.**
This would let the default Latency work for instants ahead of the group, but it
buys nothing: the instants that arrive late are late by exactly one Step, which
means they are behind the group rather than ahead of it. It would also put a
queue with its own ordering rules inside the Importer, for a case the Manifest
already answers with one declared number.

**A new Channel Latency computed from the Message.** A Latency that read the
payload would make delivery time a function of content, which is the property
that makes a Run independent of execution order inside a Slot. Issue #118 asks
every capability proposal to show that the kernel is what is missing; here it
plainly is not, because a Latency of zero and the event time the Message
already carries are enough.
