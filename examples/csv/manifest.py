"""The CSV replay example's Manifest: a converted CSV Recording drives a consumer.

`signals.csv` is a small timestamped recording; `mapping.json` says which
column is time, which columns feed which Channel fields, and how to scale
them. `sil-csv` converts the pair into an MCAP Recording and a receipt. This
Manifest replays that Recording into `observer.py`, which republishes what it
receives so the Run Recording shows the consumer's view.

The schemas of the replayed Channels come from the mapping itself: the Replay
participant accepts a Recording only when each Channel's recorded schema is
the one the Manifest declares, and one source for both keeps them equal. The
Recording is named by its absolute path and its SHA-256, which the Manifest
hashes, so a different conversion is a different Manifest.

Convert, build, run twice and compare:

    make example-csv

or, without the convenience target:

    PYTHONPATH=$PWD/python/src python -m sil.csv_recording \\
        examples/csv/mapping.json examples/csv/signals.csv \\
        -o build/signals.mcap --receipt build/signals.receipt.json
    PYTHONPATH=$PWD/python/src python examples/csv/manifest.py \\
        build/csv-replay.json --recording build/signals.mcap
    PYTHONPATH=$PWD/python/src ./build/sil-run build/csv-replay.json \\
        -o build/csv-replay.mcap
"""

import argparse
import json
from pathlib import Path

from sil.manifest import Manifest, SubscriberRoute

EXAMPLE_DIR = Path(__file__).resolve().parent
MAPPING = json.loads((EXAMPLE_DIR / "mapping.json").read_text())
REPLAYED = [entry["channel"] for entry in MAPPING["channels"]]

SEEN_SCHEMAS = {
    "csv.Seen": {"fields": [
        {"name": "published_ns", "type": "u64"},
        {"name": "channel", "type": "u8"},
        {"name": "value", "type": "f64"},
    ]},
}

STEP_PERIOD_NS = 10_000_000
# The last CSV Message is at 60 ms; the observer sees it at its 70 ms Step.
DURATION_NS = 100_000_000
# The most one Channel delivers between two observer Steps: two Messages of
# `ego.speed` share 40 ms.
ROUTE_CAPACITY = 2


def csv_replay_manifest(recording: Path) -> Manifest:
    """The example Run over the converted `recording`."""
    m = Manifest(duration_ns=DURATION_NS)
    m.add_schemas(MAPPING["schemas"])
    m.add_schemas(SEEN_SCHEMAS)
    for entry in MAPPING["channels"]:
        m.add_channel(entry["channel"], schema=entry["schema"])
    m.add_channel("csv.seen", schema="csv.Seen")
    m.add_replay("replay", recording=Path(recording).resolve(),
                 channels=REPLAYED)
    m.add_process(
        "observer",
        command=["python3", "-m", "sil.participant",
                 f"{EXAMPLE_DIR / 'observer.py'}:Observer"],
        step_period_ns=STEP_PERIOD_NS,
        subscribes=[SubscriberRoute(channel, capacity=ROUTE_CAPACITY)
                    for channel in REPLAYED],
        publishes=["csv.seen"],
    )
    return m


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", help="path to write the Manifest to")
    parser.add_argument("--recording", type=Path, required=True,
                        help="the Recording sil-csv wrote")
    args = parser.parse_args()
    print(csv_replay_manifest(args.recording).write(args.out).hash)
