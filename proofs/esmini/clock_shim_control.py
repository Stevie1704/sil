"""The Clock-shim control: does withdrawing the shim change what esmini does?

Declaring `shim: true` on the Participant is not evidence that the shim does
anything. The Manifest says the wall clock is answered from Virtual time; only
a control says whether esmini ever asks.

So the same Run is executed twice, once with the shim and once without, and
the two Recordings are compared *by payload* rather than by file bytes. The
distinction matters: a Recording embeds its Manifest hash, and withdrawing the
shim changes the Manifest, so the two files differ by construction whatever
esmini does. What decides the question is the object state.

Either answer is a result. Identical object state says esmini makes no
wall-clock read that reaches its output in this configuration, and the shim is
insurance rather than load-bearing. Differing object state says the shim is
changing what the scenario computes, and the Run depends on it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify import read_channels, trajectory


def compare(manifest: dict, first: Path, second: Path, kpi) -> dict:
    """Count the object-state readings that differ between two Recordings."""
    left = trajectory(
        read_channels(first, manifest), kpi.ego_channel, kpi.target_channel
    )
    right = trajectory(
        read_channels(second, manifest), kpi.ego_channel, kpi.target_channel
    )
    if len(left) != len(right):
        return {
            "comparable": False,
            "reason": f"{len(left)} samples against {len(right)}",
        }

    differing = 0
    first_difference = None
    for (time_ns, ego, target), (_, other_ego, other_target) in zip(
        left, right, strict=True
    ):
        for name, a, b in (("ego", ego, other_ego), ("target", target, other_target)):
            for field, value in a.items():
                if value != b[field]:
                    differing += 1
                    if first_difference is None:
                        first_difference = {
                            "virtual_time_ns": time_ns,
                            "object": name,
                            "field": field,
                            "with_shim": value,
                            "without_shim": b[field],
                        }
    return {
        "comparable": True,
        "samples": len(left),
        "field_readings": len(left) * 2 * len(left[0][1]),
        "differing_field_readings": differing,
        "first_difference": first_difference,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--with-shim", type=Path, required=True)
    parser.add_argument("--without-shim", type=Path, required=True)
    parser.add_argument("--kpi-participant", default="kpi")
    args = parser.parse_args(argv)

    from verify import Kpi

    manifest = json.loads(args.manifest.read_text())
    result = compare(
        manifest, args.with_shim, args.without_shim,
        Kpi.from_manifest(manifest, args.kpi_participant),
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["comparable"]:
        sys.stderr.write(
            f"clock_shim_control: recordings are not comparable: "
            f"{result['reason']}\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
