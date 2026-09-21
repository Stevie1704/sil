"""The Manifests that drive two pinned CAN nodes through the pinned bus FMU.

One per step grid of [`connected_expected.json`](connected_expected.json), so
the two Runs differ in exactly what the expected exchange says they differ in:
the Step period.

Each Manifest declares **one** process participant for all three FMUs — two
instances of the node attached to the two terminals of the bus simulation FMU —
and four bounded CAN frame Channels, one per terminal whose activations the Run
observes. The group is one participant because the coordination it does cannot
be expressed between participants: the bus states when it has transmitted a
frame, 480 us after it was offered, and that instant is not a Slot of any step
period a Manifest here declares. See
[`docs/adr/0001-connected-fmus-in-one-process-participant.md`](../../docs/adr/0001-connected-fmus-in-one-process-participant.md).

Each Channel carries the activation and the FMI event time it belongs to:

    {"name": "data_length",        "type": "u16"}
    {"name": "data",               "type": "u8", "count": 2048}
    {"name": "data_event_time_ns", "type": "u64"}

    python3 connected_manifest.py <node.fmu> <bus.fmu> <output-directory>

Like `clocked_manifest.py`, this one is built with the `sil` package of the
checkout under measurement — see `Dockerfile.measured`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from sil.manifest import Manifest, SubscriberRoute

EXPECTED = Path(__file__).resolve().parent / "connected_expected.json"

# Every Binary variable of both FMUs declares maxSize 2048, so a bounded
# Channel that can carry any value either is allowed to produce carries 2048
# payload bytes.
CAN_BUFFER_BYTES = 2048

BUFFER_SCHEMA = "can.Buffer"
SCHEMAS = {
    BUFFER_SCHEMA: {
        "fields": [
            {"name": "data_length", "type": "u16"},
            {"name": "data", "type": "u8", "count": CAN_BUFFER_BYTES},
            {"name": "data_event_time_ns", "type": "u64"},
        ]
    }
}

# The media type both FMUs' bus buffers declare, without its parameters. The
# artifacts' own `version=` is `1.0.0-beta.1` while the operation bytes are
# v1.0.0's, which README.md records; a profile keyed on the media type accepts
# them, and both ends are still required to declare the identical mimeType.
CAN_PROFILE = "application/org.fmi-standard.fmi-ls-bus.can"

INSTANCES = {"node1": "node", "node2": "node", "bus": "bus"}
CONNECTIONS = ["node1.CanChannel=bus.Node1", "node2.CanChannel=bus.Node2"]

# One Channel per observed terminal, named after the terminal it observes, so
# the Recording is read in the expected exchange's own vocabulary.
SOURCES = {
    "can.node1.Tx": ("node1.CanChannel", "node1.CanChannel.Tx_Data"),
    "can.node2.Tx": ("node2.CanChannel", "node2.CanChannel.Tx_Data"),
    "can.bus.Node1": ("bus.Node1", "bus.Node1.Tx_Data"),
    "can.bus.Node2": ("bus.Node2", "bus.Node2.Tx_Data"),
}

# A Slot carries what the group observed during one Step: two transmissions of
# the bus, one transmit of a node, and — in the first Slot — the event that
# ended initialization. Four is above every burst these grids produce on one
# Channel.
ROUTE_CAPACITY = 4


def connected(node: Path, bus: Path, case: dict) -> Manifest:
    """One case of the expected exchange, as a Manifest."""
    archives = {"node": node, "bus": bus}
    manifest = Manifest(duration_ns=case["duration_ns"])
    manifest.add_schemas(SCHEMAS)
    for channel in SOURCES:
        manifest.add_channel(channel, schema=BUFFER_SCHEMA)
    manifest.add_process(
        "importer",
        command=[
            "python3", "-m", "sil.fmi",
            *(argument
              for name, archive in INSTANCES.items()
              for argument in ("--instance", f"{name}={archives[archive]}")),
            "--bus-profile", CAN_PROFILE,
            *(argument for connection in CONNECTIONS
              for argument in ("--connect", connection)),
            *(argument
              for channel, (_, variable) in SOURCES.items()
              for argument in ("--bind", f"{channel}:data={variable}")),
            # The one scalar either FMU exposes beside the independent
            # variable. Upstream draws a simulated bus error from `rand()`, so
            # a Run that left this to a default would be a Run whose result
            # depends on a pseudo-random sequence.
            "--start", "bus.BusErrorProbability=0.0",
        ],
        step_period_ns=case["step_size_ns"],
        publishes=list(SOURCES),
    )
    manifest.add_process(
        "observer",
        command=["python3", "/opt/measured/clocked_observer.py"],
        step_period_ns=case["step_size_ns"],
        subscribes=[
            SubscriberRoute(channel, capacity=ROUTE_CAPACITY)
            for channel in SOURCES
        ],
        priority=1,
    )
    return manifest


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        raise SystemExit(
            "usage: connected_manifest.py <node.fmu> <bus.fmu> "
            "<output-directory>"
        )
    node, bus, destination = Path(argv[0]), Path(argv[1]), Path(argv[2])
    for case in json.loads(EXPECTED.read_text())["cases"]:
        written = connected(node, bus, case).write(
            destination / f"connected-{case['name']}.json"
        )
        print(f"{case['name']:<16} {written.hash}  {written.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
