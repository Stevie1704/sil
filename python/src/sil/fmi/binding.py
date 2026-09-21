"""A Channel's schema fields on one side, an FMU's variables on the other.

This is the per-step half of the mapping: what `mapping.py` resolved and
checked at initialization is held here as objects that write a Message's fields
into the FMU and read the FMU's values back out as fields. A Channel's bound
payload representation — the `u8` array, the length beside it, and the FMI
event time a clocked Message states — is stated here once, because both a
single FMU and a group publish the same Message.

Nothing here allocates a buffer or holds a pointer; `runtime.py` owns those and
takes Python values across.
"""

from __future__ import annotations

from dataclasses import dataclass

from sil.participant import ParticipantFailure

from sil.fmi.description import SCALARS, Variable
from sil.fmi.runtime import (
    BinaryBuffer,
    ClockedBuffer,
    CoSimulation,
    ScalarBuffer,
)


@dataclass(frozen=True)
class Binding:
    """One Channel schema field bound to one FMU variable."""

    channel: str
    field: str
    variable: Variable

    def __str__(self) -> str:
        """How every diagnostic about this binding opens."""
        return (
            f"Channel {self.channel!r} field {self.field!r} names FMU "
            f"variable {self.variable.name!r}"
        )


@dataclass(frozen=True)
class BinaryField:
    """One Binary variable and the bounded Channel fields carrying it."""

    variable: Variable
    field: str
    length_field: str
    capacity: int
    # Set when the variable declares a Clock: the Clock that gates it, and the
    # field stating the FMI event time of the activation being carried.
    clock: Variable | None = None
    event_time_field: str | None = None


def causality_of(direction: str) -> str:
    """The FMU causality a Channel of this direction binds.

    The two vocabularies meet here and nowhere else: the init line says which
    way a Channel runs, and `modelDescription.xml` says what a variable is.
    """
    return "input" if direction == "in" else "output"


def outgoing_payload(binary: BinaryField, fields: dict) -> bytes:
    """The bytes of one Message's payload field, as the FMU is handed them.

    A Message's payload field is the Channel's whole bound; the length field
    beside it says how much of that is the payload. Nothing is truncated to
    fit — a length above the bound aborts the Run.
    """
    length = fields[binary.length_field]
    if length > binary.capacity:
        raise ParticipantFailure(
            f"field {binary.length_field!r} declares {length} bytes, "
            f"above the {binary.capacity} field {binary.field!r} "
            f"carries for FMU variable {binary.variable.name!r}"
        )
    return fields[binary.field][:length]


def _incoming_payload(binary: BinaryField, payload: bytes) -> dict:
    """One Binary value the FMU produced, as the Channel's bounded fields.

    The payload is padded to the Channel's bound with zeros, so one Manifest
    records the same bytes on every Run.
    """
    if len(payload) > binary.capacity:
        raise ParticipantFailure(
            f"FMU variable {binary.variable.name!r} produced {len(payload)} "
            f"bytes; field {binary.field!r} carries {binary.capacity}"
        )
    return {
        binary.field: payload + bytes(binary.capacity - len(payload)),
        binary.length_field: len(payload),
    }


def activation_message(binary: BinaryField, payload: bytes,
                       event_time_ns: int) -> dict:
    """One Clock activation, as the three fields a clocked Channel carries.

    Stated once, because both ends of the boundary state it: the Channel a
    single clocked FMU publishes and the Channel a group observes a terminal
    through carry the same Message, and a second spelling of it could drift.
    """
    return {
        **_incoming_payload(binary, payload),
        binary.event_time_field: event_time_ns,
    }


class ScalarGroup:
    """One Channel's fields bound to FMU variables of one scalar type.

    The order the fields were bound in is the order the buffer holds them in,
    so one group is one FMI call however many fields it carries. What the
    values are held as between the two calls is the buffer's business; what
    this knows is which field each one belongs to and how the schema carries
    it.
    """

    def __init__(self, kind: str, bound: list[Binding]):
        self._scalar = SCALARS[kind]
        self._fields = [binding.field for binding in bound]
        self._buffer = ScalarBuffer(
            kind, [binding.variable.reference for binding in bound]
        )

    def write(self, fmu: CoSimulation, fields: dict) -> None:
        self._buffer.write(fmu, [fields[name] for name in self._fields])

    def read(self, fmu: CoSimulation, into: dict) -> None:
        to_field = self._scalar.to_field
        into.update(
            (name, to_field(value))
            for name, value in zip(self._fields, self._buffer.read(fmu))
        )


class BinaryGroup:
    """One Channel's Binary variables, as bounded payloads with a length.

    A group writes or reads, never both, because the Channel's direction
    decides which, and the buffer it hands the FMU is told so at build time.
    What this adds to it is the Channel's own representation: a payload out of
    the length field beside it on a write, and a payload padded to the
    Channel's bound with its length stated beside it on a read.
    """

    def __init__(self, bound: list[BinaryField], causality: str):
        self._bound = bound
        self._buffer = BinaryBuffer(
            [binary.variable.reference for binary in bound],
            [binary.capacity for binary in bound],
            incoming=causality == "input",
        )

    def write(self, fmu: CoSimulation, fields: dict) -> None:
        self._buffer.write(fmu, [
            outgoing_payload(binary, fields) for binary in self._bound
        ])

    def read(self, fmu: CoSimulation, into: dict) -> None:
        for binary, payload in zip(self._bound, self._buffer.read(fmu)):
            into.update(_incoming_payload(binary, payload))


class ChannelBinding:
    """One Channel's fields, written into or read out of the FMU."""

    def __init__(self, groups: list):
        self._groups = groups

    def write(self, fmu: CoSimulation, fields: dict) -> None:
        for group in self._groups:
            group.write(fmu, fields)

    def read(self, fmu: CoSimulation) -> dict:
        fields: dict = {}
        for group in self._groups:
            group.read(fmu, fields)
        return fields


class ClockedPayload:
    """One Channel whose Messages are activations of one Clock.

    One activation is one Message, and the Message states the FMI event time
    it belongs to rather than being timestamped with it.
    """

    def __init__(self, binary: BinaryField, causality: str):
        self._binary = binary
        self._buffer = ClockedBuffer(
            binary.variable, binary.clock, binary.capacity,
            incoming=causality == "input",
        )

    def activation(self, fmu: CoSimulation, event_time_ns: int) -> dict | None:
        """The Message this Clock's activation carries, if it is active."""
        payload = self._buffer.read(fmu)
        if payload is None:
            return None
        return activation_message(self._binary, payload, event_time_ns)

    def activate(self, fmu: CoSimulation, fields: dict) -> None:
        """Hand one Message to the FMU as an activation of its input Clock.

        The Message's own event time is not used: this importer activates the
        Clock at the communication point it is standing on, and quietly
        dating the activation as its sender did would claim a time the FMU
        was never driven to.
        """
        self._buffer.deliver(fmu, outgoing_payload(self._binary, fields))
