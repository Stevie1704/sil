"""Deliberately broken participant: publishes entropy, violating the
determinism contract. Exists to prove the check catches it."""

import os

from sil.participant import StepParticipant, run


class NonDeterministic(StepParticipant):
    def on_step(self, t, dt, inputs):
        noise = int.from_bytes(os.urandom(4), "little")
        return [("echo", {"seq": 0, "value": noise})]


if __name__ == "__main__":
    run(NonDeterministic())
