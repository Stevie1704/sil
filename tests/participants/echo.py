"""Toy out-of-process participant: republishes toy.Counter values times ten."""

from sil.participant import StepParticipant, run


class Echo(StepParticipant):
    def on_step(self, t, dt, inputs):
        return [
            ("echo", {"seq": i.data["seq"], "value": i.data["value"] * 10})
            for i in inputs
        ]


if __name__ == "__main__":
    run(Echo())
