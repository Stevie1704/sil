"""The consumer's Test participant: the domain KPI for the ALKS cut-in Run.

The scenario is UN-R157 shaped: a target vehicle cuts into the ego's lane and
brakes hard, and the ego is driven by esmini's ALKS safety model. The property
that decides whether the Run passed is therefore the regulation's own: the ego
must not close on the braking target.

Three things are asserted, and they are deliberately different in kind:

- **Every Step**: the longitudinal freespace gap to the target stays above the
  declared floor. This is the safety property, and it fails the Run at the
  first Step that violates it.
- **At one declared virtual time**: the encounter actually happened — the
  target was seen in a neighbouring lane earlier, is in the ego's lane now,
  and the ego has slowed down. A Participant that publishes syntactically
  valid but unchanging object state passes the gap floor and fails all three
  of these.
- **Nothing else.** The post-hoc half of the KPI lives in `verify.py`, which
  measures the finished Recording.

Every threshold is a command-line argument, so it lives in the hashed
Manifest: the deliberately failing variant is a different Manifest with a
different hash rather than a different participant.
"""

from __future__ import annotations

import argparse

from cutin import freespace_gap_m, in_same_lane, lateral_offset_m
from sil.participant import StepParticipant, run


class CutInSafetyKPI(StepParticipant):
    """Holds the ALKS cut-in Run to its safety and encounter properties."""

    def __init__(
        self,
        *,
        ego_channel: str,
        target_channel: str,
        min_gap_m: float,
        evaluate_at_ns: int,
        max_ego_speed_mps: float,
        max_lateral_offset_m: float,
    ):
        self._ego_channel = ego_channel
        self._target_channel = target_channel
        self._min_gap_m = min_gap_m
        self._evaluate_at_ns = evaluate_at_ns
        self._max_ego_speed_mps = max_ego_speed_mps
        self._max_lateral_offset_m = max_lateral_offset_m
        self._saw_target_in_another_lane = False

    def on_step(self, t: int, dt: int, inputs: list) -> None:
        for publish_ns, ego, target in _paired(
            inputs, self._ego_channel, self._target_channel
        ):
            self._check_gap(publish_ns, ego, target)
            if not in_same_lane(ego, target):
                self._saw_target_in_another_lane = True
            if publish_ns == self._evaluate_at_ns:
                self._check_encounter(publish_ns, ego, target)

    def _check_gap(self, publish_ns: int, ego: dict, target: dict) -> None:
        gap_m = freespace_gap_m(ego, target)
        assert gap_m >= self._min_gap_m, (
            f"freespace gap {gap_m:.3f} m is below the "
            f"{self._min_gap_m:.3f} m floor at t={publish_ns} ns "
            f"(ego s={ego['s_m']:.3f} m at {ego['speed_mps']:.3f} m/s, "
            f"target s={target['s_m']:.3f} m at "
            f"{target['speed_mps']:.3f} m/s)"
        )

    def _check_encounter(self, publish_ns: int, ego: dict, target: dict) -> None:
        assert self._saw_target_in_another_lane, (
            f"target never occupied a lane other than the ego's before "
            f"t={publish_ns} ns, so the Run contains no cut-in"
        )
        assert in_same_lane(ego, target), (
            f"target is in lane {target['lane_id']} and the ego in lane "
            f"{ego['lane_id']} at t={publish_ns} ns, so the cut-in did not "
            "complete"
        )
        offset_m = lateral_offset_m(ego, target)
        assert offset_m <= self._max_lateral_offset_m, (
            f"target is {offset_m:.3f} m laterally off the ego's path "
            f"at t={publish_ns} ns, above the "
            f"{self._max_lateral_offset_m:.3f} m the completed cut-in allows"
        )
        assert ego["speed_mps"] <= self._max_ego_speed_mps, (
            f"ego is still doing {ego['speed_mps']:.3f} m/s at "
            f"t={publish_ns} ns, above the {self._max_ego_speed_mps:.3f} m/s "
            "a braking response to the cut-in target must reach"
        )



def _paired(inputs: list, ego_channel: str, target_channel: str):
    """Yield (publish_ns, ego, target) for every complete sample.

    Both objects are published in the same Slot by the same Participant, so a
    complete sample is two Messages sharing a publish time. An incomplete one
    cannot be evaluated and is left alone rather than guessed at.
    """
    by_time: dict[int, dict[str, dict]] = {}
    for message in inputs:
        by_time.setdefault(message.publish_ns, {})[message.channel] = (
            message.data
        )
    for publish_ns in sorted(by_time):
        sample = by_time[publish_ns]
        if ego_channel in sample and target_channel in sample:
            yield publish_ns, sample[ego_channel], sample[target_channel]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ego", required=True, help="the ego's Channel")
    parser.add_argument("--target", required=True, help="the target's Channel")
    parser.add_argument("--min-gap-m", type=float, required=True)
    parser.add_argument("--evaluate-at-ns", type=int, required=True)
    parser.add_argument("--max-ego-speed-mps", type=float, required=True)
    parser.add_argument("--max-lateral-offset-m", type=float, required=True)
    args = parser.parse_args(argv)
    run(
        CutInSafetyKPI(
            ego_channel=args.ego,
            target_channel=args.target,
            min_gap_m=args.min_gap_m,
            evaluate_at_ns=args.evaluate_at_ns,
            max_ego_speed_mps=args.max_ego_speed_mps,
            max_lateral_offset_m=args.max_lateral_offset_m,
        )
    )


if __name__ == "__main__":
    main()
