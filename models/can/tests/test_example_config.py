"""The documented example configuration compiles without SiL, FMPy or the FMU."""

import json
import struct
import sys
from pathlib import Path

import pytest

EXAMPLE = Path(__file__).resolve().parents[1] / "example"
sys.path.insert(0, str(EXAMPLE))
import scenario  # noqa: E402

LIMITS = {"terminals": 4, "max_binary_size": 2048}


def config(**changes):
    base = {
        "duration_ns": 1_000_000,
        "step_period_ns": 250_000,
        "bitrate": 500_000,
        "per_node_queue_capacity": 2,
        "fault_retry_limit": 1,
        "nodes": [
            {"arbitration_loss": "BufferAndRetransmit"},
            {"arbitration_loss": "DiscardAndNotify"},
        ],
        "frames": [{"node": 1, "at_ns": 1000, "identifier": 0x123, "data": "0102"}],
        "faults": [],
    }
    base.update(changes)
    return base


def bitrate_operation(rate):
    return struct.pack("<IIBI", 0x40, 13, 1, rate)


def policy_operation(policy):
    return struct.pack("<IIBB", 0x40, 10, 4, policy)


def test_nodes_are_configured_at_zero_and_frames_follow_in_node_order():
    requests = scenario.requests(config(), LIMITS)
    assert requests == [
        (0, 0, bitrate_operation(500_000) + policy_operation(1)),
        (0, 1, bitrate_operation(500_000) + policy_operation(2)),
        (1000, 0, struct.pack("<IIIBBH", 0x10, 18, 0x123, 0, 0, 2) + b"\x01\x02"),
    ]


def test_operations_of_one_node_and_instant_share_one_buffer():
    frames = [
        {"node": 1, "at_ns": 0, "identifier": 1, "data": ""},
        {"node": 1, "at_ns": 0, "identifier": 2, "data": "ff"},
    ]
    (instant, node, buffer), second = scenario.requests(config(frames=frames), LIMITS)
    assert (instant, node) == (0, 0)
    assert buffer == (
        bitrate_operation(500_000) + policy_operation(1)
        + struct.pack("<IIIBBH", 0x10, 16, 1, 0, 0, 0)
        + struct.pack("<IIIBBH", 0x10, 17, 2, 0, 0, 1) + b"\xff"
    )
    assert second[:2] == (0, 1)


def test_start_values_select_nodes_capacity_and_fault_schedule():
    fault = {
        "kind": "ScheduledTransmissionError", "sender_node": 1,
        "receiver_node": 0, "identifier": 0x123, "request_start_ns": 1000,
        "request_end_ns": 1000, "occurrence": 1, "attempt": 1,
    }
    assert scenario.start_values(config(faults=[fault])) == [
        "bus.activeNodeCount=2",
        "bus.perNodeQueueCapacity=2",
        "bus.faultRetryLimit=1",
        "bus.faultRuleCount=1",
        "bus.faultRule1Kind=1",
        "bus.faultRule1SenderNode=1",
        "bus.faultRule1ReceiverNode=0",
        "bus.faultRule1Identifier=291",
        "bus.faultRule1RequestStartNs=1000",
        "bus.faultRule1RequestEndNs=1000",
        "bus.faultRule1Occurrence=1",
        "bus.faultRule1Attempt=1",
    ]


def test_suppression_is_named_as_its_own_fault_kind():
    fault = {
        "kind": "ReceiverDeliverySuppression", "sender_node": 1,
        "receiver_node": 2, "identifier": 1, "request_start_ns": 0,
        "request_end_ns": 10, "occurrence": 1, "attempt": 1,
    }
    assert "bus.faultRule1Kind=2" in scenario.start_values(config(faults=[fault]))


def test_packaged_limits_are_read_from_the_archive(tmp_path):
    import zipfile

    description = (
        '<fmiModelDescription fmiVersion="3.0"><ModelVariables>'
        '<Binary name="Node1.Rx_Data" valueReference="0" maxSize="2048"/>'
        '<Binary name="Node1.Tx_Data" valueReference="1" maxSize="2048"/>'
        '<Binary name="Node2.Rx_Data" valueReference="4" maxSize="2048"/>'
        '<Binary name="Node2.Tx_Data" valueReference="5" maxSize="2048"/>'
        "</ModelVariables></fmiModelDescription>"
    )
    archive = tmp_path / "bus.fmu"
    with zipfile.ZipFile(archive, "w") as fmu:
        fmu.writestr("modelDescription.xml", description)
    assert scenario.packaged_limits(archive) == {
        "terminals": 2, "max_binary_size": 2048,
    }


@pytest.mark.parametrize("changes, message", [
    ({"nodes": [{"arbitration_loss": "BufferAndRetransmit"}] * 5}, "terminals"),
    ({"nodes": []}, "at least one node"),
    ({"nodes": [{"arbitration_loss": "Keep"}]}, "arbitration_loss"),
    ({"frames": [{"node": 3, "at_ns": 0, "identifier": 1, "data": ""}]}, "node 3"),
    ({"frames": [{"node": 1, "at_ns": 0, "identifier": 1, "data": "00" * 9}]},
     "8 data bytes"),
    ({"frames": [{"node": 1, "at_ns": 5, "identifier": 1, "data": "00" * 8}] * 90},
     "2048"),
    ({"frames": [{"node": 1, "at_ns": 2_000_000, "identifier": 1, "data": ""}]},
     "duration"),
    ({"faults": [{"kind": "BitFlip"}]}, "kind"),
])
def test_invalid_example_is_rejected_before_any_run(changes, message):
    with pytest.raises(ValueError, match=message):
        scenario.requests(config(**changes), LIMITS)


def test_shipped_example_compiles():
    example = json.loads((EXAMPLE / "example.json").read_text())
    requests = scenario.requests(example, LIMITS)
    assert len({node for _, node, _ in requests}) == len(example["nodes"])
    assert scenario.start_values(example)[0] == (
        f"bus.activeNodeCount={len(example['nodes'])}"
    )
