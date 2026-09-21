"""The Manifests that drive the pinned CAN node through a clocked Importer.

One per step grid of [`expected.json`](expected.json), so the two Runs differ
in exactly what the expected exchange says they differ in: the Step period.
The node's transmit interval is 300 ms, and the grids are the fixture's own —
one that divides it and one that does not.

Each Manifest declares the Importer as an ordinary process participant, bound
to the node's clocked `Tx_Data`, publishing one bounded CAN frame Channel, and
an observer reading it. The Channel carries the activation and the FMI event
time it belongs to:

    {"name": "data_length",        "type": "u16"}
    {"name": "data",               "type": "u8", "count": 2048}
    {"name": "data_event_time_ns", "type": "u64"}

    python3 clocked_manifest.py <fmu> <output-directory>

Unlike `manifest.py`, which measures the release, this one is built with the
`sil` package of the checkout under measurement — see `Dockerfile.measured`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from sil.manifest import Manifest, SubscriberRoute

EXPECTED = Path(__file__).resolve().parent / "expected.json"

# `CanChannel.Tx_Data` declares maxSize 2048, so a bounded Channel that can
# carry any value the node is allowed to produce carries 2048 payload bytes.
CAN_BUFFER_BYTES = 2048

CAN_CHANNEL = "can.Tx"
BUFFER_SCHEMA = "can.Buffer"
TX_DATA = "CanChannel.Tx_Data"

SCHEMAS = {
    BUFFER_SCHEMA: {
        "fields": [
            {"name": "data_length", "type": "u16"},
            {"name": "data", "type": "u8", "count": CAN_BUFFER_BYTES},
            {"name": "data_event_time_ns", "type": "u64"},
        ]
    }
}

# A Slot carries one activation from the Step that ended in it, the Latency's
# own, and — in the first Slot — the event that ended initialization beside
# the first Step's. Four is above every burst these grids produce.
ROUTE_CAPACITY = 4


def clocked(fmu: Path, case: dict) -> Manifest:
    """One case of the expected exchange, as a Manifest."""
    manifest = Manifest(duration_ns=case["duration_ns"])
    manifest.add_schemas(SCHEMAS)
    manifest.add_channel(CAN_CHANNEL, schema=BUFFER_SCHEMA)
    manifest.add_process(
        "importer",
        command=[
            "python3", "-m", "sil.fmi", str(fmu),
            "--bind", f"{CAN_CHANNEL}:data={TX_DATA}",
        ],
        step_period_ns=case["step_size_ns"],
        publishes=[CAN_CHANNEL],
    )
    manifest.add_process(
        "observer",
        command=["python3", "/opt/measured/clocked_observer.py"],
        step_period_ns=case["step_size_ns"],
        subscribes=[SubscriberRoute(CAN_CHANNEL, capacity=ROUTE_CAPACITY)],
        priority=1,
    )
    return manifest


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        raise SystemExit("usage: clocked_manifest.py <fmu> <output-directory>")
    fmu, destination = Path(argv[0]), Path(argv[1])
    for case in json.loads(EXPECTED.read_text())["cases"]:
        written = clocked(fmu, case).write(
            destination / f"clocked-{case['name']}.json"
        )
        print(f"{case['name']:<16} {written.hash}  {written.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
