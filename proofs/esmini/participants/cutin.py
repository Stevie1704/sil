"""The cut-in encounter's domain quantities, defined once.

The in-run Test participant and the post-hoc verifier judge the same Run from
two sides, and they are deliberately different programs: one aborts a live Run
at the first violating Step, the other measures a finished Recording. What
they must not differ about is *what they are measuring*.

So the quantities live here and the judgements stay where they belong. How a
violation is reported — an assertion that ends the Run, a diagnostic over the
artifact afterwards — is each side's own business.
"""

from __future__ import annotations


def freespace_gap_m(ego: dict, target: dict) -> float:
    """Longitudinal bumper-to-bumper gap in road coordinates.

    Both vehicles travel along increasing `s` on a straight road, so the road
    coordinate is the distance measure the scenario is written in. Half a
    vehicle length at each end turns the reference-point distance into the
    freespace gap the regulation talks about.
    """
    return (
        target["s_m"] - ego["s_m"] - (ego["length_m"] + target["length_m"]) / 2
    )


def lateral_offset_m(ego: dict, target: dict) -> float:
    """How far the target sits off the ego's path, across the road."""
    return abs(target["y_m"] - ego["y_m"])


def in_same_lane(ego: dict, target: dict) -> bool:
    """Whether the target occupies the ego's lane."""
    return ego["lane_id"] == target["lane_id"]
