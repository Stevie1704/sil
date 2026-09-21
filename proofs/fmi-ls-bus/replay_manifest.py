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
Each declares an **Interceptor** on the boundary Channel rather than carrying
an edited artifact: an Interceptor is part of the hashed Manifest, so a Run
that proves the check can fail is as reproducible as the Run that passes.

A third grid is added here, coarser than either of the fixture's own, and it
brings its **own live composition** with it — which is why this script has two
stages:

    python3 replay_manifest.py live   <node.fmu> <bus.fmu> <output-directory>
    python3 replay_manifest.py replay <node.fmu> <bus.fmu> \\
                                      <live-recording-directory> <output-directory>

The live stage has to run, and its Run has to produce a Recording, before the
replay stage can hash it into a Manifest.

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
    connected,
)

# The Channel the removed node published, and the terminal its Messages are
# handed to now. One name for the boundary, used by the Manifests and by the
# equivalence check that reads their Recordings.
BOUNDARY = "can.node1.Tx"
BOUNDARY_TERMINAL = "bus.Node1"

# Every Channel the replay did not replace. These are the Channels the proof
# compares: the receiving node's own transmissions, and both of the bus's.
RETAINED = {
    channel: source for channel, (source, _) in SOURCES.items()
    if channel != BOUNDARY
}

INSTANCES = {"node2": "node", "bus": "bus"}
CONNECTION = "node2.CanChannel=bus.Node2"

# The Slot the aligned grid publishes the first `CanTransmit` of the boundary
# in: the node transmits at 300 ms, and the Step from 200 ms to 300 ms is the
# one that observed it. Both Interceptors below act on that one Message, so
# what they alter is a frame the receiver's behavior depends on rather than the
# configuration every later operation needs.
INTERCEPTOR_WINDOW = (200_000_000, 300_000_000)

# The instant the retimed Interceptor moves that frame to. Inside the Step it
# arrives in, so the Importer accepts it and the Run completes: what this
# variant proves is that the *check* fails, not that the Importer refuses.
RETIMED_NS = 250_000_000

# The Interceptor each failing variant declares. The variants that are
# meant to pass declare none.
INTERCEPTORS = {
    "dropped": {"kind": "drop"},
    "retimed": {
        "kind": "override",
        "field": "data_event_time_ns",
        "value": RETIMED_NS,
    },
}
# The grid the failing variants are built for. One is enough: what they measure
# is the equivalence check, and the check does not know which grid it reads.
INTERCEPTED_CASE = "aligned"

# A Step grid coarser than the node's own 300 ms transmit period, which
# neither grid of the expected exchange is. Two things only this one reaches:
#
#   - one activation carries several CAN operations, because the node
#     accumulates into its transmit buffer between communication points, and
#     one Slot carries several activations of the boundary Channel;
#   - both nodes then offer four frames of one CAN ID at one instant, and the
#     bus transmits them 480 us apart — same-time ordering with four frames to
#     order rather than two.
#
# It states no expected exchange, because it was not written before a Run: what
# judges a replay Run of this grid is the live Run it replays, which is the
# claim this step is about. The two grids that *are* stated beforehand are
# judged both ways.
COARSE = {"name": "coarse", "step_size_ns": 500_000_000,
          "duration_ns": 1_500_000_000}

# Four activations of one terminal land in the Slot at 1000 ms on that grid,
# and two Messages of the boundary Channel in the Slot at 0.
COARSE_CAPACITY = 8


def replay(node: Path, bus: Path, recording: Path, case: dict,
           interceptor: dict | None = None,
           capacity: int = ROUTE_CAPACITY) -> Manifest:
    """One case of the live exchange, with its first source replayed."""
    archives = {"node": node, "bus": bus}
    manifest = Manifest(duration_ns=case["duration_ns"])
    manifest.add_schemas(SCHEMAS)
    manifest.add_channel(BOUNDARY, schema=BUFFER_SCHEMA, latency_ns=0)
    for channel in RETAINED:
        manifest.add_channel(channel, schema=BUFFER_SCHEMA)
    if interceptor is not None:
        start_ns, end_ns = INTERCEPTOR_WINDOW
        manifest.add_interceptor(
            BOUNDARY, start_ns=start_ns, end_ns=end_ns, **interceptor
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
        subscribes=[SubscriberRoute(BOUNDARY, capacity=capacity)],
        publishes=list(RETAINED),
    )
    manifest.add_process(
        "observer",
        command=["python3", "/opt/measured/clocked_observer.py"],
        step_period_ns=case["step_size_ns"],
        subscribes=[
            SubscriberRoute(channel, capacity=capacity)
            for channel in SOURCES
        ],
        priority=1,
    )
    return manifest


def variants(node: Path, bus: Path, recordings: Path):
    """Every replay Manifest this proof runs, in the order it runs them."""
    for case in json.loads(EXPECTED.read_text())["cases"]:
        recording = recordings / f"connected-{case['name']}.mcap"
        yield f"replay-{case['name']}", replay(node, bus, recording, case)
        if case["name"] != INTERCEPTED_CASE:
            continue
        for name, interceptor in INTERCEPTORS.items():
            yield (
                f"replay-{case['name']}-{name}",
                replay(node, bus, recording, case, interceptor),
            )
    yield "replay-coarse", replay(
        node, bus, recordings / "live-coarse.mcap", COARSE,
        capacity=COARSE_CAPACITY,
    )


def write_and_report(manifest: Manifest, path: Path, name: str) -> None:
    """Write one Manifest and state the hash the Run will be attributed to."""
    reference = manifest.write(path)
    print(f"{name:<24} {reference.hash}  {reference.path}")


def main(argv: list[str]) -> int:
    stage, arguments = (argv[0], [Path(a) for a in argv[1:]]) if argv else \
        ("", [])
    if stage == "live" and len(arguments) == 3:
        node, bus, destination = arguments
        write_and_report(
            connected(node, bus, COARSE, capacity=COARSE_CAPACITY),
            destination / "live-coarse.json", "live-coarse",
        )
        return 0
    if stage == "replay" and len(arguments) == 4:
        node, bus, recordings, destination = arguments
        for name, manifest in variants(node, bus, recordings):
            write_and_report(
                manifest, destination / f"{name}.json", name
            )
        return 0
    raise SystemExit(
        "usage: replay_manifest.py live <node.fmu> <bus.fmu> "
        "<output-directory>\n"
        "       replay_manifest.py replay <node.fmu> <bus.fmu> "
        "<live-recording-directory> <output-directory>"
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
