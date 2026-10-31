"""The input of the managed-platform example: it publishes a counter."""

from sil.participant import StepParticipant, run


class Counter(StepParticipant):
    def __init__(self):
        self._count = 0

    def on_step(self, t, dt, inputs):
        self._count += 1
        return [("counts", {"value": self._count})]


if __name__ == "__main__":
    run(Counter())
