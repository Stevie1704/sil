"""Build the nominal or delayed-sensing ACC reference Manifest."""

from __future__ import annotations

import argparse
import json
from importlib.resources import files

from sil.manifest import Manifest, SubscriberRoute

ACC_SCHEMAS = json.loads(
    files("sil.examples.acc").joinpath("acc.json").read_text()
)

STEP_PERIOD_NS = 10_000_000
DURATION_NS = 5_000_000_000
SENSING_DELAY_NS = 5 * STEP_PERIOD_NS
SENSING_DELAY_START_NS = 2_000_000_000
SENSING_DELAY_END_NS = 3_000_000_000
ROUTE_CAPACITY = 2
SENSING_ROUTE_CAPACITY = ROUTE_CAPACITY + SENSING_DELAY_NS // STEP_PERIOD_NS


def _participant(module: str, cls: str) -> list[str]:
    # Keep this command location-independent so source and wheel Manifests
    # have the same bytes. A consumer activates the venv (or puts its bin
    # directory on PATH), and the child then imports the installed package.
    return [
        "python3", "-m", "sil.participant",
        f"sil.examples.acc.{module}:{cls}",
    ]


def acc_manifest(*, safety_kpi: str = "MinimumGapKPI",
                 delayed_sensing: bool = False) -> Manifest:
    """Return the ACC Run, with an optional sensing delay Interceptor."""
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
    m.add_process(
        "plant",
        command=_participant("plant", "Plant"),
        step_period_ns=STEP_PERIOD_NS,
        subscribes=[SubscriberRoute("acc.Command", capacity=ROUTE_CAPACITY)],
        publishes=["acc.Sensing"],
        priority=0,
    )
    m.add_process(
        "controller",
        command=_participant("controller", "Controller"),
        step_period_ns=STEP_PERIOD_NS,
        subscribes=[SubscriberRoute(
            "acc.Sensing", capacity=SENSING_ROUTE_CAPACITY
        )],
        publishes=["acc.Command"],
        priority=1,
        shim=True,
    )
    m.add_process(
        "test",
        command=_participant("safety", safety_kpi),
        step_period_ns=STEP_PERIOD_NS,
        subscribes=[SubscriberRoute(
            "acc.Sensing", capacity=SENSING_ROUTE_CAPACITY
        )],
        priority=2,
    )
    return m


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", help="path to write the Manifest to")
    parser.add_argument(
        "--delayed-sensing", action="store_true",
        help="declare the delay Interceptor on the sensing Channel",
    )
    args = parser.parse_args(argv)
    print(acc_manifest(delayed_sensing=args.delayed_sensing).write(args.out).hash)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
