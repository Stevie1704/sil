"""The ACC example's Manifest — the single definition of the example Run.

Three participants. The plant publishes `acc.Sensing` and subscribes to
`acc.Command`, the controller does the reverse — Messages travel in both
directions, so this is a Run rather than a pipeline. The test participant
subscribes to the sensing Channel and holds the loop to its safety KPI.

Latency at the loop boundary: the Channels take the default Latency, so a
Message published at `t` is visible at the subscriber's next activation. The
plant→controller→plant path therefore closes with one Step of Latency in each
direction — a declared property of the example, not a defect to work around.

Build it and run it:

    make example

or, without the convenience target:

    PYTHONPATH=python/src python examples/acc/manifest.py build/acc.json
    PYTHONPATH=python/src ./build/sil-run build/acc.json -o build/acc.mcap

`sil-run` spawns the participants as child processes, so `PYTHONPATH` has to be
set on the run as well as on the build. Both paths are under the already-ignored
build directory, so a demo leaves the working tree clean.

`--delayed-sensing` builds the second variant of the same Run: one declared
Interceptor delaying the sensing Channel over a bounded window, and nothing
else. Building both gives two hashes that differ, which says the Runs are not
the same Run; what says the Interceptor is the whole of the difference is
`tests/test_example_acc.py`, which takes it back out and gets the nominal
Manifest.
"""

import argparse
import json
from pathlib import Path

from sil.manifest import Manifest, SubscriberRoute
from sil.testing import participant_command

EXAMPLE_DIR = Path(__file__).resolve().parent
ROOT = EXAMPLE_DIR.parents[1]
ACC_SCHEMAS = json.loads((ROOT / "schemas" / "acc.json").read_text())

STEP_PERIOD_NS = 10_000_000
DURATION_NS = 5_000_000_000

# The delayed-sensing variant's one Interceptor: five Steps of extra Latency on
# the sensing Channel over a one-second window. The window opens after the
# commanded acceleration has left its comfort clamp, because a clamped command
# answers stale and fresh sensing with the same number — a delay injected into
# the clamped phase would be declared and have no effect.
SENSING_DELAY_NS = 5 * STEP_PERIOD_NS
SENSING_DELAY_START_NS = 2_000_000_000
SENSING_DELAY_END_NS = 3_000_000_000

# Every participant steps at the same period, so a route carries one Message
# per activation and its steady-state depth is one. A route holds a second
# Message for part of a Slot when its publisher activates ahead of it, before
# the drain — which routes those are depends on the declared priorities, so
# every route starts from the same worst case. The policy is the default: an
# overrun is a declaration that no longer matches the run, and it should abort
# loudly.
ROUTE_CAPACITY = 2

# The sensing routes carry more, because both variants are one Manifest apart
# and a capacity is not one of the things that differs. A delayed Message holds
# the front of its route until its visible time arrives, and per-Channel order
# is preserved rather than reordered by shifted time, so the window queues one
# further Message behind it per Step of delay. Those sit on top of the two any
# route already declares. The route drains back to its steady depth in one
# Burst at the first activation after the last delayed Message becomes visible,
# which is why the delayed Run's trajectory rejoins the nominal one instead of
# staying a fixed distance from it. The nominal variant declares a worst case
# only its delayed twin reaches.
SENSING_ROUTE_CAPACITY = ROUTE_CAPACITY + SENSING_DELAY_NS // STEP_PERIOD_NS


def acc_manifest(*, safety_kpi: str = "MinimumGapKPI",
                 delayed_sensing: bool = False) -> Manifest:
    """The example Run, in both its variants.

    `delayed_sensing` declares the Interceptor that makes the second variant:
    everything else the two variants contain is built by the lines below, so
    the difference between their Manifest hashes is the Interceptor and
    nothing else.

    `safety_kpi` names the KPI class the test participant holds the loop to, so
    a test can rebuild the same Run under a threshold it cannot meet.
    """
    m = Manifest(duration_ns=DURATION_NS)
    m.add_schemas(ACC_SCHEMAS)
    m.add_channel("acc.Sensing", schema="acc.Sensing")
    m.add_channel("acc.Command", schema="acc.Command")
    if delayed_sensing:
        m.add_interceptor(
            "acc.Sensing",
            kind="delay",
            delay_ns=SENSING_DELAY_NS,
            start_ns=SENSING_DELAY_START_NS,
            end_ns=SENSING_DELAY_END_NS,
        )
    # Priorities are declared rather than left implicit. Under the default
    # Latency they cannot change what the Run computes — that is the point of
    # the default — so the example shows the field without depending on it.
    m.add_process(
        "plant",
        command=participant_command(EXAMPLE_DIR / "plant.py", "Plant"),
        step_period_ns=STEP_PERIOD_NS,
        subscribes=[SubscriberRoute("acc.Command", capacity=ROUTE_CAPACITY)],
        publishes=["acc.Sensing"],
        priority=0,
    )
    # The controller is the vECU: shimmed, so every wall-clock read it makes
    # comes from virtual time, and left on the builder's default sleep policy.
    # The plant is unshimmed, which is also how the example shows that the
    # shim is per participant and that both kinds share one Run.
    m.add_process(
        "controller",
        command=participant_command(EXAMPLE_DIR / "controller.py",
                                    "Controller"),
        step_period_ns=STEP_PERIOD_NS,
        subscribes=[SubscriberRoute("acc.Sensing",
                                    capacity=SENSING_ROUTE_CAPACITY)],
        publishes=["acc.Command"],
        priority=1,
        shim=True,
    )
    # The test participant: it publishes no Channel and commands nothing, so
    # the loop cannot see it. Its only output is the Run's exit code.
    m.add_process(
        "test",
        command=participant_command(EXAMPLE_DIR / "safety.py", safety_kpi),
        step_period_ns=STEP_PERIOD_NS,
        subscribes=[SubscriberRoute("acc.Sensing",
                                    capacity=SENSING_ROUTE_CAPACITY)],
        priority=2,
    )
    return m


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", help="path to write the Manifest to")
    parser.add_argument(
        "--delayed-sensing", action="store_true",
        help="declare the delay Interceptor on the sensing Channel",
    )
    args = parser.parse_args()
    print(acc_manifest(delayed_sensing=args.delayed_sensing).write(args.out).hash)
