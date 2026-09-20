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
import itertools
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

# The Test participant and this verifier must agree on what they measure,
# so the quantities come from the one module that defines them.
sys.path.insert(0, str(Path(__file__).resolve().parent / "participants"))

from cutin import freespace_gap_m, in_same_lane, lateral_offset_m
from sil.recording import read_records
from sil.schema import load as load_schemas

NANOS_PER_SECOND = 1_000_000_000


class VerificationError(AssertionError):
    """A recorded Run that does not hold up."""


@dataclass(frozen=True)
class Kpi:
    """The Channels and thresholds the Run itself enforced.

    They are read back out of the Manifest rather than restated here: the
    Manifest is what the Run was executed from, so a post-hoc judgement built
    from anything else could disagree with the Run it is judging.
    """

    ego_channel: str
    target_channel: str
    min_gap_m: float
    evaluate_at_ns: int
    max_ego_speed_mps: float
    max_lateral_offset_m: float

    @classmethod
    def from_manifest(cls, manifest: dict, participant: str) -> Kpi:
        declaration = manifest.get("participants", {}).get(participant)
        if declaration is None:
            raise VerificationError(
                f"manifest declares no participant {participant!r}"
            )
        command = declaration.get("command", [])
        # The Test participant's thresholds are its command-line options, and
        # the command is Manifest data. Pairing each token with the next one
        # reads them back without re-declaring the option list.
        options = dict(itertools.pairwise(command))

        def option(name: str, convert):
            if name not in options:
                raise VerificationError(
                    f"participant {participant!r} declares no {name}"
                )
            try:
                return convert(options[name])
            except ValueError as error:
                raise VerificationError(
                    f"participant {participant!r} {name} is not a "
                    f"{convert.__name__}: {error}"
                ) from error

        return cls(
            ego_channel=option("--ego", str),
            target_channel=option("--target", str),
            min_gap_m=option("--min-gap-m", float),
            evaluate_at_ns=option("--evaluate-at-ns", int),
            max_ego_speed_mps=option("--max-ego-speed-mps", float),
            max_lateral_offset_m=option("--max-lateral-offset-m", float),
        )


def read_channels(recording: Path, manifest: dict) -> dict[str, list[tuple[int, dict]]]:
    """Channel name -> [(virtual_time_ns, fields), ...] in recorded order."""
    types = load_schemas(manifest["schemas"])
    schema_of = {
        name: channel["schema"]
        for name, channel in manifest["channels"].items()
    }
    messages: dict[str, list[tuple[int, dict]]] = {
        name: [] for name in schema_of
    }
    for channel, log_time_ns, data in read_records(recording):
        if channel not in schema_of:
            raise VerificationError(
                f"recording carries channel {channel!r}, which the Manifest "
                "does not declare"
            )
        messages[channel].append(
            (log_time_ns, types[schema_of[channel]].unpack(data))
        )
    return messages


def slot_times(manifest: dict) -> list[int]:
    """Every Virtual time the Manifest's Duration and Step period define."""
    period_ns = min(
        participant["step_period_ns"]
        for participant in manifest["participants"].values()
        if participant["type"] == "process"
    )
    return list(range(0, manifest["duration_ns"], period_ns))


def check_slots(
    messages: dict[str, list[tuple[int, dict]]], expected: list[int]
) -> None:
    """Every declared Channel carries one Message per Slot, and no other."""
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



def trajectory(
    messages: dict[str, list[tuple[int, dict]]],
    ego_channel: str,
    target_channel: str,
) -> list[tuple[int, dict, dict]]:
    ego = dict(messages[ego_channel])
    target = dict(messages[target_channel])
    return [(time_ns, ego[time_ns], target[time_ns]) for time_ns in sorted(ego)]


def check_kpi(samples: list[tuple[int, dict, dict]], kpi: Kpi) -> dict:
    """The domain KPI over the finished Recording."""
    gaps = [(freespace_gap_m(ego, target), t) for t, ego, target in samples]
    minimum_gap_m, minimum_gap_at_ns = min(gaps)
    if minimum_gap_m < kpi.min_gap_m:
        raise VerificationError(
            f"minimum freespace gap {minimum_gap_m:.3f} m at "
            f"{minimum_gap_at_ns} ns is below the {kpi.min_gap_m:.3f} m floor"
        )

    evaluate_at_ns = kpi.evaluate_at_ns
    evaluated = [s for s in samples if s[0] == evaluate_at_ns]
    if not evaluated:
        raise VerificationError(
            f"recording has no Message at {evaluate_at_ns} ns, so the "
            "encounter was never judged"
        )
    _, ego, target = evaluated[0]

    if all(in_same_lane(e, t) for _, e, t in samples):
        raise VerificationError(
            "target never occupied a lane other than the ego's, so the "
            "recording contains no cut-in"
        )
    if not in_same_lane(ego, target):
        raise VerificationError(
            f"target is in lane {target['lane_id']} and the ego in lane "
            f"{ego['lane_id']} at {evaluate_at_ns} ns, so the cut-in did not "
            "complete"
        )
    offset_m = lateral_offset_m(ego, target)
    if offset_m > kpi.max_lateral_offset_m:
        raise VerificationError(
            f"target is {offset_m:.3f} m laterally off the ego's path "
            f"at {evaluate_at_ns} ns, above the allowed "
            f"{kpi.max_lateral_offset_m:.3f} m"
        )
    if ego["speed_mps"] > kpi.max_ego_speed_mps:
        raise VerificationError(
            f"ego is still doing {ego['speed_mps']:.3f} m/s at "
            f"{evaluate_at_ns} ns, above the {kpi.max_ego_speed_mps:.3f} m/s "
            "a braking response must reach"
        )
    return {
        "minimum_gap_m": minimum_gap_m,
        "minimum_gap_at_ns": minimum_gap_at_ns,
        "ego_speed_at_evaluation_mps": ego["speed_mps"],
        "target_speed_at_evaluation_mps": target["speed_mps"],
        "lateral_offset_at_evaluation_m": offset_m,
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
    parser.add_argument(
        "--kpi-participant",
        default="kpi",
        help="the Test participant whose declared thresholds are re-applied",
    )
    parser.add_argument(
        "--observations",
        type=Path,
        help="write the measured observations here as JSON",
    )
    args = parser.parse_args(argv)

    manifest = json.loads(args.manifest.read_text())
    reference = json.loads(args.reference.read_text())

    try:
        kpi = Kpi.from_manifest(manifest, args.kpi_participant)
        messages = read_channels(args.recording, manifest)
        slots = slot_times(manifest)
        check_slots(messages, slots)
        samples = trajectory(messages, kpi.ego_channel, kpi.target_channel)
        measured = check_kpi(samples, kpi)
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
        "kpi": measured,
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
