"""The Manifests that replace one live CAN node with its own Recording.

[`connected_manifest.py`](connected_manifest.py) declares the live composition:
two instances of the pinned node attached to the two terminals of the pinned
bus simulation FMU, all three driven by one process participant. These
Manifests declare the same composition with **`node1` removed** and the
Messages it published handed back by a Replay participant reading the live
Run's Recording.

What changes, and nothing else does:

- `node1` is no longer an instance, and no longer the Publisher of
  `can.node1.Tx`. The Replay participant is.
- `bus.Node1` is connected to no peer and fed by `can.node1.Tx` instead, which
  is the replay-input boundary
  [ADR 0001](../../docs/adr/0001-connected-fmus-in-one-process-participant.md)
  established and [ADR 0002](../../docs/adr/0002-a-replayed-terminal-lands-on-its-own-instant.md)
  settled.
- `can.node1.Tx` declares `latency_ns` 0.

The receiving node, the bus simulation FMU, the bus error probability, the
Channels, the schema, the route capacities and the step grid are the live
Manifest's. That is what makes the comparison a statement about the replayed
source rather than about a second composition.

**Why the Latency is zero.** An activation the group observes during a Step is
published in the Slot that Step began in, so the instant it states lies inside
that Step. A Message delivered in the Slot it was published in therefore
arrives in the Step its own instant belongs to, and the Importer can stop
there. Under the default next-activation delivery every Message would arrive
one Step after the instant it names, and the Importer refuses that rather than
raising the activation at an instant the FMUs cannot be taken back to.

Two more Manifests are built for the aligned grid, and both are meant to fail.
They are declared faults rather than edited artifacts: an Interceptor is part
of the hashed Manifest, so a Run that proves the check can fail is as
reproducible as the Run that passes.

    python3 replay_manifest.py <node.fmu> <bus.fmu> <live-recording-directory> \\
                               <output-directory>

Like `connected_manifest.py`, this is built with the `sil` package of the
checkout under measurement — see `Dockerfile.measured`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from sil.manifest import Manifest, SubscriberRoute

sys.path.insert(0, str(Path(__file__).resolve().parent))

from connected_manifest import (  # noqa: E402
    BUFFER_SCHEMA,
    CAN_PROFILE,
    EXPECTED,
    ROUTE_CAPACITY,
    SCHEMAS,
    SOURCES,
)

# The Channel the removed node published, and the terminal its Messages are
# handed to now. One name for the boundary, used by the Manifests and by the
# equivalence check that reads their Recordings.
BOUNDARY = "can.node1.Tx"
BOUNDARY_TERMINAL = "bus.Node1"

# Everything the replay did not replace. These are the streams the proof
# compares: the receiving node's own transmissions, and both of the bus's.
RETAINED = {
    channel: source for channel, (source, _) in SOURCES.items()
    if channel != BOUNDARY
}

INSTANCES = {"node2": "node", "bus": "bus"}
CONNECTION = "node2.CanChannel=bus.Node2"

# The Slot the aligned grid publishes the first `CanTransmit` of the boundary
# in: the node transmits at 300 ms, and the Step from 200 ms to 300 ms is the
# one that observed it. Both failing variants act on that one Message, so what
# they alter is a frame the receiver's behavior depends on rather than the
# configuration every later operation needs.
FAULT_WINDOW = (200_000_000, 300_000_000)

# The instant the retimed variant moves that frame to. Inside the Step it
# arrives in, so the Importer accepts it and the Run completes: what this
# variant proves is that the *check* fails, not that the Importer refuses.
RETIMED_NS = 250_000_000

# Every variant of one grid, and what it declares beyond the faultless one.
FAULTS = {
    "dropped": {"kind": "drop"},
    "retimed": {
        "kind": "override",
        "field": "data_event_time_ns",
        "value": RETIMED_NS,
    },
}
# The grid the failing variants are built for. One is enough: what they measure
# is the equivalence check, and the check does not know which grid it reads.
FAULTED_CASE = "aligned"


def replay(node: Path, bus: Path, recording: Path, case: dict,
           fault: dict | None = None) -> Manifest:
    """One case of the live exchange, with its first source replayed."""
    archives = {"node": node, "bus": bus}
    manifest = Manifest(duration_ns=case["duration_ns"])
    manifest.add_schemas(SCHEMAS)
    manifest.add_channel(BOUNDARY, schema=BUFFER_SCHEMA, latency_ns=0)
    for channel in RETAINED:
        manifest.add_channel(channel, schema=BUFFER_SCHEMA)
    if fault is not None:
        start_ns, end_ns = FAULT_WINDOW
        manifest.add_interceptor(
            BOUNDARY, start_ns=start_ns, end_ns=end_ns, **fault
        )
    # The stimulus is identified by the content hash of the Recording, which
    # the Manifest hash covers: what this Run replays is attributable to the
    # Run that produced it without a second record of it.
    manifest.add_replay("node1", recording=recording, channels=[BOUNDARY])
    manifest.add_process(
        "importer",
        command=[
            "python3", "-m", "sil.fmi",
            *(argument
              for name, archive in INSTANCES.items()
              for argument in ("--instance", name, str(archives[archive]))),
            "--bus-profile", CAN_PROFILE,
            "--connect", CONNECTION,
            # The boundary: an in-direction Channel feeding the terminal the
            # removed node was connected to.
            "--bind", f"{BOUNDARY}:data={BOUNDARY_TERMINAL}.Rx_Data",
            *(argument
              for channel, (_, variable) in SOURCES.items()
              if channel != BOUNDARY
              for argument in ("--bind", f"{channel}:data={variable}")),
            "--start", "bus.BusErrorProbability=0.0",
        ],
        step_period_ns=case["step_size_ns"],
        subscribes=[SubscriberRoute(BOUNDARY, capacity=ROUTE_CAPACITY)],
        publishes=list(RETAINED),
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


def variants(node: Path, bus: Path, recordings: Path):
    """Every Manifest this proof runs, in the order it runs them."""
    for case in json.loads(EXPECTED.read_text())["cases"]:
        recording = recordings / f"connected-{case['name']}.mcap"
        yield f"replay-{case['name']}", replay(node, bus, recording, case)
        if case["name"] != FAULTED_CASE:
            continue
        for name, fault in FAULTS.items():
            yield (
                f"replay-{case['name']}-{name}",
                replay(node, bus, recording, case, fault),
            )


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        raise SystemExit(
            "usage: replay_manifest.py <node.fmu> <bus.fmu> "
            "<live-recording-directory> <output-directory>"
        )
    node, bus, recordings, destination = (Path(argument) for argument in argv)
    for name, manifest in variants(node, bus, recordings):
        written = manifest.write(destination / f"{name}.json")
        print(f"{name:<24} {written.hash}  {written.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
