"""In-schedule test participants for the pytest frontend example."""

from sil.participant import StepParticipant


class TicksAreCorrect(StepParticipant):
    """Stimulus-free checker: asserts tick invariants at defined times."""

    def __init__(self):
        self.seen = 0

    def on_step(self, t, dt, inputs):
        for i in inputs:
            assert i.data["value"] == 3 * i.data["seq"], f"corrupt tick {i.data}"
            self.seen += 1
        if t == 90_000_000:
            assert self.seen == 9, f"expected 9 ticks before t=90ms, saw {self.seen}"


class TicksAreWrong(StepParticipant):
    """Asserts an invariant the producer does not satisfy."""

    def on_step(self, t, dt, inputs):
        for i in inputs:
            assert i.data["value"] == i.data["seq"], (
                f"value should equal seq at t={t}"
            )
