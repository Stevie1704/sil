"""The consumer's Manifest for the esmini adoption proof (issue #117).

Two participants and two Channels. `scenario` drives esmini and publishes the
ego's and the target's object state; `kpi` subscribes to both and holds the
Run to the cut-in safety property. Everything that decides the Run is in the
Manifest: the scenario file, the object-to-Channel mapping, esmini's seed, the
Step period, the Duration, every route capacity, and every KPI threshold.

Both variants come from this one builder:

    python3 manifest.py alks-cut-in.json
    python3 manifest.py --unmeetable-gap alks-cut-in-failing.json

The second declares a gap floor the Run cannot hold. It differs from the first
in exactly one Manifest value, so the two hashes differ and the deliberately
failing Run is a different Run rather than a different participant.

It is built with the `sil` package installed in the published runner image.
Nothing here imports from a SiL checkout.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from sil.manifest import Manifest, SubscriberRoute

CONSUMER_DIR = Path(__file__).resolve().parent
ESMINI_RESOURCES = Path(os.environ.get("ESMINI_RESOURCES", "/opt/esmini/resources"))
ESMINI_LIBRARY = os.environ.get(
    "ESMINI_LIBRARY", "/opt/esmini/bin/libesminiLib.so"
)

SCENARIO = ESMINI_RESOURCES / "xosc" / "alks_r157_cut_in_quick_brake.xosc"

EGO_CHANNEL = "esmini.Ego"
TARGET_CHANNEL = "esmini.Target"

# esmini's own smoke test drives this scenario at a 10 ms fixed timestep, and
# its API documents a fixed timestep as the precondition for repeatable
# results. The Manifest declares the same period for both participants.
STEP_PERIOD_NS = 10_000_000

# The encounter is over well before this: the target starts its lane change at
# 2 s, brakes from 15 m/s at 5.2 m/s^2 from about 4 s, and is stopped by about
# 6.9 s, and the ego is at a standstill by 7.68 s. Eight seconds covers the
# whole encounter while staying inside the scenario's own stop trigger, which
# raises esmini's quit flag at 10.00 s.
# Duration is SiL's decision; the stop trigger is esmini's. Keeping them apart
# is what stops the two definitions of "finished" from racing.
DURATION_NS = 8_000_000_000

# esmini seeds its own random number generator; this scenario uses no
# randomised action, but the seed is declared rather than left to a default so
# the Manifest covers it.
SEED = 0

# Every participant steps at the same period, so each route carries one
# Message per activation and its steady-state depth is one. A route holds a
# second Message for part of a Slot when its publisher activates ahead of the
# subscriber, which the declared priorities make the case here. Two is that
# worst case; the default "fail" policy makes an overrun abort loudly rather
# than quietly changing what the KPI sees.
ROUTE_CAPACITY = 2

# The instant the encounter is judged at. esmini's own smoke test publishes
# expected positions and speeds for this scenario at 6.35 s, so the in-run
# assertion and the vendor's reference values describe the same instant.
EVALUATE_AT_NS = 6_350_000_000

# The regulation's property, and this scenario's whole point: the ALKS must
# bring the ego to a stop without contacting the target that cut in and
# braked. The floor is a no-contact floor with a small margin — the measured
# minimum is 0.358 m at 7.68 s, where both vehicles have come to rest.
MIN_GAP_M = 0.25

# A floor the Run cannot hold, used by the failing variant. It is far above
# the measured minimum and far below the 24.96 m the Run starts at, so it is
# first violated around 6.5 s, by the encounter rather than by the scenario's
# initial conditions.
UNMEETABLE_GAP_M = 2.0

# At 6.35 s the ego has been braking for about three seconds. The ceiling sits
# above the measured 6.080 m/s and well below the 20 m/s it would still be
# doing if it had not responded to the cut-in at all.
MAX_EGO_SPEED_MPS = 10.0

# Both vehicles are in the same lane once the cut-in has completed, so their
# lateral positions differ only by lane offset.
MAX_LATERAL_OFFSET_M = 0.5


def _scenario_command(scenario: Path) -> list[str]:
    return [
        "python3",
        str(CONSUMER_DIR / "participants" / "esmini_participant.py"),
        str(scenario),
        "--library",
        ESMINI_LIBRARY,
        "--object",
        f"Ego={EGO_CHANNEL}",
        "--object",
        f"Target={TARGET_CHANNEL}",
        "--seed",
        str(SEED),
    ]


def _kpi_command(min_gap_m: float) -> list[str]:
    return [
        "python3",
        str(CONSUMER_DIR / "participants" / "kpi.py"),
        "--ego",
        EGO_CHANNEL,
        "--target",
        TARGET_CHANNEL,
        "--min-gap-m",
        str(min_gap_m),
        "--evaluate-at-ns",
        str(EVALUATE_AT_NS),
        "--max-ego-speed-mps",
        str(MAX_EGO_SPEED_MPS),
        "--max-lateral-offset-m",
        str(MAX_LATERAL_OFFSET_M),
    ]


def cut_in_manifest(
    *, scenario: Path = SCENARIO, unmeetable_gap: bool = False
) -> Manifest:
    """The proof Run, in both its variants."""
    schemas = json.loads(
        (CONSUMER_DIR / "schemas" / "esmini.json").read_text()
    )
    m = Manifest(duration_ns=DURATION_NS)
    m.add_schemas(schemas)
    m.add_channel(EGO_CHANNEL, schema="esmini.ObjectState")
    m.add_channel(TARGET_CHANNEL, schema="esmini.ObjectState")
    # The consumer artifact. It is shimmed: esmini keeps its own notion of
    # time and its applications synchronise to the wall clock by default, so
    # every wall-clock read it makes is answered from Virtual time.
    m.add_process(
        "scenario",
        command=_scenario_command(scenario),
        step_period_ns=STEP_PERIOD_NS,
        publishes=[EGO_CHANNEL, TARGET_CHANNEL],
        priority=0,
        shim=True,
    )
    # The Test participant publishes nothing, so the scenario cannot see it.
    # Its only output is the Run's exit code.
    m.add_process(
        "kpi",
        command=_kpi_command(
            UNMEETABLE_GAP_M if unmeetable_gap else MIN_GAP_M
        ),
        step_period_ns=STEP_PERIOD_NS,
        subscribes=[
            SubscriberRoute(EGO_CHANNEL, capacity=ROUTE_CAPACITY),
            SubscriberRoute(TARGET_CHANNEL, capacity=ROUTE_CAPACITY),
        ],
        priority=1,
    )
    return m


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", help="path to write the Manifest to")
    parser.add_argument(
        "--unmeetable-gap",
        action="store_true",
        help="declare a gap floor the Run cannot hold",
    )
    parser.add_argument(
        "--scenario",
        type=Path,
        default=SCENARIO,
        help="the OpenSCENARIO file esmini loads",
    )
    args = parser.parse_args(argv)
    manifest = cut_in_manifest(
        scenario=args.scenario, unmeetable_gap=args.unmeetable_gap
    )
    print(manifest.write(args.out).hash)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
