"""Toy process participant: publishes toy.Counter on the channel named
on its command line, so a run can declare more than one live publisher for one
channel.

The payload is a pure function of the step index (t // dt), keeping the run
deterministic per the participant contract (no wall-clock, derived from t).
"""

import sys

from sil.participant import StepParticipant, run


class CounterSource(StepParticipant):
    def __init__(self, channel: str):
        self.channel = channel

    def on_step(self, t, dt, inputs):
        step = t // dt
        return [(self.channel, {"seq": step, "value": -int(step)})]


if __name__ == "__main__":
    run(CounterSource(sys.argv[1]))
