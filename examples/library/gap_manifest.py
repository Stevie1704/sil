"""The port binding example's Manifest: two recorded inputs into gap_monitor.

`gap_signals.csv` holds an ego speed every 20 ms and a radar object every
30 ms, two of them at the same time; `sil-csv` converts it with
`gap_mapping.json` into a Recording. The Replay participant publishes both
Channels. One `adapter.py` instance loads `gap_monitor` through
`gap_binding.py`: two input ports, two output ports and two cyclic entry
points. `gap_test.py`, a Test participant, computes both outputs
independently and fails the Run at the first difference.

Convert, build, run twice and compare:

    make example-library-ports

or, with the staged installation on PATH:

    cc -shared -fPIC -O2 -o gap_monitor.so examples/library/gap_monitor.c
    sil-csv examples/library/gap_mapping.json examples/library/gap_signals.csv \\
        -o gap-signals.mcap --receipt gap-signals.receipt.json
    python examples/library/gap_manifest.py gap.json \\
        --recording gap-signals.mcap --library gap_monitor.so
    sil-run gap.json -o run.mcap --participant-timeout-ms 10000
"""

import argparse
import json
from pathlib import Path

from sil.manifest import Manifest, SubscriberRoute

EXAMPLE_DIR = Path(__file__).resolve().parent
MAPPING = json.loads((EXAMPLE_DIR / "gap_mapping.json").read_text())

STEP_PERIOD_NS = 10_000_000
# The two inputs have different Latencies: an ego speed is visible at the
# first Step after it was published, a radar object two Steps after.
INPUT_LATENCY_NS = {"ego.motion": 10_000_000, "radar.object": 20_000_000}
# An output is visible in the Slot that publishes it, and the Test
# participant runs after the library in each Slot.
OUTPUT_LATENCY_NS = 0
LIBRARY_PRIORITY = 0
TEST_PRIORITY = 1
# The last recorded Message is at 80 ms and visible at 90 ms; the Step at
# 100 ms runs once more on the held inputs and is a `report` Step.
DURATION_NS = 110_000_000
# The radar Burst at 40 ms is two Messages that wait two Periods in the route.
INPUT_ROUTE_CAPACITY = {"ego.motion": 2, "radar.object": 2}
# At most one output per Channel and Step, taken in the Slot that publishes it.
OUTPUT_ROUTE_CAPACITY = 1

# Each input port's Channel and its value before the first Message is
# visible, field by field.
INPUTS = {
    "ego": ("ego.motion", {"speed_mps": 20.0}),
    "radar": ("radar.object", {"range_m": 50.0, "object_id": 0}),
}
OUTPUTS = {"gap": "monitor.gap", "report": "monitor.report"}
WARNING_GAP_S = 1.2

OUTPUT_SCHEMAS = {
    "gap.TimeGap": {"fields": [
        {"name": "time_gap_s", "type": "f64"},
        {"name": "object_id", "type": "u32"},
        {"name": "track_cycles", "type": "u32"},
    ]},
    "gap.Report": {"fields": [
        {"name": "min_time_gap_s", "type": "f64"},
        {"name": "warnings", "type": "u32"},
        {"name": "report_cycles", "type": "u32"},
    ]},
}
OUTPUT_SCHEMA_OF = {"monitor.gap": "gap.TimeGap",
                    "monitor.report": "gap.Report"}


def gap_monitor_manifest(recording: Path, library: Path, *,
                         step_period_ns: int = STEP_PERIOD_NS,
                         schemas: dict | None = None,
                         duration_ns: int = DURATION_NS,
                         participants: Path = EXAMPLE_DIR) -> Manifest:
    """The example Run over the converted `recording` and a `library` build.

    `schemas` replaces Schemas of the same name, so a test can declare a
    Schema that does not match the binding. `participants` is the directory
    holding `adapter.py`, the bindings and `gap_test.py`."""
    m = Manifest(duration_ns=duration_ns)
    m.add_schemas({**MAPPING["schemas"], **OUTPUT_SCHEMAS, **(schemas or {})})
    for entry in MAPPING["channels"]:
        m.add_channel(entry["channel"], schema=entry["schema"],
                      latency_ns=INPUT_LATENCY_NS[entry["channel"]])
    for channel, schema in OUTPUT_SCHEMA_OF.items():
        m.add_channel(channel, schema=schema, latency_ns=OUTPUT_LATENCY_NS)
    inputs = [entry["channel"] for entry in MAPPING["channels"]]
    m.add_replay("replay", recording=Path(recording).resolve(),
                 channels=inputs)
    input_routes = [
        SubscriberRoute(channel, capacity=INPUT_ROUTE_CAPACITY[channel])
        for channel in inputs
    ]
    directory = Path(participants).resolve()
    m.add_process(
        "gap_monitor",
        command=[
            "python3", str(directory / "adapter.py"),
            str(Path(library).resolve()),
            "--binding", str(directory / "gap_binding.py"),
            *(f"--port={port}={channel}"
              for port, (channel, _) in INPUTS.items()),
            *(f"--port={port}={channel}" for port, channel in OUTPUTS.items()),
            "--period-ns", str(step_period_ns),
            f"--parameter=warning_gap_s={WARNING_GAP_S!r}",
            *(f"--initial={port}.{name}={value!r}"
              for port, (_, values) in INPUTS.items()
              for name, value in values.items()),
        ],
        step_period_ns=step_period_ns,
        subscribes=input_routes,
        publishes=list(OUTPUTS.values()),
        priority=LIBRARY_PRIORITY,
    )
    m.add_process(
        "gap_test",
        command=[
            "python3", str(directory / "gap_test.py"),
            f"--warning-gap-s={WARNING_GAP_S!r}",
            f"--initial-speed-mps={INPUTS['ego'][1]['speed_mps']!r}",
            f"--initial-range-m={INPUTS['radar'][1]['range_m']!r}",
            f"--initial-object-id={INPUTS['radar'][1]['object_id']!r}",
        ],
        step_period_ns=step_period_ns,
        subscribes=[
            *input_routes,
            *(SubscriberRoute(channel, capacity=OUTPUT_ROUTE_CAPACITY)
              for channel in OUTPUTS.values()),
        ],
        priority=TEST_PRIORITY,
    )
    return m


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", help="path to write the Manifest to")
    parser.add_argument("--recording", type=Path, required=True,
                        help="the Recording sil-csv wrote")
    parser.add_argument("--library", type=Path, required=True,
                        help="the gap_monitor shared library build")
    args = parser.parse_args()
    print(gap_monitor_manifest(args.recording, args.library)
          .write(args.out).hash)
