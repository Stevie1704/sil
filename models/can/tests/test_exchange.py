"""Independent FMI calls and a kernel Run against the exact same archives."""

import ctypes
import hashlib
import json
import shutil
import struct
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest
from fmpy import extract, read_model_description
from fmpy.fmi3 import FMU3Slave
from fmpy.fmi1 import FMICallException

import wire

ROOT = Path(__file__).resolve().parents[3]
ARTIFACTS = ROOT / "build/can"
PROFILE = json.loads((ROOT / "models/can/profile.json").read_text())
MODEL = PROFILE["model_name"]
LAYOUT = PROFILE["layout"]
NS = 1_000_000_000
BITRATE = 100_000
BIT_TIME_NS = NS // BITRATE
MAX_CLASSICAL_IDENTIFIER = 0x7FF
UPSTREAM_PAYLOAD = bytes((1, 2, 3, 4))
FRAME = bytes.fromhex("1000000014000000010000000000040001020304")
CONFIRM = bytes.fromhex("200000000c00000001000000")
CONFIG = bytes.fromhex("400000000d00000001a0860100400000000a0000000401")
UPSTREAM_REQUEST_NS = 300_000_000
LATER_REQUEST_NS = 600_000_000


def terminal_ref(node, member):
    return LAYOUT["terminal_stride"] * node + LAYOUT[member]


def config(rate):
    return struct.pack("<IIBI", 0x40, 13, 1, rate) + CONFIG[13:]


def frame(identifier, data):
    return (
        struct.pack("<IIIBBH", 0x10, 16 + len(data), identifier, 0, 0, len(data)) + data
    )


def confirm(identifier):
    return struct.pack("<III", 0x20, 12, identifier)


def bus_error(identifier, error_flag, is_sender):
    return struct.pack("<IIIBBB", 0x31, 15, identifier, 1, error_flag, is_sender)


