"""Connected FMUs, coordinated at communication points of their own.

The scheduling policy here is the group's, not the kernel's: the interval one
Step declares is covered in sub-intervals ending at every instant an instance
asked for and at every instant an arrived Message states. That finer grid has
to exist — a bus simulation FMU asks to be activated at the end of a frame's
transmission time, which is an instant the Manifest's Step period has no reason
to contain. The kernel still owns the Slots the Messages are published in.

One FMU on its own is the other policy, in `single.py`, where the Slot grid is
the only grid there is.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Sequence

from sil.participant import (
    ManifestError,
    ParticipantFailure,
    StepParticipant,
)

from sil.fmi.archive import Extraction
from sil.fmi.composition import (
    Transceivers,
    bind_group_channels,
    connect,
    group_start_values,
    instance_paths,
)
from sil.fmi.description import ModelDescription
from sil.fmi.mapping import channel_fields
from sil.fmi.stepping import require_contiguous
from sil.fmi.terminals import Instance, Transceiver

# How many times one instant may hand an activation from one connected FMU to
# another before this importer stops propagating. Two FMUs that answer each
# other at the same instant would otherwise never let the instant end; the
# bound is the same kind of declaration as the one on the iteration of a
# single event, at the group's own level rather than one FMU's.
_MAX_PROPAGATIONS = 100


def _require_reachable(channel: str, transceiver: Transceiver, instant: int,
                       start: int, end: int) -> None:
    """Refuse an arrived activation the Step it arrived in cannot reach.

    An instant behind the group is one no FMU of this profile can be taken
    back to; one beyond the Step's end would have to be reached by stepping
    past the end the kernel asked for. Which of the two a Run hits is a
    statement about the Channel's Latency: an activation observed during a
    Step is published in the Slot that Step began in, so only a Channel
    delivering in the Slot it was published in — `latency_ns` 0 — puts the
    Message back in the Step its instant belongs to. Both bounds are in the
    diagnostic, because the repair is a Manifest change rather than something
    the Run can settle.
    """
    if start <= instant <= end:
        return
    raise ParticipantFailure(
        f"Channel {channel!r} states {instant} ns as the instant of an "
        f"activation of {transceiver}, and the Step it arrived in stands at "
        f"{start} ns and ends at {end} ns; an activation is raised at the "
        f"instant it states, so a replayed Channel declares the Latency that "
        f"delivers a Message in the Step its instant belongs to"
    )


@dataclass(frozen=True)
class _Pending:
    """One event a group owes an instance at the instant it is settling.

    An activation handed over by a connected peer, or by a Channel, comes with
    it: the payload is written into the receiving FMU immediately before its
    event, because that write is what the event is about.
    """

    instance: Instance
    transceiver: Transceiver | None = None
    payload: bytes | None = None


class _Group:
    """Connected FMUs, coordinated at their own communication points.

    Every instance stands on the same internal communication point at all
    times. That is what makes rollback unnecessary — and no FMU of this
    profile offers it, because they declare `canGetAndSetFMUState` false: an
    event reported at the end of an interval is reported at an instant no peer
    has passed, so no peer has to be taken back to it.

    The group's own grid is finer than the kernel's Slot grid, and it has to
    be: a bus simulation FMU asks to be activated at the end of a frame's
    transmission time, which is an instant the Manifest's Step period has no
    reason to contain. The kernel still owns the Slots the Messages are
    published in; what the group owns is where the FMUs meet between them.

    Propagation at one instant is bounded, like the iteration of one event:
    two FMUs answering each other at the same instant would otherwise never
    let the instant end.
    """

    def __init__(self, instances: list[Instance],
                 transceivers: list[Transceiver]):
        self._instances = instances
        self._injections = {
            transceiver.injection.channel: transceiver
            for transceiver in transceivers
            if transceiver.injection is not None
        }

    def initialize(self) -> list:
        """The event initialization ended in, across the whole group.

        A bus node's own configuration is already waiting in it, and the bus
        simulation FMU is what that configuration is for, so the first instant
        is propagated exactly like every later one.
        """
        return self._settle(0, [_Pending(i) for i in self._instances])

    def advance(self, t: int, dt: int, inputs: list) -> list:
        """Advance the group over one kernel Step, event by event.

        The interval is covered in sub-intervals ending at every instant any
        instance asked for, at every instant an arrived Message states, and at
        the Step's own end. Nothing is published with an internal instant as
        its timestamp: a Message is published in the Slot this activation runs
        in and states the FMI event time it belongs to, which is how the
        group's finer grid reaches a Recording.
        """
        now, end = t, t + dt
        injected = self._injected(inputs, now, end)
        published = self._settle(now, injected.pop(now, []))
        while now < end:
            boundary = self._boundary(now, end, injected)
            for instance in self._instances:
                instance.step(now, boundary - now)
            now = boundary
            # An arrived Message goes ahead of the events this instant caused
            # by itself: the group was holding it before it took the Step, and
            # the peer it stands in for would have offered it from the same
            # place in the group's declaration order.
            pending = injected.pop(now, [])
            pending.extend(
                _Pending(instance) for instance in self._instances
                if instance.due(now)
            )
            published.extend(self._settle(now, pending))
        return published

    def close(self) -> None:
        """Terminate and free every instance, whatever any one of them does.

        An FMU that fails to terminate must not leave its peers instantiated:
        the first failure is the one reported, and every instance is closed
        either way.
        """
        failure = None
        for instance in self._instances:
            if instance.fmu is None:
                continue
            try:
                instance.fmu.close()
            except ParticipantFailure as error:
                failure = failure or ParticipantFailure(
                    f"FMU of instance {instance.name!r}: {error}"
                )
        if failure is not None:
            raise failure

    def _injected(self, inputs: list, start: int, end: int) -> dict[int, list]:
        """The Messages that arrived, by the instant each one states.

        A Message states the communication point its activation was observed
        at, and that is where the group raises the Clock: the receiving FMU is
        handed the operation at the instant the peer it stands in for produced
        it. The group stops there like it stops at any instant an instance
        asked for.

        Messages keep the order they arrived in, which is the order they were
        published in, so two activations of one instant reach the terminal the
        way the Recording holds them.
        """
        pending: dict[int, list] = {}
        for message in inputs:
            transceiver = self._injections[message.channel]
            injection = transceiver.injection
            instant = injection.instant_ns(message.data)
            _require_reachable(message.channel, transceiver, instant,
                              start, end)
            pending.setdefault(instant, []).append(_Pending(
                transceiver.instance, transceiver,
                injection.payload(message.data),
            ))
        return pending

    def _boundary(self, now: int, end: int, injected: dict[int, list]) -> int:
        """The next instant the whole group stops at, at the latest `end`.

        `injected` holds what this Step still owes, keyed by instant; the
        caller removes each instant as it settles it, so everything left is
        ahead of `now`.
        """
        asked = [
            instant for instance in self._instances
            for instant in instance.asked_for(now) if instant <= end
        ]
        asked.extend(instant for instant in injected if instant > now)
        return min(asked) if asked else end

    def _settle(self, instant_ns: int, pending: list) -> list:
        """One instant, propagated between the FMUs until it stops producing.

        Non-bus activations are handled one at a time. A bus simulation gets
        all operations offered at this instant in one event, so physical
        arbitration is independent of the order its inputs were delivered.
        """
        if not pending:
            return []
        published: list = []
        work = deque(pending)
        for _ in range(_MAX_PROPAGATIONS):
            if not work:
                self._quiesce(instant_ns)
                return published
            instance = self._deliver_same_instant(work)
            for source, payload in instance.handle(instant_ns):
                if source.observation is not None:
                    published.append(
                        source.observation.message(payload, instant_ns)
                    )
                if source.peer is not None:
                    work.append(
                        _Pending(source.peer.instance, source.peer, payload)
                    )
        raise ParticipantFailure(
            f"the connected FMUs handed each other {_MAX_PROPAGATIONS} "
            f"activations at {instant_ns} ns without the instant ending; this "
            f"importer bounds the propagation of one instant"
        )

    def _deliver_same_instant(self, work: deque[_Pending]) -> Instance:
        """Give a bus all offered operations before its event is handled."""
        index = next((n for n, item in enumerate(work)
                      if not item.instance.bus_simulation), 0)
        owed = work[index]
        del work[index]
        if not owed.instance.bus_simulation:
            if owed.transceiver is not None:
                owed.instance.deliver(owed.transceiver, owed.payload)
            return owed.instance

        # Drain all other work first; several activations of one bus terminal
        # become consecutive operations in one Clock-gated Binary buffer.
        combined: dict[Transceiver, bytearray] = {}
        remaining = deque()
        for item in (owed, *work):
            if item.instance is owed.instance:
                if item.transceiver is not None:
                    combined.setdefault(item.transceiver, bytearray()).extend(item.payload)
            else:
                remaining.append(item)
        work.clear()
        work.extend(remaining)
        for transceiver, payload in combined.items():
            owed.instance.deliver(transceiver, bytes(payload))
        return owed.instance

    def _quiesce(self, instant_ns: int) -> None:
        """End the instant: check what was asked for, return to Step Mode."""
        for instance in self._instances:
            instance.require_nothing_passed(instant_ns)
        for instance in self._instances:
            instance.leave_event()


class FmuGroupParticipant(StepParticipant):
    """A process participant whose behavior is a group of connected FMUs'.

    The group is one participant because the coordination it does cannot be
    expressed between participants. A Channel's Latency is by default the
    subscriber's next activation, which is what makes a Run independent of
    execution order inside a Slot; a frame crossing a bus reaches its
    destination at an instant the bus computes, in the same instant its
    neighbours are standing on. Two separately stepped participants would
    deliver it a Slot later and lose the instant it happened at, and no
    Manifest Latency could restore it, because the delay is the bus model's
    output rather than a declaration.

    So FMI-specific event processing stays where it already was — at the
    Importer edge — and what the kernel sees is one process participant
    publishing bounded Messages on Channels it declares. The kernel learns
    nothing about Clocks, events, or transmission times.
    """

    def __init__(self, instances: Sequence[str], *, connects: Sequence[str] = (),
                 binds: Sequence[str] = (), starts: Sequence[str] = (),
                 profile: str = ""):
        self.name = ""  # the init line's, for the one diagnostic the kernel misses
        self._declared = list(instances)
        self._connects = list(connects)
        self._binds = list(binds)
        self._starts = list(starts)
        self._profile = profile
        self._extraction = None
        self._extracted: dict[str, Path] = {}
        self._group: _Group | None = None
        # What the event that ended initialization produced, held until the
        # first activation: the kernel has no Slot before it.
        self._pending_activations: list = []
        # Where the FMUs stand, in kernel nanoseconds. They stand together.
        self._communication_point = 0

    def on_init(self, init: dict) -> None:
        self.name = init["name"]
        instances = self._read(init)
        # Everything the Manifest got wrong is rejected before any FMU is
        # instantiated: a group that cannot hold is a fact about the Run's
        # configuration, and no FMU has to be loaded to see it.
        transceivers = Transceivers(instances, self._profile)
        connect(self._connects, transceivers)
        bind_group_channels(
            self._binds, init, channel_fields(init), transceivers
        )
        transceivers.require_every_instance_used()
        starts = group_start_values(self._starts, instances)
        # The group is held before any FMU is loaded, so an instance that
        # fails to instantiate or to initialize is still one this participant
        # closes: the instances already loaded are reachable from `close`.
        self._group = _Group(list(instances.values()), transceivers.all())
        for name, instance in instances.items():
            instance.instantiate(self._extracted[name], starts[name])
        self._pending_activations = self._group.initialize()

    def _read(self, init: dict) -> dict[str, Instance]:
        """Extract every declared FMU and read what driving it depends on.

        Each archive is extracted into its own directory beneath this
        participant's own, because two instances of one FMU are two extractions
        of it: they are told apart by where they were extracted, and nothing
        one of them writes belongs to the other.
        """
        paths = instance_paths(self._declared)
        if not paths:
            raise ManifestError(
                "a group declares at least one --instance <name>=<path>"
            )
        if not self._profile:
            raise ManifestError(
                "a group declares the BUS profile its terminals carry: "
                "--bus-profile <media-type>"
            )
        self._extraction = Extraction()
        self._extracted = {}
        instances: dict[str, Instance] = {}
        for name, path in paths.items():
            extracted = self._extraction.unpack(path, name)
            self._extracted[name] = extracted
            instances[name] = Instance(name, ModelDescription.read(extracted))
        return instances

    def on_step(self, t: int, dt: int, inputs: list):
        """One Step of the group, and every Message the interval produced."""
        require_contiguous(self._communication_point, t)
        published, self._pending_activations = self._pending_activations, []
        published.extend(self._group.advance(t, dt, inputs))
        self._communication_point = t + dt
        return published

    def close(self) -> None:
        """Terminate and free every instance, and drop the extracted FMUs."""
        group, extraction = self._group, self._extraction
        self._group = None
        self._extraction = None
        try:
            if group is not None:
                group.close()
        finally:
            if extraction is not None:
                extraction.cleanup()
