"""Toy process participant: publishes a Float64 ramp on the channel named on
its command line, as stimulus for the FMI importer.

Both fields are pure functions of the step index (t // dt), so the run stays
deterministic and every published value is distinguishable from its neighbours
and from the two fields of the same Message.
"""

import sys

from sil.participant import StepParticipant, run


class Float64Ramp(StepParticipant):
    def __init__(self, channel: str):
        self.channel = channel

    def on_step(self, t, dt, inputs):
        step = t // dt
        return [(self.channel, {
            "Float64_continuous_input": float(step),
            "Float64_discrete_input": 100.0 - step,
        })]


if __name__ == "__main__":
    run(Float64Ramp(sys.argv[1]))
