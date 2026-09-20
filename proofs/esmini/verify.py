"""Post-hoc half of the proof: read the Recording, judge it, report it.

Three separate questions are answered here, and they stay separate because
they can fail for different reasons:

- **Is the Recording readable and complete?** The declared Channels are
  present, and their virtual timestamps are exactly the Slots the Manifest's
  Step period and Duration define.
- **Did the Run behave?** The domain KPI is recomputed over the whole
  trajectory, including the final Message the in-run half cannot see, because
  under the default Latency a Message published one Step before the Duration
  becomes visible at the Duration, where no activation is due.
- **Does it agree with the vendor?** The recorded trajectory is compared
  against esmini's own expected positions and speeds with a tolerance. That
  question is independent of the determinism check: determinism says two Runs
  agree with each other, this says the Run agrees with the upstream project.

It reads only the Recording and the Manifest, so it judges the artifacts a
consumer keeps rather than the process that produced them.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from sil.recording import read_records
from sil.schema import load as load_schemas

NANOS_PER_SECOND = 1_000_000_000


class VerificationError(AssertionError):
    """A recorded Run that does not hold up."""


def read_channel(recording: Path, manifest: dict) -> dict[str, list[tuple[int, dict]]]:
    """Channel name -> [(virtual_time_ns, fields), ...] in recorded order."""
    types = load_schemas(manifest["schemas"])
    schema_of = {
        name: channel["schema"]
        for name, channel in manifest["channels"].items()
    }
    messages: dict[str, list[tuple[int, dict]]] = {
        name: [] for name in schema_of
    }
    for topic, log_time_ns, data in read_records(recording):
        if topic not in schema_of:
            raise VerificationError(
                f"recording carries channel {topic!r}, which the Manifest "
                "does not declare"
            )
        messages[topic].append((log_time_ns, types[schema_of[topic]].unpack(data)))
    return messages


def check_slots(
    messages: dict[str, list[tuple[int, dict]]], manifest: dict
) -> list[int]:
    """Every declared Channel carries one Message per Slot, and no other."""
    period_ns = min(
        participant["step_period_ns"]
        for participant in manifest["participants"].values()
        if participant["type"] == "process"
    )
    expected = list(range(0, manifest["duration_ns"], period_ns))
    for channel, recorded in messages.items():
        if not recorded:
            raise VerificationError(f"channel {channel!r} recorded no Message")
        times = [time_ns for time_ns, _ in recorded]
        if times != expected:
            raise VerificationError(
                f"channel {channel!r} carries {len(times)} Messages between "
                f"{times[0]} ns and {times[-1]} ns; the Manifest's Duration "
                f"and Step period define {len(expected)} between "
                f"{expected[0]} ns and {expected[-1]} ns"
            )
    return expected


def freespace_gap_m(ego: dict, target: dict) -> float:
    return (
        target["s_m"] - ego["s_m"] - (ego["length_m"] + target["length_m"]) / 2
    )


def trajectory(
    messages: dict[str, list[tuple[int, dict]]],
    ego_channel: str,
    target_channel: str,
) -> list[tuple[int, dict, dict]]:
    ego = dict(messages[ego_channel])
    target = dict(messages[target_channel])
    return [(time_ns, ego[time_ns], target[time_ns]) for time_ns in sorted(ego)]


def check_kpi(
    samples: list[tuple[int, dict, dict]],
    *,
    min_gap_m: float,
    evaluate_at_ns: int,
    max_ego_speed_mps: float,
    max_lateral_offset_m: float,
) -> dict:
    """The domain KPI over the finished Recording."""
    gaps = [(freespace_gap_m(ego, target), t) for t, ego, target in samples]
    minimum_gap_m, minimum_gap_at_ns = min(gaps)
    if minimum_gap_m < min_gap_m:
        raise VerificationError(
            f"minimum freespace gap {minimum_gap_m:.3f} m at "
            f"{minimum_gap_at_ns} ns is below the {min_gap_m:.3f} m floor"
        )

    evaluated = [s for s in samples if s[0] == evaluate_at_ns]
    if not evaluated:
        raise VerificationError(
            f"recording has no Message at {evaluate_at_ns} ns, so the "
            "encounter was never judged"
        )
    _, ego, target = evaluated[0]

    if not any(e["lane_id"] != t["lane_id"] for _, e, t in samples):
        raise VerificationError(
            "target never occupied a lane other than the ego's, so the "
            "recording contains no cut-in"
        )
    if ego["lane_id"] != target["lane_id"]:
        raise VerificationError(
            f"target is in lane {target['lane_id']} and the ego in lane "
            f"{ego['lane_id']} at {evaluate_at_ns} ns, so the cut-in did not "
            "complete"
        )
    lateral_offset_m = abs(target["y_m"] - ego["y_m"])
    if lateral_offset_m > max_lateral_offset_m:
        raise VerificationError(
            f"target is {lateral_offset_m:.3f} m laterally off the ego's path "
            f"at {evaluate_at_ns} ns, above the allowed "
            f"{max_lateral_offset_m:.3f} m"
        )
    if ego["speed_mps"] > max_ego_speed_mps:
        raise VerificationError(
            f"ego is still doing {ego['speed_mps']:.3f} m/s at "
            f"{evaluate_at_ns} ns, above the {max_ego_speed_mps:.3f} m/s a "
            "braking response must reach"
        )
    return {
        "minimum_gap_m": minimum_gap_m,
        "minimum_gap_at_ns": minimum_gap_at_ns,
        "ego_speed_at_evaluation_mps": ego["speed_mps"],
        "target_speed_at_evaluation_mps": target["speed_mps"],
        "lateral_offset_at_evaluation_m": lateral_offset_m,
        "initial_gap_m": gaps[0][0],
    }


def _heading_difference(recorded: float, expected: float) -> float:
    """Shortest angular distance, so 6.283 and 0.0 are the same heading."""
    return abs((recorded - expected + math.pi) % (2 * math.pi) - math.pi)


def check_reference(
    samples: list[tuple[int, dict, dict]], reference: dict
) -> list[dict]:
    """Compare the recorded trajectory against the vendor's expected values."""
    tolerance = reference["tolerance"]
    by_time = {t: {"Ego": ego, "Target": target} for t, ego, target in samples}
    checked = []
    for expected in reference["samples"]:
        time_ns = round(expected["t_s"] * NANOS_PER_SECOND)
        if time_ns not in by_time:
            raise VerificationError(
                f"recording has no Message at {expected['t_s']} s, which the "
                "vendor expectation describes"
            )
        recorded = by_time[time_ns][expected["object"]]
        deviations = {
            "x_m": abs(recorded["x_m"] - expected["x_m"]),
            "y_m": abs(recorded["y_m"] - expected["y_m"]),
            "speed_mps": abs(recorded["speed_mps"] - expected["speed_mps"]),
            "heading_rad": _heading_difference(
                recorded["heading_rad"], expected["heading_rad"]
            ),
        }
        limits = {
            "x_m": tolerance["position_m"],
            "y_m": tolerance["position_m"],
            "speed_mps": tolerance["speed_mps"],
            "heading_rad": tolerance["heading_rad"],
        }
        for field, deviation in deviations.items():
            if deviation > limits[field]:
                raise VerificationError(
                    f"{expected['object']} {field} at {expected['t_s']} s is "
                    f"{recorded[field]:.3f}, {deviation:.3f} away from the "
                    f"vendor's {expected[field]:.3f}; tolerance is "
                    f"{limits[field]}"
                )
        checked.append(
            {
                "t_s": expected["t_s"],
                "object": expected["object"],
                "deviations": deviations,
            }
        )
    return checked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--recording", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--ego-channel", required=True)
    parser.add_argument("--target-channel", required=True)
    parser.add_argument("--min-gap-m", type=float, required=True)
    parser.add_argument("--evaluate-at-ns", type=int, required=True)
    parser.add_argument("--max-ego-speed-mps", type=float, required=True)
    parser.add_argument("--max-lateral-offset-m", type=float, required=True)
    parser.add_argument(
        "--observations",
        type=Path,
        help="write the measured observations here as JSON",
    )
    args = parser.parse_args(argv)

    manifest = json.loads(args.manifest.read_text())
    reference = json.loads(args.reference.read_text())

    try:
        messages = read_channel(args.recording, manifest)
        slots = check_slots(messages, manifest)
        samples = trajectory(messages, args.ego_channel, args.target_channel)
        kpi = check_kpi(
            samples,
            min_gap_m=args.min_gap_m,
            evaluate_at_ns=args.evaluate_at_ns,
            max_ego_speed_mps=args.max_ego_speed_mps,
            max_lateral_offset_m=args.max_lateral_offset_m,
        )
        reference_check = check_reference(samples, reference)
    except VerificationError as error:
        sys.stderr.write(f"verify: {error}\n")
        return 1

    observations = {
        "recording_bytes": args.recording.stat().st_size,
        "messages": {name: len(items) for name, items in messages.items()},
        "slots": len(slots),
        "first_virtual_time_ns": slots[0],
        "last_virtual_time_ns": slots[-1],
        "kpi": kpi,
        "reference_samples_checked": len(reference_check),
        "reference_max_deviation": {
            field: max(
                sample["deviations"][field] for sample in reference_check
            )
            for field in ("x_m", "y_m", "speed_mps", "heading_rad")
        },
    }
    text = json.dumps(observations, indent=2, sort_keys=True)
    print(text)
    if args.observations:
        args.observations.write_text(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
