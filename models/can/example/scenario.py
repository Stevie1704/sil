"""Compile an example configuration into bus start values and node requests.

Standard library only, so the SiL path and the independent FMI path read one
configuration without sharing an execution engine. Bitrate and arbitration
policy are what each node asks for with FMI-LS-BUS Configuration operations;
node count, queue capacity and the fault schedule are the FMU's fixed
parameters. The terminal count and Binary maxSize are fixed when the archive
is packaged and are read from its modelDescription.xml.
"""

import json
import struct
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ARBITRATION_LOSS = {"BufferAndRetransmit": 1, "DiscardAndNotify": 2}
FAULT_KINDS = {"ScheduledTransmissionError": 1, "ReceiverDeliverySuppression": 2}
FAULT_FIELDS = (
    ("sender_node", "SenderNode"),
    ("receiver_node", "ReceiverNode"),
    ("identifier", "Identifier"),
    ("request_start_ns", "RequestStartNs"),
    ("request_end_ns", "RequestEndNs"),
    ("occurrence", "Occurrence"),
    ("attempt", "Attempt"),
)
CLASSICAL_DATA_BYTES = 8
# FMI-LS-BUS 1.0.0 CAN operations: OP code, Length, and Configuration kind.
TRANSMIT = 0x10
CONFIGURATION, BITRATE, ARBITRATION_LOST_BEHAVIOR = 0x40, 1, 4


def load(path):
    return json.loads(Path(path).read_text())


def packaged_limits(archive):
    """Terminal count and Binary maxSize fixed when the FMU was packaged."""
    with zipfile.ZipFile(archive) as fmu:
        description = ET.fromstring(fmu.read("modelDescription.xml"))
    receivers = [
        variable for variable in description.iter("Binary")
        if variable.get("name", "").endswith(".Rx_Data")
    ]
    return {
        "terminals": len(receivers),
        "max_binary_size": min(int(v.get("maxSize")) for v in receivers),
    }


def start_values(config):
    """The `--start` assignments that carry the configuration into the Manifest."""
    starts = [
        f"bus.activeNodeCount={len(config['nodes'])}",
        f"bus.perNodeQueueCapacity={config['per_node_queue_capacity']}",
        f"bus.faultRetryLimit={config['fault_retry_limit']}",
        f"bus.faultRuleCount={len(config['faults'])}",
    ]
    for number, fault in enumerate(config["faults"], start=1):
        starts.append(f"bus.faultRule{number}Kind={fault_kind(fault)}")
        starts += [
            f"bus.faultRule{number}{name}={fault[field]}"
            for field, name in FAULT_FIELDS
        ]
    return starts


def requests(config, limits):
    """Validated (instant_ns, node_index, buffer) inputs, one per node and instant."""
    validate(config, limits)
    buffers = {}
    for node, settings in enumerate(config["nodes"]):
        buffers[(0, node)] = (
            struct.pack("<IIBI", CONFIGURATION, 13, BITRATE, config["bitrate"])
            + struct.pack("<IIBB", CONFIGURATION, 10, ARBITRATION_LOST_BEHAVIOR,
                          ARBITRATION_LOSS[settings["arbitration_loss"]])
        )
    for frame in config["frames"]:
        key = (frame["at_ns"], frame["node"] - 1)
        buffers[key] = buffers.get(key, b"") + transmit(frame)
    for (instant, node), buffer in buffers.items():
        if len(buffer) > limits["max_binary_size"]:
            raise ValueError(
                f"node {node + 1} at {instant} ns needs {len(buffer)} bytes; the "
                f"archive was packaged with {limits['max_binary_size']}"
            )
    return [(instant, node, buffers[instant, node]) for instant, node in sorted(buffers)]


def transmit(frame):
    data = bytes.fromhex(frame["data"])
    return struct.pack(
        "<IIIBBH", TRANSMIT, 16 + len(data), frame["identifier"], 0, 0, len(data)
    ) + data


def fault_kind(fault):
    if fault.get("kind") not in FAULT_KINDS:
        raise ValueError(f"fault kind must be one of {sorted(FAULT_KINDS)}")
    return FAULT_KINDS[fault["kind"]]


def validate(config, limits):
    """Reject what only the example knows; the FMU validates its own ranges."""
    nodes = config["nodes"]
    if not nodes:
        raise ValueError("the example needs at least one node")
    if len(nodes) > limits["terminals"]:
        raise ValueError(
            f"{len(nodes)} nodes exceed the {limits['terminals']} packaged terminals"
        )
    for settings in nodes:
        if settings.get("arbitration_loss") not in ARBITRATION_LOSS:
            raise ValueError(
                f"arbitration_loss must be one of {sorted(ARBITRATION_LOSS)}"
            )
    for frame in config["frames"]:
        if not 1 <= frame["node"] <= len(nodes):
            raise ValueError(f"frame from node {frame['node']} is not an active node")
        if len(bytes.fromhex(frame["data"])) > CLASSICAL_DATA_BYTES:
            raise ValueError("a Classical CAN frame carries at most 8 data bytes")
        if not 0 <= frame["at_ns"] < config["duration_ns"]:
            raise ValueError("every frame must be requested within the duration")
    for fault in config["faults"]:
        fault_kind(fault)
