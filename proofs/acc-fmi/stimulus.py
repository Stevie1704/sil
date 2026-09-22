"""Sparse open-loop updates; quiet Slots deliberately leave FMU inputs held."""
import sys
from sil.participant import StepParticipant, run
from cases import CASES, INPUTS, STEP_NS


class Stimulus(StepParticipant):
    def __init__(self, case):
        self.case = CASES[case]

    def on_step(self, t, dt, inputs):
        return [(f"input{i}", dict(zip(INPUTS[self.case.model], values)))
                for i, instance in enumerate(self.case.instances)
                if (values := instance.update_at(t // STEP_NS)) is not None]


if __name__ == "__main__":
    run(Stimulus(sys.argv[1]))
