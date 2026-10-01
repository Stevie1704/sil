"""The ADAS reference example's Manifest: recorded sensing into Native
instances of one C library.

Every maneuver that `prepare.py` converted gets its own Replay participant,
its own three input Channels and its own Native participant. All Native
participants load the same library, so the Run also shows that instances
keep their state apart: each must match its own expected trajectory.

What the Manifest makes explicit:

- **Period.** Every input is sampled every 10 ms. The library registers one
  10 ms Task and rejects any other `period_ns`.
- **Latency.** Input Channels declare `latency_ns=0`. A Replay participant
  without a priority publishes before every activation of a Slot, so a
  Message sampled at t is consumed by the activation at t.
- **Sample time.** The activation at t advances over [t, t + 10 ms] and
  publishes, in Slot t, a Command whose `sample_time_ns` is t + 10 ms. The
  Recording stores the publication Slot t, as for an FMU output.
- **Finite routes.** Each input route holds one Message and fails on
  overflow. A list is one Message, whatever its count. The Command Channels have no in-Run subscriber; they are
  recorded.
- **Profile.** Each Native config names the profile and its version; the
  library refuses another one.

    python manifest.py reference.json --inputs OUTDIR --library adas_reference.so
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sil.manifest import Manifest, SubscriberRoute

EXAMPLE_DIR = Path(__file__).resolve().parent
SCHEMAS = json.loads((EXAMPLE_DIR / "schemas.json").read_text())
MANEUVERS = ("clear", "hazard", "release", "unavailable", "boundaries",
             "occupancy", "turnover", "ordering")

PROFILE = "sil.adas-reference.radar-camera"
PROFILE_VERSION = 2
PERIOD_NS = 10_000_000
INPUT_LATENCY_NS = 0
# No in-Run subscriber reads a Command; one Period keeps the default unit
# delay explicit.
OUTPUT_LATENCY_NS = PERIOD_NS
INPUT_ROUTE_CAPACITY = 1
# Twenty activations, 0 ms to 190 ms; the last publishes Sample time 200 ms.
DURATION_NS = 200_000_000

PARAMETERS = {
    "hazard_acceleration_mps2": -3.0,
    "max_change_mps2": 0.5,
}

INPUTS = {"radar": "adas.ObjectList", "camera": "adas.ObjectList",
          "ego": "adas.EgoMotion"}


def controller_config(maneuver: str, **overrides) -> dict:
    """The Native config of one maneuver's controller."""
    return {
        "profile": PROFILE,
        "profile_version": PROFILE_VERSION,
        **{role: f"{maneuver}.{role}" for role in INPUTS},
        "command": f"{maneuver}.command",
        "period_ns": PERIOD_NS,
        **PARAMETERS,
        **overrides,
    }


def reference_manifest(inputs: Path, library: Path,
                       maneuvers: tuple[str, ...] = MANEUVERS,
                       configs: dict[str, dict] | None = None) -> Manifest:
    """The Run over the Recordings `prepare.py` wrote into `inputs`.

    `configs` replaces the config of a named maneuver's controller."""
    configs = configs or {}
    m = Manifest(duration_ns=DURATION_NS)
    m.add_schemas(SCHEMAS)
    for maneuver in maneuvers:
        for role, schema in INPUTS.items():
            m.add_channel(f"{maneuver}.{role}", schema=schema,
                          latency_ns=INPUT_LATENCY_NS)
        m.add_channel(f"{maneuver}.command", schema="adas.Command",
                      latency_ns=OUTPUT_LATENCY_NS)
        m.add_replay(
            f"{maneuver}_replay",
            recording=(Path(inputs) / f"{maneuver}.inputs.mcap").resolve(),
            channels=[f"{maneuver}.{role}" for role in INPUTS],
        )
        m.add_native(
            maneuver,
            library=str(Path(library).resolve()),
            config=configs.get(maneuver, controller_config(maneuver)),
            subscribes=[SubscriberRoute(f"{maneuver}.{role}",
                                        capacity=INPUT_ROUTE_CAPACITY)
                        for role in INPUTS],
            publishes=[f"{maneuver}.command"],
        )
    return m


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", help="path to write the Manifest to")
    parser.add_argument("--inputs", type=Path, required=True,
                        help="the directory prepare.py wrote")
    parser.add_argument("--library", type=Path, required=True,
                        help="the adas_reference Native library build")
    args = parser.parse_args()
    print(reference_manifest(args.inputs, args.library).write(args.out).hash)
