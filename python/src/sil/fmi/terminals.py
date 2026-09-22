"""The members of a connected group, and the terminals that join them.

An FMU of a group and the network terminals it drives are one subject: a
terminal is the Clocks of one instance, and an instance's Clocks are what its
terminals are. This module holds both, plus the two Channel ends a terminal may
carry — an Observation of what it sends, and an Injection of what stands in for
a peer that no longer sends it.

What is *not* here is the group's schedule. When to step, which instant to
settle next and how far an activation propagates belongs to `group.py`; an
instance only knows where it stands in its own lifecycle, because a group
enters an event on one instance and not on another and an FMU asked to enter
the mode it is already in refuses.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sil.participant import ParticipantFailure

from sil.fmi.archive import Extraction
from sil.fmi.binding import (
    BinaryField,
    activation_message,
    outgoing_payload,
)
from sil.fmi.description import RX_DATA, ModelDescription, Terminal, Variable
from sil.fmi.runtime import (
    NS_PER_S,
    ClockedBuffer,
    CountdownBuffer,
    CoSimulation,
)
from sil.fmi.stepping import declared_ns, run_event


@dataclass(frozen=True)
class Observation:
    """An out-direction Channel carrying one clocked variable's activations.

    It is observation and nothing else: the group reads the variable once,
    because the Clock that gates it clears on the read, and the Message is a
    statement of what crossed the terminal rather than a second read of it.
    """

    channel: str
    binary: BinaryField

    def message(self, payload: bytes, event_time_ns: int) -> tuple[str, dict]:
        return self.channel, activation_message(
            self.binary, payload, event_time_ns
        )


@dataclass(frozen=True)
class Injection:
    """An in-direction Channel whose Messages are activations of one Clock.

    This is the replay-input half of the boundary: a Message on it is what a
    connected peer would otherwise have handed the same terminal, so a
    Recording of the observation Channel can stand in for the FMU that
    produced it.

    The activation is raised at the instant the Message states, which is the
    instant the peer it stands in for produced it at. That is what makes the
    replayed source equivalent to the live one: the receiving FMU is handed
    the same operation at the same instant, so what it does next is the same.
    """

    channel: str
    binary: BinaryField

    def payload(self, fields: dict) -> bytes:
        return outgoing_payload(self.binary, fields)

    def instant_ns(self, fields: dict) -> int:
        return fields[self.binary.event_time_field]


class Transceiver:
    """One network terminal of one instance, as the group drives it.

    The direction is the terminal owner's: `Tx_Data` is what this FMU sends
    and `Rx_Data` is what it is handed. A connection pairs one transceiver's
    send side with its peer's receive side, in both directions, and a Channel
    may watch the one or feed the other.
    """

    def __init__(self, instance: Instance, terminal: Terminal,
                 receive: ClockedBuffer, capacity: int):
        self.instance = instance
        self.terminal = terminal
        self.receive = receive
        self.capacity = capacity
        self.peer: Transceiver | None = None
        self.observation: Observation | None = None
        self.injection: Injection | None = None

    def __str__(self) -> str:
        """How every diagnostic names one end of a connection."""
        return f"{self.instance.name}.{self.terminal.name}"

    def accept(self, payload: bytes) -> None:
        """Refuse a payload above what the receiving variable declared.

        `maxSize` is the FMU's own statement of the most it will take, so a
        longer buffer is refused here rather than handed over and rejected
        with a status that names no length.
        """
        if len(payload) > self.capacity:
            raise ParticipantFailure(
                f"{self} was handed {len(payload)} bytes, above the "
                f"{self.capacity} its {RX_DATA!r} variable "
                f"{self.receive.variable.name!r} declares as maxSize"
            )


class Instance:
    """One FMU of a group: the instance, and the Clocks the group drives.

    It owns where the FMU stands in its own lifecycle — Step Mode or Event
    Mode — because a group enters an event on one instance and not on another,
    and an FMU asked to enter the mode it is already in refuses.
    """

    def __init__(self, name: str, description: ModelDescription):
        self.name = name
        self.description = description
        self.fmu: CoSimulation | None = None
        # The send side of every terminal, split by who decides when it
        # activates: the FMU raises a triggered Clock, and the group raises a
        # countdown one at the instant the FMU asked for.
        self.triggered: list[tuple[Transceiver, ClockedBuffer]] = []
        self.countdown: list[tuple[Transceiver, CountdownBuffer]] = []
        self._in_event = False
        self._event_pending = False
        self._next_event_time: float | None = None

    def instantiate(self, extracted: Path,
                    starts: list[tuple[Variable, object]]) -> None:
        """Load and initialize this FMU, in the group's declaration order.

        Event Mode is always in use: every instance of a group drives a
        network terminal, and a terminal is Clocks. Initialization therefore
        ends in Event Mode, and the caller handles that first event.
        """
        self.fmu = CoSimulation(
            self.description.binary(extracted), self.description,
            event_mode=True,
            resource_path=Extraction.resource_path(extracted),
        )
        self.fmu.apply_start_values(starts)
        self.fmu.initialize()
        self._in_event = True

    def enter_event(self) -> None:
        if not self._in_event:
            self.fmu.enter_event_mode()
            self._in_event = True

    def leave_event(self) -> None:
        if self._in_event:
            self.fmu.enter_step_mode()
            self._in_event = False

    def step(self, t: int, dt: int) -> None:
        """Advance over one of the group's own intervals."""
        self._event_pending = self.fmu.do_step(t / NS_PER_S, dt / NS_PER_S)

    def due(self, instant_ns: int) -> bool:
        """Whether this FMU asked for an event at the instant just reached."""
        return (
            self._event_pending
            or self._declared_ns() == instant_ns
            or any(buffer.due_ns == instant_ns for _, buffer in self.countdown)
        )

    def asked_for(self, after_ns: int) -> list[int]:
        """Every instant this FMU asked to be stopped at, after `after_ns`.

        Both kinds of request are the same thing to a group: an instant the
        FMU means to be in Event Mode at. The group stops at the earliest of
        them across every instance, so no FMU is stepped past its own event
        and no peer of it is past that instant either.
        """
        instants = [
            buffer.due_ns for _, buffer in self.countdown
            if buffer.due_ns is not None
        ]
        declared = self._declared_ns()
        if declared is not None:
            instants.append(declared)
        return [instant for instant in instants if instant > after_ns]

    def require_nothing_passed(self, instant_ns: int) -> None:
        """Refuse an FMU that asks to be activated at an instant already gone.

        The group can stop anywhere between two Slots, so an instant it cannot
        reach is one behind it. Stepping on regardless would take the FMU past
        an event it asked for, and no FMU of this profile offers the rollback
        that would take it back.
        """
        for transceiver, buffer in self.countdown:
            if buffer.due_ns is not None and buffer.due_ns <= instant_ns:
                raise ParticipantFailure(
                    f"{transceiver} asks for Clock {buffer.clock.name!r} to be "
                    f"activated at {buffer.due_ns} ns, and the group stands at "
                    f"{instant_ns} ns; a countdown interval is counted from the "
                    f"event it was stated in"
                )
        declared = self._declared_ns()
        if declared is not None and declared <= instant_ns:
            raise ParticipantFailure(
                f"FMU {self.name!r} declared its next event at "
                f"{self._next_event_time} s ({declared} ns), and the group "
                f"stands at {instant_ns} ns; an event is asked for ahead of the "
                f"instant it is asked in"
            )

    def handle(self, event_time_ns: int) -> list[tuple[Transceiver, bytes]]:
        """One event of this FMU: every activation it hands the group.

        The countdown Clocks due at this instant go up first, together in one
        call, and the buffers they gate are read before any discrete-state
        update — an FMU clears them in the update that ends the activation. The
        triggered Clocks are then read around every update, as they are for one
        FMU on its own.
        """
        self.enter_event()
        self._event_pending = False
        due = [
            (transceiver, buffer) for transceiver, buffer in self.countdown
            if buffer.due_ns == event_time_ns
        ]
        if due:
            self.fmu.raise_clocks(
                [buffer.clock.reference for _, buffer in due]
            )
        produced = [
            (transceiver, buffer.take(self.fmu)) for transceiver, buffer in due
        ]
        collected, states = run_event(
            self.fmu, event_time_ns, self._activations
        )
        produced.extend(collected)
        self._next_event_time = states.next_event_time
        for _, buffer in self.countdown:
            buffer.refresh(self.fmu, event_time_ns)
        return produced

    def deliver(self, transceiver: Transceiver, payload: bytes) -> None:
        """Raise one input Clock and hand it the payload, before its event."""
        transceiver.accept(payload)
        self.enter_event()
        transceiver.receive.deliver(self.fmu, payload)

    def _activations(self) -> list[tuple[Transceiver, bytes]]:
        """Every triggered output Clock that reads active, and its buffer."""
        produced = []
        for transceiver, buffer in self.triggered:
            payload = buffer.read(self.fmu)
            if payload is not None:
                produced.append((transceiver, payload))
        return produced

    def _declared_ns(self) -> int | None:
        """The declared next event time, in the kernel's own nanoseconds."""
        return declared_ns(self._next_event_time)
