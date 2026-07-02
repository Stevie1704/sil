"""Toy test participant whose assertion fails at t=20ms virtual time."""

from sil.participant import StepParticipant, run


class FailAt20ms(StepParticipant):
    def on_step(self, t, dt, inputs):
        assert t < 20_000_000, f"boom at t={t}"


if __name__ == "__main__":
    run(FailAt20ms())
