"""In-Run minimum-gap KPI for one sensitivity row."""
from __future__ import annotations

import json
import math
import sys

from sil.participant import StepParticipant, run


class MinimumGap(StepParticipant):
    def __init__(self, threshold: float, duration_ns: int):
        self.threshold = threshold
        self.duration_ns = duration_ns
        self.messages = 0
        self.minimum_gap = math.inf
        self.minimum_gap_at_ns = None
        self.emitted = False

    def on_step(self, t, dt, inputs):
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
        if not self.emitted and t + dt >= self.duration_ns:
            print(
                "ACC_SENSITIVITY_KPI "
                + json.dumps({
                    "checked_messages": self.messages,
                    "minimum_gap_m": self.minimum_gap,
                    "minimum_gap_publication_ns": self.minimum_gap_at_ns,
                    "final_activation_ns": t,
                }),
                file=sys.stderr,
            )
            self.emitted = True


if __name__ == "__main__":
    run(MinimumGap(float(sys.argv[1]), int(sys.argv[2])))
