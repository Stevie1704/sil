"""The shared-library example's Manifest: recorded input into a C library.

`signals.csv` holds recorded speed values; `sil-csv` converts it with
`mapping.json` into a Recording. The Replay participant publishes it on
`ego.speed`. Two instances of `adapter.py` load the same `speed_filter`
library, each in its own process with its own parameters, and publish its
output. `filter_test.py`, a Test participant, computes both outputs independently and fails the Run
at the first difference.

Everything a reviewer needs to reproduce the Run is explicit here: the
Period, the Latency of every Channel, finite route capacities, the library
parameters and the initial input value. The library itself is named by path.
Its bytes are not part of the Manifest hash; the Run's provenance side-car
records their SHA-256 because the command names the file.

Convert, build, run twice and compare:

    make example-library

or, with the staged installation on PATH:

    cc -shared -fPIC -O2 -o speed_filter.so examples/library/speed_filter.c
    sil-csv examples/library/mapping.json examples/library/signals.csv \\
        -o signals.mcap --receipt signals.receipt.json
    python examples/library/manifest.py library.json \\
        --recording signals.mcap --library speed_filter.so
    sil-run library.json -o run.mcap
"""

import argparse
import json
from pathlib import Path

from sil.manifest import Manifest, SubscriberRoute

EXAMPLE_DIR = Path(__file__).resolve().parent
MAPPING = json.loads((EXAMPLE_DIR / "mapping.json").read_text())

STEP_PERIOD_NS = 10_000_000
# Explicit, and equal to the default: a Message is visible at the first Step
# after it was published.
LATENCY_NS = STEP_PERIOD_NS
# The last recorded Message is at 80 ms and visible at 90 ms; the Test participant sees
# the 90 ms output at 100 ms.
DURATION_NS = 110_000_000
# One Message per Period on every Channel. A Message waits in the route until
# the subscriber's Step one Period later, and the next one can be published
# in that Slot before the subscriber takes it.
ROUTE_CAPACITY = 2
# The input before the first recorded Message, at 20 ms, is visible.
INITIAL_SPEED_MPS = 8.0

# Two instances of one library, each with its own parameters and state.
INSTANCES = {
    "filter.fast": {"time_constant_s": 0.02, "initial_speed_mps": 0.0},
    "filter.slow": {"time_constant_s": 0.05, "initial_speed_mps": 9.0},
}

OUTPUT_SCHEMAS = {
    "library.Filtered": {"fields": [
        {"name": "filtered_speed_mps", "type": "f64"},
        {"name": "cycles", "type": "u32"},
    ]},
}


def library_manifest(recording: Path, library: Path,
                     instances: dict[str, dict] = INSTANCES) -> Manifest:
    """The example Run over the converted `recording` and a `library` build."""
    m = Manifest(duration_ns=DURATION_NS)
    m.add_schemas(MAPPING["schemas"])
    m.add_schemas(OUTPUT_SCHEMAS)
    (entry,) = MAPPING["channels"]
    speed = entry["channel"]
    m.add_channel(speed, schema=entry["schema"], latency_ns=LATENCY_NS)
    for channel in instances:
        m.add_channel(channel, schema="library.Filtered",
                      latency_ns=LATENCY_NS)
    m.add_replay("replay", recording=Path(recording).resolve(),
                 channels=[speed])
    for channel, parameters in instances.items():
        m.add_process(
            channel.replace(".", "_"),
            command=[
                "python3", str(EXAMPLE_DIR / "adapter.py"),
                str(Path(library).resolve()),
                "--input", speed, "--output", channel,
                "--period-ns", str(STEP_PERIOD_NS),
                *(f"--parameter={name}={value!r}"
                  for name, value in parameters.items()),
                f"--initial=speed_mps={INITIAL_SPEED_MPS!r}",
            ],
            step_period_ns=STEP_PERIOD_NS,
            subscribes=[SubscriberRoute(speed, capacity=ROUTE_CAPACITY)],
            publishes=[channel],
        )
    m.add_process(
        "filter_test",
        command=[
            "python3", str(EXAMPLE_DIR / "filter_test.py"),
            "--input", speed, "--period-ns", str(STEP_PERIOD_NS),
            f"--initial-speed-mps={INITIAL_SPEED_MPS!r}",
            *(f"--expect={channel}={p['time_constant_s']!r},"
              f"{p['initial_speed_mps']!r}"
              for channel, p in instances.items()),
        ],
        step_period_ns=STEP_PERIOD_NS,
        subscribes=[SubscriberRoute(channel, capacity=ROUTE_CAPACITY)
                    for channel in (speed, *instances)],
    )
    return m


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", help="path to write the Manifest to")
    parser.add_argument("--recording", type=Path, required=True,
                        help="the Recording sil-csv wrote")
    parser.add_argument("--library", type=Path, required=True,
                        help="the speed_filter shared library build")
    args = parser.parse_args()
    print(library_manifest(args.recording, args.library).write(args.out).hash)
