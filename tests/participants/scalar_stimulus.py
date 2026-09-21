"""Toy process participant: publishes one Float64 and one Boolean each Step,
as stimulus for the FMI importer's declared scalar bindings.

Both fields are pure functions of the step index (t // dt), so the run stays
deterministic and the Boolean alternates rather than holding one value.
"""

import sys

from sil.participant import StepParticipant, run


class ScalarStimulus(StepParticipant):
    def __init__(self, channel: str):
        self.channel = channel

    def on_step(self, t, dt, inputs):
        step = t // dt
        return [(self.channel, {"value": step / 4, "flag": step % 2})]


if __name__ == "__main__":
    run(ScalarStimulus(sys.argv[1]))
