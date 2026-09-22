"""In-run minimum-gap check; emits its coverage receipt to the Run log."""
import json
import math
import sys

from sil.participant import StepParticipant, run
from proof_support import require


class MinimumGap(StepParticipant):
    def __init__(self, threshold, step_ns, steps):
        self.threshold = threshold
        self.step_ns = step_ns
        self.steps = steps
        self.messages = 0
        self.last_publication_ns = None

    def on_step(self, t, dt, inputs):
        for message in inputs:
            gap = message.data["gap_m"]
            require(math.isfinite(gap) and gap >= self.threshold,
                    f"minimum-gap KPI: {gap} m < {self.threshold} m at publication {message.publish_ns}")
            self.messages += 1
            self.last_publication_ns = message.publish_ns
        if t == (self.steps - 1) * self.step_ns:
            print("ACC_KPI " + json.dumps(dict(checked_messages=self.messages,
                  last_publication_ns=self.last_publication_ns)), file=sys.stderr)


if __name__ == "__main__":
    run(MinimumGap(float(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])))
