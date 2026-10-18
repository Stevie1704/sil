"""Process participant whose output is its own call count, not Virtual time.

It publishes how often `on_step` ran in this process, so a Recording shows
whether the Participant started fresh: a reused instance would continue the
count instead of starting again at zero.
"""

from sil.participant import StepParticipant, run


class CallCounter(StepParticipant):
    def __init__(self):
        self.calls = 0

    def on_step(self, t, dt, inputs):
        published = [("calls", {"seq": self.calls, "value": self.calls})]
        self.calls += 1
        return published


if __name__ == "__main__":
    run(CallCounter())