def frame_end_ns(request_ns, identifier=1, payload=UPSTREAM_PAYLOAD, bitrate=BITRATE):
    return request_ns + wire.frame_bits(identifier, payload) * (NS // bitrate)


UPSTREAM_END_NS = frame_end_ns(UPSTREAM_REQUEST_NS)


def successful_frame_trace(request_ns, *, identifier=1, payload=UPSTREAM_PAYLOAD,
                           senders=(0,), active_nodes=2, suppressed_receiver=None):
    end = frame_end_ns(request_ns, identifier, payload)
    operation = frame(identifier, payload)
    trace = []
    for node in range(active_nodes):
        if node == suppressed_receiver:
            continue
        output = confirm(identifier) if node in senders else operation
        trace.append((end, node, output))
    return trace


def bus_error_trace(end_ns, identifier=1, *, primary_sender=0, active_nodes=2):
    return [
        (end_ns, node, bus_error(
            identifier,
            1 if node == primary_sender else 2,
            1 if node == primary_sender else 0,
        ))
        for node in range(active_nodes)
    ]


def expected_scenario(parameters, expected, observed, **metadata):
    return {
        "parameters": parameters,
        "expected_event_table": trace_rows(expected),
        "observed_trace": trace_rows(observed),
        **metadata,
    }


RULE_OFFSETS = {
    "kind": "fault_rule_kind",
    "sender_node": "fault_rule_sender",
    "receiver_node": "fault_rule_receiver",
    "identifier": "fault_rule_identifier",
    "request_start_ns": "fault_rule_first_request",
    "request_end_ns": "fault_rule_last_request",
    "occurrence": "fault_rule_occurrence",
    "attempt": "fault_rule_attempt",
}
RULE_PARAMETER_NAMES = {
    "kind": "Kind",
    "sender_node": "SenderNode",
    "receiver_node": "ReceiverNode",
    "identifier": "Identifier",
    "request_start_ns": "RequestStartNs",
    "request_end_ns": "RequestEndNs",
    "occurrence": "Occurrence",
    "attempt": "Attempt",
}


def fault_parameters(rules=(), retry_limit=1, *, count=None):
    """Materialize the exact fixed FMI parameters used by one Run."""
    parameters = [
        (LAYOUT["fault_retry_limit"], retry_limit),
        (LAYOUT["fault_rule_count"], len(rules) if count is None else count),
    ]
    for index, rule in enumerate(rules):
        for field, value in rule.items():
            parameters.append((
                LAYOUT["fault_rule_base"]
                + index * LAYOUT["fault_rule_stride"]
                + LAYOUT[RULE_OFFSETS[field]],
                value,
            ))
    return parameters


def fault_start_values(rules, retry_limit=1):
    starts = [
        f"bus.faultRetryLimit={retry_limit}",
        f"bus.faultRuleCount={len(rules)}",
    ]
    for index, rule in enumerate(rules):
        starts.extend(
            f"bus.faultRule{index + 1}{RULE_PARAMETER_NAMES[field]}={value}"
            for field, value in rule.items()
        )
    return starts


def trace_rows(trace):
    return [(instant, node, payload.hex()) for instant, node, payload in trace]


def retain_independent_scenario(name, scenario, *, reset=False):
    path = ARTIFACTS / "issue155-independent.json"
    evidence = {"scenarios": {}}
    if path.exists() and not reset:
        evidence = json.loads(path.read_text())
    evidence["fmu_sha256"] = hashlib.sha256(
        (ARTIFACTS / f"{MODEL}.fmu").read_bytes()
    ).hexdigest()
    evidence.setdefault("scenarios", {})[name] = scenario
    path.write_text(json.dumps(evidence, indent=2) + "\n")


@contextmanager
def initialized_fmu(name=MODEL, logs=None, *, exit_initialization=True,
                    active_nodes=None, queue_capacity=None, parameters=(),
                    individual_parameter_calls=False):
    path = ARTIFACTS / f"{name}.fmu"
    description = read_model_description(path, validate=True)
    unpacked = extract(path)
    fmu = FMU3Slave(
        guid=description.guid,
        unzipDirectory=unpacked,
        modelIdentifier=description.coSimulation.modelIdentifier,
        instanceName=name,
    )

    def log(_environment, _status, _category, message):
        if logs is not None:
            logs.append(message.decode())

    fmu.instantiate(eventModeUsed=True, loggingOn=True, logMessage=log)
    try:
        assignments = []
        if active_nodes is not None:
            assignments.append((LAYOUT["active_nodes"], active_nodes))
        if queue_capacity is not None:
            assignments.append((LAYOUT["queue_capacity"], queue_capacity))
        assignments.extend(parameters)
        if individual_parameter_calls:
            for reference, value in assignments:
                fmu.setFloat64([reference], [float(value)])
        elif assignments:
            refs, values = zip(*assignments)
            refs = list(refs)
            values = [float(value) for value in values]
            fmu.setFloat64(refs, values)
        fmu.enterInitializationMode(startTime=0)
        if exit_initialization:
            fmu.exitInitializationMode()
        yield fmu
    finally:
        fmu.freeInstance()
        shutil.rmtree(unpacked)


def deliver(fmu, data, node=0):
    fmu.setClock([terminal_ref(node, "rx_clock")], [True])
    fmu.setBinary([terminal_ref(node, "rx_data")], [data])


def transmit_configured(fmu, data):
    """Node2 agrees on 100 kbit/s while Node1 configures it and sends."""
    deliver(fmu, CONFIG, 1)
    deliver(fmu, CONFIG + data)


def intervals(fmu, nodes=2):
    refs = (ctypes.c_uint32 * nodes)(
        *(terminal_ref(n, "tx_clock") for n in range(nodes))
    )
    counters = (ctypes.c_uint64 * nodes)()
    resolutions = (ctypes.c_uint64 * nodes)()
    qualifiers = (ctypes.c_int * nodes)()
    fmu.fmi3GetIntervalFraction(
        fmu.component, refs, nodes, counters, resolutions, qualifiers
    )
    return list(counters), list(resolutions), list(qualifiers)


def advance(fmu, start, end):
    fmu.enterStepMode()
    _, terminate, early, last = fmu.doStep(
        currentCommunicationPoint=start, communicationStepSize=end - start
    )
    assert not terminate and not early and last == pytest.approx(end)
    fmu.enterEventMode()


def drive(bus, requests, until_ns, grid_ns=None, nodes=2):
    """An independent event-driven FMI master for the bus alone.

    It stops at every request instant, at every countdown instant the bus
    states and, as an outer Step grid, at every multiple of `grid_ns`.
    Returns every Tx activation as (instant_ns, node, payload).
    """
    trace, now, due = [], 0, None
    while True:
        for _, node, data in (r for r in requests if r[0] == now):
            deliver(bus, data, node)
        if due == now:
            bus.setClock([terminal_ref(n, "tx_clock") for n in range(nodes)],
                         [True] * nodes)
            outputs = bus.getBinary([
                terminal_ref(n, "tx_data") for n in range(nodes)
            ])
            trace.extend((now, n, data) for n, data in enumerate(outputs) if data)
        bus.updateDiscreteStates()
        counters, resolutions, qualifiers = intervals(bus, nodes)
        assert len(set(qualifiers)) == len(set(counters)) == 1
        if qualifiers[0] == 2:
            assert resolutions[0] == NS
            due = now + counters[0]
        elif qualifiers[0] == 0:
            due = None
        stops = [r[0] for r in requests if r[0] > now] + [until_ns]
        stops += [due] if due is not None else []
        stops += [(now // grid_ns + 1) * grid_ns] if grid_ns else []
        end = min(stops)
        if now == until_ns:
            return trace
        advance(bus, now / NS, end / NS)
        now = end


def test_independent_external_exchange():
    logs = []
    with (
        initialized_fmu("ExternalSender") as sender,
        initialized_fmu("ExternalReceiver", logs) as receiver,
        initialized_fmu() as bus,
    ):
        for node, peer in enumerate((sender, receiver)):
            assert peer.getClock([3]) == [True]
            assert peer.getClock([3]) == [False]
            assert peer.getBinary([1]) == [CONFIG]
            deliver(bus, CONFIG, node)
            peer.updateDiscreteStates()
            assert peer.getClock([3]) == [False]
        bus.updateDiscreteStates()
        assert intervals(bus)[2] == [0, 0]
        for fmu in (sender, receiver, bus):
            advance(fmu, 0, 0.3)
        assert sender.getClock([3]) == [True]
        assert sender.getBinary([1]) == [FRAME]
        assert receiver.getClock([3]) == [False]
        deliver(bus, sender.getBinary([1])[0])
        for fmu in (sender, receiver, bus):
            fmu.updateDiscreteStates()
        assert intervals(bus) == ([830000] * 2, [NS] * 2, [2, 2])
        assert intervals(bus) == ([830000] * 2, [NS] * 2, [1, 1])
        for fmu in (sender, receiver, bus):
            advance(fmu, 0.3, UPSTREAM_END_NS / NS)
        bus.setClock([3, 7], [True, True])
        assert bus.getBinary([1, 5]) == [CONFIRM, FRAME]
        deliver(sender, CONFIRM)
        deliver(receiver, FRAME)
        for fmu in (sender, receiver, bus):
            fmu.updateDiscreteStates()
        assert any("Received CAN frame with ID 1 and length 4" in line for line in logs)
        assert intervals(bus)[2] == [0, 0]
        for fmu in (sender, receiver, bus):
            advance(fmu, UPSTREAM_END_NS / NS, 0.31)
            fmu.updateDiscreteStates()
        assert sender.getClock([3]) == receiver.getClock([3]) == [False]
        report = {
            "event_time_ns": UPSTREAM_END_NS,
            "sender": CONFIRM.hex(),
            "receiver": FRAME.hex(),
            "receiver_log": logs,
            "fmu_sha256": hashlib.sha256(
                (ARTIFACTS / f"{MODEL}.fmu").read_bytes()
            ).hexdigest(),
        }
        (ARTIFACTS / "independent.json").write_text(json.dumps(report, indent=2) + "\n")


# Independently hand-derived in README.md: ID 0 with all-zero data.
HAND_BITS = {0: 50, 1: 56, 8: 124}


@pytest.mark.parametrize("rate", [125000, 500000])
@pytest.mark.parametrize("size", [0, 1, 8])
def test_hand_derived_frame_timing(rate, size):
    bits, bit = HAND_BITS[size], NS // rate
    assert wire.frame_bits(0, bytes(size)) == bits
    op = frame(0, bytes(size))
    first = bits * bit
    # The reply arrives at the first frame's end: it waits out intermission.
    second = first + (wire.INTERMISSION + bits) * bit
    requests = [(0, 0, config(rate) + op), (0, 1, config(rate)), (first, 1, op)]
    with initialized_fmu() as bus, initialized_fmu() as other:
        trace = drive(bus, requests, until_ns=second + NS // 1000)
        assert intervals(other)[2] == [0, 0]
    assert trace == [
        (first, 0, confirm(0)),
        (first, 1, op),
        (second, 0, op),
        (second, 1, confirm(0)),
    ]


# Node1 sends A then, while A is on the wire, B; Node2's C arrives during B.
A, B, C = frame(0, bytes(8)), frame(0x7FF, b"\xff" * 8), frame(1, bytes([1, 2, 3, 4]))
BURST = [
    (0, 0, config(500000)),
    (0, 1, config(500000)),
    (1000, 0, A),
    (50000, 0, B),
    (300000, 1, C),
]
BURST_TRACE = [
    (249000, 0, confirm(0)),
    (249000, 1, A),
    (501000, 0, confirm(0x7FF)),
    (501000, 1, B),
    (673000, 0, C),
    (673000, 1, confirm(1)),
]
BURST_END_NS = 1_000_000


def test_burst_expectation_matches_independent_reference():
    bit, gap = 2000, wire.INTERMISSION * 2000
    a_end = 1000 + wire.frame_bits(0, bytes(8)) * bit
    b_end = a_end + gap + wire.frame_bits(0x7FF, b"\xff" * 8) * bit
    c_end = b_end + gap + wire.frame_bits(1, bytes([1, 2, 3, 4])) * bit
    assert [a_end, b_end, c_end] == [t for t, node, _ in BURST_TRACE if node == 0]


# No grid; bit-aligned so every completion is on a Step boundary; A's end
# exactly on a boundary; one Step for the whole burst.
@pytest.mark.parametrize("grid_ns", [None, 1000, 249000, BURST_END_NS])
def test_burst_trace_is_independent_of_outer_steps(grid_ns):
    with initialized_fmu() as bus:
        assert drive(bus, BURST, BURST_END_NS, grid_ns) == BURST_TRACE


FAULT_RULE = {
    "kind": 1,
    "sender_node": 1,
    "receiver_node": 0,
    "identifier": 1,
    "request_start_ns": 300_000_000,
    "request_end_ns": 300_000_000,
    "occurrence": 1,
    "attempt": 1,
}
FAULT_REQUESTS = [
    (0, 0, CONFIG),
    (0, 1, CONFIG),
    (UPSTREAM_REQUEST_NS, 0, FRAME),
    (LATER_REQUEST_NS, 0, FRAME),
]
FAULT_UNTIL_NS = 610_000_000
FIRST_ERROR_END_NS = frame_end_ns(UPSTREAM_REQUEST_NS)
RETRY_REQUEST_NS = FIRST_ERROR_END_NS + wire.INTERMISSION * BIT_TIME_NS
RETRY_END_NS = frame_end_ns(RETRY_REQUEST_NS)
LATER_END_NS = frame_end_ns(LATER_REQUEST_NS)
BASELINE_FAULT_TRACE = (
    successful_frame_trace(UPSTREAM_REQUEST_NS)
    + successful_frame_trace(LATER_REQUEST_NS)
)
ONE_ERROR_RECOVERY_TRACE = (
    bus_error_trace(FIRST_ERROR_END_NS)
    + successful_frame_trace(RETRY_REQUEST_NS)
    + successful_frame_trace(LATER_REQUEST_NS)
)


def test_scheduled_transmission_error_recovers_and_stays_deterministic():
    with initialized_fmu() as bus:
        baseline = drive(bus, FAULT_REQUESTS, FAULT_UNTIL_NS)
    assert baseline == BASELINE_FAULT_TRACE
    retain_independent_scenario(
        "no_fault_baseline",
        expected_scenario(
            {"faultRetryLimit": 1, "faultRuleCount": 0},
            BASELINE_FAULT_TRACE, baseline,
        ),
        reset=True,
    )

    inputs = fault_parameters([FAULT_RULE])
    with initialized_fmu(parameters=inputs) as bus:
        first = drive(bus, FAULT_REQUESTS, FAULT_UNTIL_NS)
    with initialized_fmu(parameters=inputs) as bus:
        repeat = drive(bus, FAULT_REQUESTS, FAULT_UNTIL_NS)
    assert first == repeat == ONE_ERROR_RECOVERY_TRACE
    assert first != baseline
    retain_independent_scenario(
        "single_error_recovery",
        expected_scenario(
            {"faultRetryLimit": 1, "faultRuleCount": 1,
             "faultRule1": FAULT_RULE},
            ONE_ERROR_RECOVERY_TRACE, first,
            same_schedule_repeat_equal=first == repeat,
        ),
    )


def test_complete_fault_rule_is_validated_by_set_float64():
    invalid = {**FAULT_RULE, "sender_node": 3}
    with pytest.raises(FMICallException):
        with initialized_fmu(
            exit_initialization=False,
            parameters=fault_parameters([invalid]),
        ):
            pass


def test_fault_schedule_can_be_set_in_separate_fmi_calls():
    parameters = fault_parameters([FAULT_RULE])
    staged = parameters[:1] + parameters[2:] + parameters[1:2]
    with initialized_fmu(
        parameters=staged,
        individual_parameter_calls=True,
    ) as bus:
        observed = drive(bus, FAULT_REQUESTS, FAULT_UNTIL_NS)
    assert observed == ONE_ERROR_RECOVERY_TRACE


def test_retry_limit_exhaustion_and_consumed_rule_recovery():
    retry_error = {**FAULT_RULE, "attempt": 2}
    with initialized_fmu(
        parameters=fault_parameters([FAULT_RULE, retry_error], retry_limit=1)
    ) as bus:
        trace = drive(bus, FAULT_REQUESTS, FAULT_UNTIL_NS)
    expected = (
        bus_error_trace(FIRST_ERROR_END_NS)
        + bus_error_trace(RETRY_END_NS)
        + successful_frame_trace(LATER_REQUEST_NS)
    )
    assert trace == expected
    retain_independent_scenario(
        "retry_exhaustion_then_next_occurrence",
        expected_scenario(
            {"faultRetryLimit": 1, "faultRuleCount": 2,
             "faultRules": [FAULT_RULE, retry_error]},
            expected, trace,
        ),
    )


def test_simultaneous_equal_frames_match_node_identity_in_either_input_order():
    rule = {**FAULT_RULE, "request_start_ns": 1000, "request_end_ns": 1000,
            "attempt": 1, "sender_node": 2}
    simultaneous = [
        (0, 0, CONFIG),
        (0, 1, CONFIG),
        (1000, 0, FRAME),
        (1000, 1, FRAME),
    ]
    first_end = frame_end_ns(1000)
    retry_start = first_end + wire.INTERMISSION * BIT_TIME_NS
    expected = (
        bus_error_trace(first_end, primary_sender=1)
        + successful_frame_trace(retry_start, senders=(0, 1))
    )
    for requests in (simultaneous, list(reversed(simultaneous))):
        with initialized_fmu(parameters=fault_parameters([rule])) as bus:
            assert drive(bus, requests, 1_800_000) == expected
    retain_independent_scenario(
        "identical_timestamp_equal_frame",
        expected_scenario(
            {"faultRetryLimit": 1, "faultRuleCount": 1,
             "faultRule1": rule},
            expected, expected, input_order_invariant=True,
        ),
    )


def test_overlapping_rule_precedence_and_receiver_delivery_suppression():
    suppression = {
        **FAULT_RULE,
        "kind": 2,
        "receiver_node": 2,
        "request_start_ns": 1000,
        "request_end_ns": 1000,
    }
    overlapping_error = {
        **suppression,
        "kind": 1,
        "receiver_node": 0,
    }
    requests = [
        (0, 0, CONFIG),
        (0, 1, CONFIG),
        (1000, 0, FRAME),
    ]
    with initialized_fmu(
        parameters=fault_parameters([suppression, overlapping_error])
    ) as bus:
        suppressed = drive(bus, requests, 900_000)
        expected_suppressed = successful_frame_trace(
            1000, suppressed_receiver=1,
        )
        assert suppressed == expected_suppressed

    # Reversing the same two matching rules selects the transmission error.
    with initialized_fmu(
        parameters=fault_parameters([overlapping_error, suppression])
    ) as bus:
        error_first = drive(bus, requests, 1_800_000)
        first_end = frame_end_ns(1000)
        expected_error_first = (
            bus_error_trace(first_end)
            + successful_frame_trace(
                first_end + wire.INTERMISSION * BIT_TIME_NS,
            )
        )
        assert error_first == expected_error_first
    retain_independent_scenario(
        "overlap_precedence_receiver_suppression",
        {
            "suppression_first": expected_scenario(
                {"faultRetryLimit": 1, "faultRuleCount": 2,
                 "faultRules": [suppression, overlapping_error]},
                expected_suppressed, suppressed,
            ),
            "error_first": expected_scenario(
                {"faultRetryLimit": 1, "faultRuleCount": 2,
                 "faultRules": [overlapping_error, suppression]},
                expected_error_first, error_first,
            ),
        },
    )


def test_absent_fault_match_leaves_successful_transmission_unchanged():
    absent = {**FAULT_RULE, "identifier": 2, "request_start_ns": 1000,
              "request_end_ns": 1000}
    empty_payload_frame = frame(1, b"")
    requests = [(0, 0, CONFIG), (0, 1, CONFIG), (1000, 0, empty_payload_frame)]
    expected = successful_frame_trace(1000, payload=b"")
    with initialized_fmu(parameters=fault_parameters([absent])) as bus:
        trace = drive(bus, requests, 900_000)
        assert trace == expected
    retain_independent_scenario(
        "absent_match",
        expected_scenario(
            {"faultRetryLimit": 1, "faultRuleCount": 1,
             "faultRule1": absent},
            expected, trace,
        ),
    )


@pytest.mark.parametrize(
    "rule,retry_limit,count",
    [
        ({"kind": 3}, 1, 1),
        ({**FAULT_RULE, "sender_node": 0}, 1, 1),
        ({**FAULT_RULE, "sender_node": 3}, 1, 1),
        ({**FAULT_RULE, "identifier": MAX_CLASSICAL_IDENTIFIER + 1}, 1, 1),
        ({**FAULT_RULE, "request_start_ns": 300_000_001}, 1, 1),
        ({**FAULT_RULE, "request_start_ns": 1_125_899_906_842_625}, 1, 1),
        ({**FAULT_RULE, "occurrence": 0}, 1, 1),
        ({**FAULT_RULE, "attempt": 2}, 0, 1),
        ({**FAULT_RULE, "sender_node": 1.5}, 1, 1),
        ({**FAULT_RULE}, 1, 9),
        ({**FAULT_RULE, "receiver_node": 2}, 1, 1),
        ({**FAULT_RULE, "kind": 2, "receiver_node": 1}, 1, 1),
        ({"kind": 1}, 1, 1),  # incomplete rule
        ({**FAULT_RULE}, 1, 0),  # fields beyond the declared count
    ],
)
def test_invalid_fault_schedules_fail_before_initialization(rule, retry_limit, count):
    parameters = fault_parameters([rule], retry_limit=retry_limit, count=count)
    with pytest.raises(FMICallException):
        with initialized_fmu(
            exit_initialization=False, parameters=parameters
        ) as bus:
            bus.exitInitializationMode()


BAD = [
    b"\x10",  # truncated header
    struct.pack("<II", 0x10, 0),  # invalid length
    FRAME[:-1],  # truncated operation
    FRAME + b"\x00",  # trailing junk
    FRAME[:14] + b"\x05\x00" + FRAME[16:],  # payload mismatch
    struct.pack("<IIIBBH", 0x10, 25, 1, 0, 0, 9) + bytes(9),
    FRAME[:8] + struct.pack("<I", 0x800) + FRAME[12:],
    FRAME[:12] + b"\x01" + FRAME[13:],  # extended
    FRAME[:13] + b"\x01" + FRAME[14:],  # remote
    struct.pack("<II", 0x11, 8),  # CAN FD
    struct.pack("<II", 0x12, 8),  # CAN XL
    struct.pack("<II", 0xDEAD, 8),  # unknown opcode
    config(83333),  # bit time is no whole number of nanoseconds
    config(0),
    config(9999),  # below the supported domain
    config(2000000),  # above Classical CAN
    config(500000),  # inconsistent with the already configured 100000
    struct.pack("<IIBI", 0x40, 13, 2, 100000),  # FD bitrate
    struct.pack("<IIBB", 0x40, 10, 4, 3),  # invalid policy
    CONFIRM,  # wrong direction
]


@pytest.mark.parametrize("payload", BAD)
def test_reject_malformed_and_unsupported(payload):
    with initialized_fmu() as bus:
        # One atomic transaction: the valid configuration is not committed.
        deliver(bus, CONFIG + payload)
        with pytest.raises(FMICallException, match="fmi3UpdateDiscreteStates.*3"):
            bus.updateDiscreteStates()
        # Failure is sticky; a reset restores the initial independent state.
        with pytest.raises(FMICallException):
            bus.enterStepMode()
        bus.reset()
        bus.enterInitializationMode(startTime=0)
        bus.exitInitializationMode()
        assert intervals(bus)[2] == [0, 0]


def test_competing_terminals():
    with initialized_fmu() as bus:
        deliver(bus, CONFIG + FRAME, 0)
        deliver(bus, CONFIG + FRAME, 1)
        bus.updateDiscreteStates()
        assert intervals(bus)[0] == [830000, 830000]
        advance(bus, 0, 0.00083)
        bus.setClock([3, 7], [True, True])
        assert bus.getBinary([1, 5]) == [CONFIRM, CONFIRM]


def lost(identifier):
    return struct.pack("<III", 0x30, 12, identifier)


THREE_REQUESTS = [
    (0, 0, config(125000)),
    (0, 1, config(125000)),
    (0, 2, config(125000) + struct.pack("<IIBB", 0x40, 10, 4, 2)),
    (1000, 0, frame(2, b"")),
    (1000, 1, frame(0, b"")),
    (1000, 2, frame(1, b"")),
]
THREE_TRACE = [
    (401000, 0, frame(0, b"")),
    (401000, 1, confirm(0)),
    (401000, 2, lost(1) + frame(0, b"")),
    (801000, 0, confirm(2)),
    (801000, 1, frame(2, b"")),
    (801000, 2, frame(2, b"")),
]


def test_three_node_arbitration_and_discard_independent_of_input_order():
    for requests in (THREE_REQUESTS, list(reversed(THREE_REQUESTS))):
        with initialized_fmu(active_nodes=3, queue_capacity=2) as bus:
            assert drive(bus, requests, 900000, nodes=3) == THREE_TRACE


def test_staggered_contention_reconsiders_pending_heads():
    requests = [(0, node, config(125000)) for node in range(3)] + [
        (1000, 0, frame(2, b"")),
        (100000, 1, frame(3, b"")),
        (200000, 2, frame(1, b"")),
        (300000, 0, frame(0, b"")),
    ]
    expected = [
        (377000, 0, confirm(2)), (377000, 1, frame(2, b"")),
        (377000, 2, frame(2, b"")),
        (801000, 0, confirm(0)), (801000, 1, frame(0, b"")),
        (801000, 2, frame(0, b"")),
        (1201000, 0, frame(1, b"")), (1201000, 1, frame(1, b"")),
        (1201000, 2, confirm(1)),
        (1601000, 0, frame(3, b"")), (1601000, 1, confirm(3)),
        (1601000, 2, frame(3, b"")),
    ]
    for grid in (None, 1000, 377000):
        with initialized_fmu(active_nodes=3, queue_capacity=2) as bus:
            assert drive(bus, requests, 1800000, grid, nodes=3) == expected


def test_multi_operation_fifo_and_completion_boundary():
    first = frame(1, b"")
    requests = [
        (0, n, config(125000)) for n in range(3)
    ] + [
        (1000, 0, frame(2, b"") + frame(0, b"")),
        (1000, 1, first),
        (1000, 2, frame(3, b"")),
        (377000, 1, frame(0, b"")),
    ]
    expected = [
        (377000, 0, first), (377000, 1, confirm(1)), (377000, 2, first),
        (801000, 0, frame(0, b"")), (801000, 1, confirm(0)),
        (801000, 2, frame(0, b"")),
        (1201000, 0, confirm(2)), (1201000, 1, frame(2, b"")),
        (1201000, 2, frame(2, b"")),
        (1625000, 0, confirm(0)), (1625000, 1, frame(0, b"")),
        (1625000, 2, frame(0, b"")),
        (2025000, 0, frame(3, b"")), (2025000, 1, frame(3, b"")),
        (2025000, 2, confirm(3)),
    ]
    for grid in (None, 1000, 377000):
        with initialized_fmu(active_nodes=3, queue_capacity=2) as bus:
            assert drive(bus, requests, 2200000, grid, nodes=3) == expected


def test_equal_id_difference_and_configured_bounds():
    with initialized_fmu(active_nodes=3) as bus:
        for n in range(3):
            deliver(bus, config(125000), n)
        bus.updateDiscreteStates()
        deliver(bus, frame(1, b"a"), 0)
        deliver(bus, frame(1, b"b"), 1)
        with pytest.raises(FMICallException):
            bus.updateDiscreteStates()
    with initialized_fmu(active_nodes=1, queue_capacity=1) as bus:
        assert intervals(bus, 1)[2] == [0]
        deliver(bus, config(125000) + frame(1, b""))
        bus.updateDiscreteStates()
        advance(bus, 0, 0.000001)
        deliver(bus, frame(2, b""))
        bus.updateDiscreteStates()
        advance(bus, 0.000001, 0.000002)
        deliver(bus, frame(3, b""))
        with pytest.raises(FMICallException):
            bus.updateDiscreteStates()
    with initialized_fmu(active_nodes=1) as bus:
        with pytest.raises(FMICallException):
            deliver(bus, frame(1, b""), 1)
    for settings in ({"active_nodes": 0}, {"active_nodes": 1.5},
                     {"active_nodes": 5}, {"queue_capacity": 0},
                     {"queue_capacity": 65}):
        with pytest.raises(FMICallException):
            with initialized_fmu(**settings):
                pass


@pytest.mark.parametrize(
    "case",
    [
        "unconfigured",
        "peer_unconfigured",
        "beyond_time",
    ],
)
def test_timing_rejections(case):
    with initialized_fmu() as bus:
        with pytest.raises(FMICallException):
            if case == "unconfigured":
                deliver(bus, FRAME)
                bus.updateDiscreteStates()
            elif case == "peer_unconfigured":
                deliver(bus, CONFIG + FRAME)
                bus.updateDiscreteStates()
            elif case == "beyond_time":
                bus.enterStepMode()
                bus.doStep(currentCommunicationPoint=0, communicationStepSize=2e6)


@pytest.mark.parametrize(
    "case",
    [
        "missing_clock",
        "missing_binary",
        "overflow",
        "early_clock",
        "late_step",
        "wrong_reference",
        "wrong_mode",
    ],
)
def test_abi_rejections(case):
    with initialized_fmu() as bus:
        with pytest.raises(FMICallException):
            if case == "missing_clock":
                bus.setBinary([0], [FRAME])
                bus.updateDiscreteStates()
            elif case == "missing_binary":
                bus.setClock([2], [True])
                bus.updateDiscreteStates()
            elif case == "overflow":
                deliver(bus, bytes(2049))
            elif case == "wrong_reference":
                bus.setClock([99], [True])
            elif case == "wrong_mode":
                bus.doStep(currentCommunicationPoint=0, communicationStepSize=0.001)
            else:
                transmit_configured(bus, FRAME)
                bus.updateDiscreteStates()
                if case == "early_clock":
                    bus.setClock([3, 7], [True, True])
                    bus.getBinary([1])
                else:
                    bus.enterStepMode()
                    bus.doStep(currentCommunicationPoint=0, communicationStepSize=0.002)


def test_sil_group_and_determinism():
    from sil import schema
    from sil.manifest import Manifest
    from sil.recording import read_records

    schemas = {
        "can.Buffer": {
            "fields": [
                {"name": "data_length", "type": "u16"},
                {"name": "data", "type": "u8", "count": 2048},
                {"name": "data_event_time_ns", "type": "u64"},
            ]
        }
    }
    sources = {
        "sender": "sender.CanChannel",
        "receiver": "receiver.CanChannel",
        "confirm": "bus.Node1",
        "frame": "bus.Node2",
    }
    manifest = Manifest(duration_ns=310000000)
    manifest.add_schemas(schemas)
    for channel in sources:
        manifest.add_channel(channel, schema="can.Buffer")
    command = ["python", "-m", "sil.fmi"]
    for name, archive in (
        ("sender", "ExternalSender"),
        ("receiver", "ExternalReceiver"),
        ("bus", MODEL),
    ):
        command += ["--instance", name, str(ARTIFACTS / f"{archive}.fmu")]
    command += [
        "--connect",
        "sender.CanChannel=bus.Node1",
        "--connect",
        "receiver.CanChannel=bus.Node2",
        "--bus-profile",
        "application/org.fmi-standard.fmi-ls-bus.can",
    ]
    for channel, terminal in sources.items():
        command += ["--bind", f"{channel}:data={terminal}.Tx_Data"]
    manifest.add_process(
        "can", command=command, step_period_ns=1000000, publishes=list(sources)
    )
    path = manifest.write(ARTIFACTS / "manifest.json").path
    runner = Path("/opt/kernel/sil-run")
    for name in ("first", "second"):
        result = subprocess.run(
            [
                str(runner),
                str(path),
                "--participant-timeout-ms",
                "5000",
                "-o",
                str(ARTIFACTS / f"{name}.mcap"),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        (ARTIFACTS / f"{name}.log").write_text(result.stdout + result.stderr)
        assert result.returncode == 0, result.stdout + result.stderr
    assert (ARTIFACTS / "first.mcap").read_bytes() == (
        ARTIFACTS / "second.mcap"
    ).read_bytes()
    codec = schema.load(schemas)["can.Buffer"]
    actual = {channel: [] for channel in sources}
    for channel, published, raw in read_records(ARTIFACTS / "first.mcap"):
        fields = codec.unpack(raw)
        actual[channel].append(
            (
                fields["data_event_time_ns"],
                fields["data"][: fields["data_length"]],
                published,
            )
        )
    assert actual == {
        "sender": [(0, CONFIG, 0), (300000000, FRAME, 299000000)],
        "receiver": [(0, CONFIG, 0)],
        "confirm": [(UPSTREAM_END_NS, CONFIRM, 300000000)],
        "frame": [(UPSTREAM_END_NS, FRAME, 300000000)],
    }
    (ARTIFACTS / "sil.json").write_text(
        json.dumps(
            {
                key: [(t, b.hex(), p) for t, b, p in values]
                for key, values in actual.items()
            },
            indent=2,
        )
        + "\n"
    )

    # Two upstream senders of the same bits co-transmit and both get Confirm.
    competing = json.loads(path.read_text())
    participant = competing["participants"]["can"]
    participant["command"] = [
        argument.replace("ExternalReceiver.fmu", "ExternalSender.fmu")
        for argument in participant["command"]
    ]
    competing_path = ARTIFACTS / "competing.json"
    competing_path.write_text(json.dumps(competing) + "\n")
    result = subprocess.run(
        [
            str(runner),
            str(competing_path),
            "--participant-timeout-ms",
            "5000",
            "-o", str(ARTIFACTS / "competing.mcap"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    (ARTIFACTS / "competing.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    competing_outputs = {}
    for channel, _, raw in read_records(ARTIFACTS / "competing.mcap"):
        if channel in ("confirm", "frame"):
            fields = codec.unpack(raw)
            competing_outputs[channel] = (
                fields["data_event_time_ns"],
                fields["data"][:fields["data_length"]],
            )
    assert competing_outputs == {
        "confirm": (UPSTREAM_END_NS, CONFIRM),
        "frame": (UPSTREAM_END_NS, CONFIRM),
    }


def faulted_external_manifest(name, rules, *, active_nodes=2, retry_limit=1):
    from sil.manifest import Manifest

    schemas = {"can.Buffer": {"fields": [
        {"name": "data_length", "type": "u16"},
        {"name": "data", "type": "u8", "count": 2048},
        {"name": "data_event_time_ns", "type": "u64"},
    ]}}
    manifest = Manifest(duration_ns=FAULT_UNTIL_NS)
    manifest.add_schemas(schemas)
    outputs = {f"node{node}": f"bus.Node{node}"
               for node in range(1, active_nodes + 1)}
    channels = [f"bus.{node}" for node in outputs]
    for channel in channels:
        manifest.add_channel(channel, schema="can.Buffer")
    command = ["python", "-m", "sil.fmi"]
    for node in range(1, active_nodes + 1):
        fixture = "FaultAwareSender" if node < active_nodes else "FaultAwareReceiver"
        command += ["--instance", f"node{node}",
                    str(ARTIFACTS / f"{fixture}.fmu")]
    command += [
        "--instance", "bus", str(ARTIFACTS / f"{MODEL}.fmu"),
        "--bus-profile", "application/org.fmi-standard.fmi-ls-bus.can",
    ]
    for node in range(1, active_nodes + 1):
        command += ["--connect", f"node{node}.CanChannel=bus.Node{node}"]
    for node, terminal_name in outputs.items():
        command += ["--bind", f"bus.{node}:data={terminal_name}.Tx_Data"]
    if active_nodes != 2:
        command += ["--start", f"bus.activeNodeCount={active_nodes}"]
    for start in fault_start_values(rules, retry_limit=retry_limit):
        command += ["--start", start]
    manifest.add_process(
        "can",
        command=command,
        step_period_ns=1_000_000,
        publishes=channels,
    )
    path = manifest.write(ARTIFACTS / f"issue155-{name}.json").path
    return schemas, path


def sil_bus_trace(recording, schemas):
    from sil import schema
    from sil.recording import read_records

    codec = schema.load(schemas)["can.Buffer"]
    trace = []
    for channel, _, raw in read_records(recording):
        fields = codec.unpack(raw)
        payload = bytes(fields["data"][:fields["data_length"]])
        trace.append((
            fields["data_event_time_ns"],
            int(channel[-1]) - 1,
            payload,
        ))
    return sorted(trace)


def test_sil_fault_aware_external_node_receives_errors_and_recovers():
    scenarios = {}

    def qualify(name, rules, expected, *, retry_limit=1, active_nodes=2,
                repeat=False, expect_error_count=None):
        schemas, manifest = faulted_external_manifest(
            name, rules, retry_limit=retry_limit, active_nodes=active_nodes,
        )
        recording = ARTIFACTS / f"issue155-{name}.mcap"
        sil_run(manifest, recording)
        observed = sil_bus_trace(recording, schemas)
        assert observed == expected, name
        log = recording.with_suffix(".log").read_text()
        if expect_error_count is not None:
            assert log.count("Consumed CAN Bus Error") == expect_error_count
        else:
            assert "Consumed CAN Bus Error" not in log
        if repeat:
            repeated = ARTIFACTS / f"issue155-{name}-repeat.mcap"
            sil_run(manifest, repeated)
            assert repeated.read_bytes() == recording.read_bytes()
        scenarios[name] = {
            "expected_event_table": trace_rows(expected),
            "observed_trace": trace_rows(observed),
            "manifest": manifest.name,
            "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        }
        return log

    fault_log = qualify(
        "fault-recovery", [FAULT_RULE], ONE_ERROR_RECOVERY_TRACE,
        repeat=True, expect_error_count=2,
    )
    assert "ID 1 code 1 flag 1 sender 1 count 1" in fault_log
    assert "ID 1 code 1 flag 2 sender 0 count 1" in fault_log

    retry_error = {**FAULT_RULE, "attempt": 2}
    exhausted_expected = (
        bus_error_trace(FIRST_ERROR_END_NS)
        + bus_error_trace(RETRY_END_NS)
        + successful_frame_trace(LATER_REQUEST_NS)
    )
    qualify(
        "retry-exhaustion", [FAULT_RULE, retry_error], exhausted_expected,
        expect_error_count=4,
    )

    simultaneous_rule = {
        **FAULT_RULE,
        "sender_node": 2,
    }
    co_transmit_expected = (
        bus_error_trace(FIRST_ERROR_END_NS, primary_sender=1, active_nodes=3)
        + successful_frame_trace(
            RETRY_REQUEST_NS, senders=(0, 1), active_nodes=3,
        )
        + successful_frame_trace(
            LATER_REQUEST_NS, senders=(0, 1), active_nodes=3,
        )
    )
    simultaneous_log = qualify(
        "identical-co-transmit", [simultaneous_rule], co_transmit_expected,
        active_nodes=3, expect_error_count=3,
    )
    assert "ID 1 code 1 flag 1 sender 1 count 1" in simultaneous_log
    assert "ID 1 code 1 flag 2 sender 0 count 1" in simultaneous_log
    assert simultaneous_log.count("ID 1 code 1 flag 1 sender 1") == 1
    assert "ID 1 code 1 flag 2 sender 1" not in simultaneous_log

    suppression = {**FAULT_RULE, "kind": 2, "receiver_node": 2}
    suppression_expected = (
        successful_frame_trace(UPSTREAM_REQUEST_NS, suppressed_receiver=1)
        + successful_frame_trace(LATER_REQUEST_NS)
    )
    qualify(
        "receiver-suppression", [suppression], suppression_expected,
    )

    overlapping_error = {**FAULT_RULE}
    suppression_first = qualify(
        "overlap-suppression-first", [suppression, overlapping_error],
        suppression_expected,
    )
    assert "Consumed CAN Bus Error" not in suppression_first
    error_first = qualify(
        "overlap-error-first", [overlapping_error, suppression],
        ONE_ERROR_RECOVERY_TRACE, expect_error_count=2,
    )
    assert error_first.count("Consumed CAN Bus Error") == 2

    absent = {
        **FAULT_RULE,
        "request_start_ns": UPSTREAM_REQUEST_NS + 1,
        "request_end_ns": UPSTREAM_REQUEST_NS + 1,
    }
    qualify(
        "absent-match", [absent], BASELINE_FAULT_TRACE,
    )

    (ARTIFACTS / "issue155-traces.json").write_text(json.dumps({
        "scenarios": scenarios,
        "bus_error_encoding_source": (
            "FMI-LS-BUS 1.0.0, Network Abstraction, Table 14 (Bus Error), "
            "Table 15 (Error Code), Table 16 (Error Flag)"
        ),
        "notification_fixture_scope": (
            "first-party test consumer; verifies this profile's bytes and logs, "
            "not independent implementation compatibility"
        ),
    }, indent=2) + "\n")


BURST_GRIDS = {"coarse": BURST_END_NS, "boundary": 249000, "bit": 1000}


def burst_manifest(name, grid_ns):
    from sil.manifest import Manifest, SubscriberRoute

    schemas = {
        "can.Buffer": {
            "fields": [
                {"name": "data_length", "type": "u16"},
                {"name": "data", "type": "u8", "count": 2048},
                {"name": "data_event_time_ns", "type": "u64"},
            ]
        }
    }
    manifest = Manifest(duration_ns=BURST_END_NS)
    manifest.add_schemas(schemas)
    requests, observed = ["in.node1", "in.node2"], ["out.node1", "out.node2"]
    for channel in requests:
        # Latency 0 delivers each request in the Step its instant belongs to.
        manifest.add_channel(channel, schema="can.Buffer", latency_ns=0)
    for channel in observed:
        manifest.add_channel(channel, schema="can.Buffer")
    schedule = [[requests[node], t, data.hex()] for t, node, data in BURST]
    manifest.add_process(
        "source",
        command=[
            "python",
            str(ROOT / "models/can/tests/source.py"),
            json.dumps(schedule),
        ],
        step_period_ns=grid_ns,
        publishes=requests,
    )
    command = [
        "python",
        "-m",
        "sil.fmi",
        "--instance",
        "bus",
        str(ARTIFACTS / f"{MODEL}.fmu"),
    ]
    command += ["--bus-profile", "application/org.fmi-standard.fmi-ls-bus.can"]
    for node in (1, 2):
        command += ["--bind", f"in.node{node}:data=bus.Node{node}.Rx_Data"]
        command += ["--bind", f"out.node{node}:data=bus.Node{node}.Tx_Data"]
    manifest.add_process(
        "can",
        command=command,
        step_period_ns=grid_ns,
        subscribes=[SubscriberRoute(channel, capacity=8) for channel in requests],
        publishes=observed,
        # After the source in every Slot, so a request reaches its own Step.
        priority=1,
    )
    return schemas, manifest.write(ARTIFACTS / f"burst-{name}.json").path


def sil_run(manifest, recording):
    result = subprocess.run(
        [
            "/opt/kernel/sil-run",
            str(manifest),
            "--participant-timeout-ms",
            "5000",
            "-o",
            str(recording),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    recording.with_suffix(".log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr


def test_sil_burst_is_independent_of_step_grid():
    from sil import schema
    from sil.recording import read_records

    traces = {}
    for name, grid_ns in BURST_GRIDS.items():
        schemas, path = burst_manifest(name, grid_ns)
        recording = ARTIFACTS / f"burst-{name}.mcap"
        sil_run(path, recording)
        codec = schema.load(schemas)["can.Buffer"]
        trace = []
        for channel, published, raw in read_records(recording):
            if not channel.startswith("out."):
                continue
            fields = codec.unpack(raw)
            instant = fields["data_event_time_ns"]
            # Publication metadata: the Slot of the Step that held the event.
            assert published <= instant <= published + grid_ns
            node = int(channel[-1]) - 1
            trace.append(
                (instant, node, bytes(fields["data"][: fields["data_length"]]))
            )
        traces[name] = sorted(trace)
        assert traces[name] == BURST_TRACE, name
    (ARTIFACTS / "burst.json").write_text(
        json.dumps(
            {
                name: [(t, node, data.hex()) for t, node, data in trace]
                for name, trace in traces.items()
            },
            indent=2,
        )
        + "\n"
    )
    # The same Manifest again: byte-identical Recording.
    repeat = ARTIFACTS / "burst-bit-repeat.mcap"
    sil_run(ARTIFACTS / "burst-bit.json", repeat)
    assert repeat.read_bytes() == (ARTIFACTS / "burst-bit.mcap").read_bytes()


def test_sil_three_node_arbitration_and_manifest_determinism():
    from sil import schema
    from sil.manifest import Manifest, SubscriberRoute
    from sil.recording import read_records

    schemas = {"can.Buffer": {"fields": [
        {"name": "data_length", "type": "u16"},
        {"name": "data", "type": "u8", "count": 2048},
        {"name": "data_event_time_ns", "type": "u64"},
    ]}}
    codec = schema.load(schemas)["can.Buffer"]
    traces = {}
    for name, order in (("forward", (0, 1, 2)), ("reverse", (2, 1, 0))):
        manifest = Manifest(duration_ns=2_000_000)
        manifest.add_schemas(schemas)
        for node in order:
            manifest.add_channel(f"in.node{node + 1}", schema="can.Buffer", latency_ns=0)
            manifest.add_channel(f"out.node{node + 1}", schema="can.Buffer")
        schedule = [
            [f"in.node{node + 1}", instant, data.hex()]
            for instant, node, data in THREE_REQUESTS
        ]
        if name == "reverse":
            schedule.reverse()
        manifest.add_process(
            "source", command=["python", str(ROOT / "models/can/tests/source.py"),
                               json.dumps(schedule)],
            step_period_ns=1_000_000,
            publishes=[f"in.node{node + 1}" for node in order],
        )
        command = ["python", "-m", "sil.fmi", "--instance", "bus",
                   str(ARTIFACTS / f"{MODEL}.fmu"), "--bus-profile",
                   "application/org.fmi-standard.fmi-ls-bus.can",
                   "--start", "bus.activeNodeCount=3",
                   "--start", "bus.perNodeQueueCapacity=2"]
        for node in order:
            command += ["--bind", f"in.node{node + 1}:data=bus.Node{node + 1}.Rx_Data"]
            command += ["--bind", f"out.node{node + 1}:data=bus.Node{node + 1}.Tx_Data"]
        manifest.add_process(
            "can", command=command, step_period_ns=1_000_000,
            subscribes=[SubscriberRoute(f"in.node{node + 1}", capacity=8)
                        for node in order],
            publishes=[f"out.node{node + 1}" for node in order], priority=1,
        )
        path = manifest.write(ARTIFACTS / f"arbitration-{name}.json").path
        recordings = [ARTIFACTS / f"arbitration-{name}-{run}.mcap"
                      for run in ("first", "second")]
        for recording in recordings:
            sil_run(path, recording)
        assert recordings[0].read_bytes() == recordings[1].read_bytes()
        trace = []
        for channel, _, raw in read_records(recordings[0]):
            if not channel.startswith("out."):
                continue
            fields = codec.unpack(raw)
            trace.append((fields["data_event_time_ns"], int(channel[-1]) - 1,
                          bytes(fields["data"][:fields["data_length"]])))
        traces[name] = sorted(trace)
        assert traces[name] == THREE_TRACE
    (ARTIFACTS / "arbitration.json").write_text(json.dumps({
        name: [(t, node, payload.hex()) for t, node, payload in trace]
        for name, trace in traces.items()
    }, indent=2) + "\n")
    conflicting = json.loads(path.read_text())
    conflicting["participants"]["source"]["command"][2] = json.dumps([
        [f"in.node{node + 1}", 0, config(125000).hex()] for node in range(3)
    ] + [
        ["in.node1", 1000, frame(1, b"a").hex()],
        ["in.node2", 1000, frame(1, b"b").hex()],
    ])
    conflict_path = ARTIFACTS / "arbitration-conflict.json"
    conflict_path.write_text(json.dumps(conflicting) + "\n")
    failure = subprocess.run(
        ["/opt/kernel/sil-run", str(conflict_path), "--no-recording",
         "--provenance", str(ARTIFACTS / "arbitration-conflict.provenance.json")],
        capture_output=True, text=True, timeout=60,
    )
    (ARTIFACTS / "arbitration-conflict.log").write_text(
        failure.stdout + failure.stderr
    )
    assert failure.returncode == 1
    assert "fmi3UpdateDiscreteStates" in failure.stderr


def test_package_identity_metadata_and_linkage():
    import zipfile
    from lxml import etree
    import fmpy

    retained_identities = {}
    for name in (
        MODEL,
        "ExternalSender",
        "ExternalReceiver",
        "FaultAwareSender",
        "FaultAwareReceiver",
    ):
        path = ARTIFACTS / f"{name}.fmu"
        with zipfile.ZipFile(path) as archive:
            identity = json.loads(archive.read("resources/identity.json"))
            retained_identities[name] = identity
            if name == MODEL:
                assert len(identity["git_revision"]) == 40
                assert all(c in "0123456789abcdef" for c in identity["git_revision"])
                assert isinstance(identity["source_dirty"], bool)
            for source, expected in identity["sources"].items():
                assert (
                    hashlib.sha256(archive.read(f"sources/{source}")).hexdigest()
                    == expected
                )
            if name.startswith("FaultAware"):
                patch_path = ROOT / "models/can/qualification/fault-aware-node.patch"
                assert identity["fault_aware_bus_error"] is True
                assert identity["fault_node_patch_sha256"] == hashlib.sha256(
                    patch_path.read_bytes()
                ).hexdigest()
                assert archive.read(
                    "documentation/fault-aware-node.patch"
                ) == patch_path.read_bytes()
            else:
                assert "fault_aware_bus_error" not in identity
            manifest = etree.fromstring(
                archive.read("extra/org.fmi-standard.fmi-ls-bus/fmi-ls-manifest.xml")
            )

            class OfflineSchema(etree.Resolver):
                def resolve(self, url, _public_id, context):
                    if url.endswith("/fmi3LayeredStandardManifest.xsd"):
                        bundled = (
                            Path(fmpy.__file__).parent
                            / "schema/fmi3/fmi3LayeredStandardManifest.xsd"
                        )
                        return self.resolve_filename(str(bundled), context)
                    return None

            parser = etree.XMLParser(no_network=True)
            parser.resolvers.add(OfflineSchema())
            validator = etree.XMLSchema(
                etree.parse(
                    "/opt/spec/schema/fmi3LayeredStandardBusManifest.xsd", parser
                )
            )
            validator.assertValid(manifest)
            assert (
                manifest.get("{http://fmi-standard.org/fmi-ls-manifest}fmi-ls-version")
                == "1.0.0"
            )
            assert manifest.get("isBusSimulationFMU", "false") == (
                "true" if name == MODEL else "false"
            )
            assert (
                "documentation/licenses/LICENSE" in archive.namelist()
                or "documentation/licenses/LICENSE.txt" in archive.namelist()
            )
        read_model_description(path, validate=True)
    (ARTIFACTS / "issue155-identities.json").write_text(json.dumps({
        "fmu_sha256": {
            name: hashlib.sha256((ARTIFACTS / f"{name}.fmu").read_bytes()).hexdigest()
            for name in retained_identities
        },
        "identities": retained_identities,
    }, indent=2, sort_keys=True) + "\n")
    with initialized_fmu() as bus:
        library = Path(bus.unzipDirectory) / f"binaries/x86_64-linux/{MODEL}.so"
        symbols = subprocess.check_output(["nm", "-D", str(library)], text=True)
        assert "fmi3InstantiateCoSimulation" in symbols
        for forbidden in ("clock_gettime", "gettimeofday", " rand", "sil_"):
            assert forbidden not in symbols


def test_unsupported_capability_is_explicit():
    with initialized_fmu() as bus:
        with pytest.raises(FMICallException):
            bus.getFMUState()


@pytest.mark.parametrize("initial", [b"", CONFIG, FRAME])
def test_initial_binary_assignments_are_not_clock_activations(initial):
    with initialized_fmu(exit_initialization=False) as bus:
        bus.setBinary([0, 4], [initial, b""])
        bus.exitInitializationMode()
        bus.updateDiscreteStates()
        assert intervals(bus)[2] == [0, 0]
        advance(bus, 0, 0.1)
        transmit_configured(bus, FRAME)
        bus.updateDiscreteStates()
        assert intervals(bus)[2] == [2, 2]
        advance(bus, 0.1, 0.10083)
        bus.setClock([3, 7], [True, True])
        assert bus.getBinary([1, 5]) == [CONFIRM, FRAME]


def test_initial_unknowns_are_readable_before_any_clock_activation():
    description = read_model_description(ARTIFACTS / f"{MODEL}.fmu")
    references = [
        unknown.variable.valueReference for unknown in description.initialUnknowns
    ]
    assert references == [1, 5, 9, 13]
    with initialized_fmu(exit_initialization=False) as bus:
        assert [value or b"" for value in bus.getBinary(references)] == [b""] * 4
        bus.setBinary([0], [FRAME])
        bus.setBinary([0], [b""])
        assert [value or b"" for value in bus.getBinary(references)] == [b""] * 4
        bus.exitInitializationMode()
        bus.updateDiscreteStates()
        assert intervals(bus)[2] == [0, 0]
        bus.enterStepMode()


def test_upstream_revision_guard_survives_optimized_python(tmp_path):
    result = subprocess.run(
        [
            "python",
            "-O",
            str(ROOT / "models/can/qualification/build_nodes.py"),
            "/opt/spec",
            "/opt/examples",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "expected revision" in result.stderr
    assert "got" in result.stderr
    assert not list(tmp_path.glob("*.fmu"))


def test_upstream_mime_guard_survives_optimized_python(tmp_path):
    checkout = tmp_path / "examples"
    shutil.copytree("/opt/examples", checkout)
    description = (
        checkout / "can-node-triggered-output/description/modelDescription.xml"
    )
    description.write_text(description.read_text().replace("1.0.0", "0.0.0"))
    result = subprocess.run(
        [
            "python",
            "-O",
            str(ROOT / "models/can/qualification/build_nodes.py"),
            str(checkout),
            "/opt/spec",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "Binary variables must declare" in result.stderr
    assert not list(tmp_path.glob("*.fmu"))
