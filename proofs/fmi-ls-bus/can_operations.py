"""Decode the CAN bus operations an FMI-LS-BUS Binary variable carries.

An FMI-LS-BUS `Tx_Data` or `Rx_Data` value is not one message: it is a buffer
of consecutive operations, each an 8-byte little-endian header — `opCode` and
the operation's total `length` — followed by that operation's own fields. The
layout is fixed by `fmi3LsBus.h` and `fmi3LsBusCan.h` at the pinned revision
recorded in README.md, and is byte-identical in FMI-LS-BUS v1.0.0.

Only the three operations the pinned CAN FMUs emit are decoded: the node's
transmit and configuration operations, and the confirmation the bus simulation
FMU answers a transmitting node with. Every other operation the standard
defines is rejected by name rather than skipped: this fixture's job is to state
what the supported profile covers, and an operation silently passed over would
widen that claim without evidence.

Nothing here imports SiL. The decoder is what reads the fixture's payloads in
the reference exchange, so it has to be independent of the importer it judges.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

# `fmi3LsBusOperationHeader`: two little-endian uint32, packed.
_HEADER = struct.Struct("<II")
HEADER_SIZE = _HEADER.size

# The CAN operation codes of the layered standard. The two this fixture
# exercises are decoded; the rest are named so a payload outside the supported
# profile fails with the standard's own word for what it contained.
_CAN_TRANSMIT = 0x0010
_CONFIRM = 0x0020
_CONFIGURATION = 0x0040
_OPERATION_NAMES = {
    0x0001: "FormatError",
    _CAN_TRANSMIT: "CanTransmit",
    0x0011: "CanFdTransmit",
    0x0012: "CanXlTransmit",
    _CONFIRM: "Confirm",
    0x0030: "ArbitrationLost",
    0x0031: "BusError",
    _CONFIGURATION: "Configuration",
    0x0041: "Status",
    0x0042: "Wakeup",
}

# `fmi3LsBusCanOperationCanTransmit` after the header: id, ide, rtr, then the
# data length that the payload bytes follow.
_TRANSMIT = struct.Struct("<IBBH")

# `fmi3LsBusCanOperationConfirm` after the header: the CAN ID of the frame the
# bus simulation FMU has transmitted, and nothing else.
_CONFIRM_BODY = struct.Struct("<I")

# `fmi3LsBusCanOperationConfiguration` after the header: the parameter type,
# then the one member of the union that parameter selects.
_PARAMETER_TYPES = {1: "CanBaudrate", 2: "CanFdBaudrate", 3: "CanXlBaudrate",
                    4: "ArbitrationLostBehavior"}
_BAUDRATE_PARAMETERS = ("CanBaudrate", "CanFdBaudrate", "CanXlBaudrate")
_ARBITRATION_LOST_BEHAVIORS = {1: "BufferAndRetransmit", 2: "DiscardAndNotify"}


class OperationError(ValueError):
    """A payload that is not a well-formed operation of this profile."""


@dataclass(frozen=True)
class Operation:
    """One decoded bus operation: the standard's name for it, and its fields.

    The payload bytes are held as hex rather than as `bytes` so that a decoded
    operation compares, prints, and serialises as the same value — the
    evidence this fixture retains is read by people as well as by code.
    """

    name: str
    fields: dict[str, object] = field(default_factory=dict)


def decode(payload: bytes) -> list[Operation]:
    """Every operation in one Binary value, in the order the buffer holds them.

    A buffer is consumed whole: a trailing byte that is not a complete
    operation is a decoding failure, not a shorter list.
    """
    operations: list[Operation] = []
    offset = 0
    while offset < len(payload):
        operations.append(_decode_one(payload, offset))
        offset += _length_at(payload, offset)
    return operations


def _length_at(payload: bytes, offset: int) -> int:
    """The declared total length of the operation starting at `offset`."""
    return _HEADER.unpack_from(payload, offset)[1]


def _decode_one(payload: bytes, offset: int) -> Operation:
    """The one operation starting at `offset`, with its bounds checked here.

    Both bound checks belong before any field is read: an operation that
    declares a length the buffer cannot hold is the shape a truncated or
    misaligned transfer takes, and reading its fields anyway would report a
    plausible frame built out of the next operation's bytes.
    """
    remaining = len(payload) - offset
    if remaining < HEADER_SIZE:
        raise OperationError(
            f"payload ends in {remaining} bytes, which is no 8-byte "
            f"operation header"
        )
    op_code, length = _HEADER.unpack_from(payload, offset)
    name = _OPERATION_NAMES.get(op_code, f"0x{op_code:04x}")
    if length < HEADER_SIZE or length > remaining:
        raise OperationError(
            f"{name} operation declares {length} bytes, and the payload "
            f"holds {remaining} from here"
        )
    body = payload[offset + HEADER_SIZE:offset + length]
    if op_code == _CAN_TRANSMIT:
        return Operation("CanTransmit", _transmit_fields(body))
    if op_code == _CONFIRM:
        return Operation("Confirm", _confirm_fields(body))
    if op_code == _CONFIGURATION:
        return Operation("Configuration", _configuration_fields(body))
    raise OperationError(
        f"{name} operation is outside this fixture's supported CAN profile"
    )


def _transmit_fields(body: bytes) -> dict[str, object]:
    """The fields of a CAN transmit operation, with its two lengths agreed.

    `dataLength` and the operation's own length are independent statements
    about the same payload; a frame whose two lengths disagree is rejected
    rather than resolved in favour of either.
    """
    if len(body) < _TRANSMIT.size:
        raise OperationError(
            f"CanTransmit operation carries {len(body)} bytes after its "
            f"header, which is short of the {_TRANSMIT.size} its fields need"
        )
    identifier, ide, rtr, data_length = _TRANSMIT.unpack_from(body)
    data = body[_TRANSMIT.size:]
    if data_length != len(data):
        raise OperationError(
            f"CanTransmit operation declares dataLength {data_length} and "
            f"carries {len(data)} payload bytes"
        )
    return {
        "id": identifier,
        "ide": bool(ide),
        "rtr": bool(rtr),
        "data": data.hex(),
    }


def _confirm_fields(body: bytes) -> dict[str, object]:
    """The one field a confirmation carries: the frame it confirms.

    The operation is fixed-length, so a body of any other size is a payload
    that is not this operation rather than one with something extra in it.
    """
    if len(body) != _CONFIRM_BODY.size:
        raise OperationError(
            f"Confirm operation carries {len(body)} bytes after its header, "
            f"and a confirmation is the {_CONFIRM_BODY.size} of a CAN ID"
        )
    return {"id": _CONFIRM_BODY.unpack(body)[0]}


def _configuration_fields(body: bytes) -> dict[str, object]:
    """The one parameter a configuration operation carries.

    The standard's structure holds a union, so the parameter type decides how
    many bytes follow and how they are read.
    """
    if not body:
        raise OperationError(
            "Configuration operation carries no parameter type"
        )
    parameter = _PARAMETER_TYPES.get(body[0])
    if parameter is None:
        raise OperationError(
            f"Configuration operation names parameter type {body[0]}, which "
            f"the CAN layered standard does not define"
        )
    value = body[1:]
    if parameter in _BAUDRATE_PARAMETERS:
        if len(value) != 4:
            raise OperationError(
                f"{parameter} configuration carries {len(value)} bytes, and "
                f"a baud rate is four"
            )
        return {"parameter": parameter,
                "baudrate": int.from_bytes(value, "little")}
    if len(value) != 1:
        raise OperationError(
            f"{parameter} configuration carries {len(value)} bytes, and an "
            f"arbitration lost behavior is one"
        )
    behavior = _ARBITRATION_LOST_BEHAVIORS.get(value[0])
    if behavior is None:
        raise OperationError(
            f"{parameter} configuration names behavior {value[0]}, which the "
            f"CAN layered standard does not define"
        )
    return {"parameter": parameter, "behavior": behavior}
