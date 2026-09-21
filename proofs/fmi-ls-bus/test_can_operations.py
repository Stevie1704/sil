"""The decoder's own tests, run before the fixture is ever executed.

Every byte string here is written from the FMI-LS-BUS headers, not captured
from a Run: `fmi3LsBus.h` fixes the 8-byte operation header as a little-endian
`opCode` and `length` pair, and `fmi3LsBusCan.h` fixes each CAN operation's
fields. A decoder tested against its own recording would agree with whatever
the FMU emitted; these expectations disagree when the FMU is wrong.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The decoder is proof-local, so the test finds it beside itself rather than
# through an installed package.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from can_operations import Operation, OperationError, decode

# CAN baudrate 100000: header (opCode 0x40, length 13), parameterType 0x01,
# baudrate 0x000186a0.
CONFIGURATION_BAUDRATE = bytes.fromhex("400000000d000000" "01" "a0860100")
# Arbitration lost behavior BUFFER_AND_RETRANSMIT: parameterType 0x04, 0x01.
CONFIGURATION_ARBITRATION = bytes.fromhex("400000000a000000" "04" "01")
# CAN frame, ID 0x1, no IDE, no RTR, four payload bytes.
CAN_TRANSMIT = bytes.fromhex(
    "1000000014000000" "01000000" "00" "00" "0400" "01020304"
)


def test_empty_payload_decodes_to_no_operations():
    assert decode(b"") == []


def test_configuration_baudrate():
    assert decode(CONFIGURATION_BAUDRATE) == [
        Operation("Configuration", {"parameter": "CanBaudrate",
                                    "baudrate": 100_000})
    ]


def test_configuration_arbitration_lost_behavior():
    assert decode(CONFIGURATION_ARBITRATION) == [
        Operation("Configuration",
                  {"parameter": "ArbitrationLostBehavior",
                   "behavior": "BufferAndRetransmit"})
    ]


def test_can_transmit():
    assert decode(CAN_TRANSMIT) == [
        Operation("CanTransmit", {"id": 1, "ide": False, "rtr": False,
                                  "data": "01020304"})
    ]


def test_operations_decode_in_buffer_order():
    payload = CONFIGURATION_BAUDRATE + CONFIGURATION_ARBITRATION
    assert [operation.name for operation in decode(payload)] == [
        "Configuration", "Configuration"
    ]


def test_empty_can_payload_is_a_frame_with_no_data():
    payload = bytes.fromhex("1000000010000000" "01000000" "00" "00" "0000")
    assert decode(payload) == [
        Operation("CanTransmit", {"id": 1, "ide": False, "rtr": False,
                                  "data": ""})
    ]


def test_partial_header_is_rejected():
    with pytest.raises(OperationError, match="8-byte operation header"):
        decode(CAN_TRANSMIT[:6])


def test_declared_length_past_the_buffer_is_rejected():
    with pytest.raises(OperationError, match="declares 20 bytes"):
        decode(CAN_TRANSMIT[:-1])


def test_declared_length_below_the_header_is_rejected():
    with pytest.raises(OperationError, match="declares 4 bytes"):
        decode(bytes.fromhex("1000000004000000"))


def test_data_length_disagreeing_with_the_operation_length_is_rejected():
    payload = bytes.fromhex(
        "1000000014000000" "01000000" "00" "00" "0300" "01020304"
    )
    with pytest.raises(OperationError, match="dataLength 3"):
        decode(payload)


def test_a_can_operation_outside_the_supported_profile_is_named():
    # CAN FD transmit, 0x0011: defined by the standard, outside this fixture.
    with pytest.raises(OperationError, match="CanFdTransmit"):
        decode(bytes.fromhex("110000000f000000" "01000000" "000000" "0000"))


def test_an_unknown_operation_code_is_reported_as_its_code():
    with pytest.raises(OperationError, match="0x0099"):
        decode(bytes.fromhex("9900000008000000"))


def test_an_unknown_configuration_parameter_is_rejected():
    with pytest.raises(OperationError, match="parameter type 9"):
        decode(bytes.fromhex("400000000a000000" "09" "01"))
