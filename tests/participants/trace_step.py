"""Scheduler-trace process participant (#404).

Usage: trace_step.py CHANNEL DURATION_NS

Each Step publishes one toy.Counter marker on CHANNEL: `seq` is the Step's
Virtual time and `value` is 0, the index of its only Task. The Recording then
holds the complete ordered Activation trace.

The interceptor drops every Publish at or after the Run duration, so a Step
there would leave no marker. The Step fails the Run instead.
"""

import sys

from sil.participant import ParticipantFailure, StepParticipant, run


class TraceStep(StepParticipant):
    def __init__(self, channel: str, duration_ns: int):
        self.channel = channel
        self.duration_ns = duration_ns

    def on_step(self, t, dt, inputs):
        if t >= self.duration_ns:
            raise ParticipantFailure(
                f"Activation at {t} ns is at or after the run duration")
        return [(self.channel, {"seq": t, "value": 0})]


if __name__ == "__main__":
    run(TraceStep(sys.argv[1], int(sys.argv[2])))
