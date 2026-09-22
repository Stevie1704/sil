"""In-Run minimum-gap KPI for one sensitivity row."""
from __future__ import annotations

import json
import math
import sys

from sil.participant import StepParticipant, run


class MinimumGap(StepParticipant):
    def __init__(self, threshold: float):
        self.threshold = threshold
        self.messages = 0
        self.minimum_gap = math.inf
        self.minimum_gap_at_ns = None

    def on_step(self, _t, _dt, inputs):
        for message in inputs:
            gap = message.data["gap_m"]
            if not math.isfinite(gap) or gap < self.threshold:
                raise AssertionError(
                    f"sensitivity minimum-gap KPI: {gap} m < {self.threshold} m "
                    f"at publication {message.publish_ns}"
                )
            self.messages += 1
            if gap < self.minimum_gap:
                self.minimum_gap = gap
                self.minimum_gap_at_ns = message.publish_ns
        if self.minimum_gap_at_ns is not None:
            print(
                "ACC_SENSITIVITY_KPI "
                + json.dumps({
                    "checked_messages": self.messages,
                    "minimum_gap_m": self.minimum_gap,
                    "minimum_gap_publication_ns": self.minimum_gap_at_ns,
                }),
                file=sys.stderr,
            )


if __name__ == "__main__":
    run(MinimumGap(float(sys.argv[1])))

