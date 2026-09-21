"""The two Manifests that put the pinned CAN node in front of current SiL.

Both declare the released FMI Importer (`python3 -m sil.fmi`) as an ordinary
process participant driving the fixture's CAN node, and they differ in exactly
one thing — whether a Channel asks the importer for the node's bus data:

- `no-channels` asks for nothing, so the Run reaches the FMU itself. It is
  what shows the node refusing an importer that does not use Event Mode.
- `binary-channel` declares a bounded CAN frame Channel whose schema field
  names are the node's Binary variables, which is the mapping the importer's
  own contract defines. It is what shows those variables are invisible to it.

The Channel's schema is the bounded representation the kernel already offers:
a `u16` length beside a fixed `u8` array, sized to the `maxSize` the node's
Binary variables declare. It is declared here to establish which side of the
boundary the gap is on — the kernel accepts this Channel, and the importer is
what cannot fill it.

    python3 manifest.py <fmu> <output-directory>

It is built with the `sil` package the published runner image ships. Nothing
here imports from a SiL checkout.
"""

from __future__ import annotations

import sys
from pathlib import Path

from sil.manifest import Manifest, SubscriberRoute

# The node transmits every 300 ms; a 100 ms Step divides that interval, so a
# Run that could observe the node at all would observe it on this grid.
STEP_PERIOD_NS = 100_000_000
DURATION_NS = 1_000_000_000

# `CanChannel.Tx_Data` declares maxSize 2048, so a bounded Channel that can
# carry any value the node is allowed to produce carries 2048 payload bytes
# and the length actually used.
CAN_BUFFER_BYTES = 2048

CAN_CHANNEL = "can.Tx"

# The schema field names are the FMU's variable names: that is the importer's
# declared mapping, with no configuration of its own.
SCHEMAS = {
    "can.Buffer": {
        "fields": [
            {"name": "CanChannel.Tx_Data_length", "type": "u16"},
            {"name": "CanChannel.Tx_Data", "type": "u8",
             "count": CAN_BUFFER_BYTES},
        ]
    }
}

# One Message per Step on a route drained every Step, plus the one the
# Channel's default Latency holds.
ROUTE_CAPACITY = 2


def importer_command(fmu: Path) -> list[str]:
    """The released FMI Importer, driving one FMU archive."""
    return ["python3", "-m", "sil.fmi", str(fmu)]


def no_channels(fmu: Path) -> Manifest:
    """One participant, no Channels: the Run reaches the FMU itself."""
    manifest = Manifest(duration_ns=DURATION_NS)
    manifest.add_process(
        "importer", command=importer_command(fmu),
        step_period_ns=STEP_PERIOD_NS,
    )
    return manifest


def binary_channel(fmu: Path) -> Manifest:
    """One participant publishing the node's bus data on a bounded Channel."""
    manifest = Manifest(duration_ns=DURATION_NS)
    manifest.add_schemas(SCHEMAS)
    manifest.add_channel(CAN_CHANNEL, schema="can.Buffer")
    manifest.add_process(
        "importer",
        command=importer_command(fmu),
        step_period_ns=STEP_PERIOD_NS,
        publishes=[CAN_CHANNEL],
    )
    # A published Channel with no subscriber would leave the Manifest
    # describing a Run with no reader; the observer is what a consumer would
    # have written to see the frames.
    manifest.add_process(
        "observer",
        command=["python3", "-m", "sil.participant",
                 "/opt/consumer/observer.py:Observer"],
        step_period_ns=STEP_PERIOD_NS,
        subscribes=[SubscriberRoute(CAN_CHANNEL, capacity=ROUTE_CAPACITY)],
    )
    return manifest


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        raise SystemExit("usage: manifest.py <fmu> <output-directory>")
    fmu, destination = Path(argv[0]), Path(argv[1])
    for name, build in (("no-channels", no_channels),
                        ("binary-channel", binary_channel)):
        written = build(fmu).write(destination / f"{name}.json")
        print(f"{name:<16} {written.hash}  {written.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
