"""Toy process participant: the ACC plant FMU's contract without the FMU.

    standin_acc_plant.py <initial_lead_position_m>

The qualified `AccPlant.fmu` (proofs/acc-fmi) ships Linux x86-64 binaries
only. This stand-in steps the same `dynamics.advance` under the importer's
time convention, so a host test can close the issue #227 loop: inputs
`accel_mps2` and `lead_accel_mps2` are held until replaced, the Step at t
advances over [t, t + dt], and its truth, published in Slot t, describes
t + dt.
"""

import sys

from sil.examples.acc.dynamics import (EGO_POSITION_M, EGO_SPEED_MPS,
                                       LEAD_SPEED_MPS, advance)
from sil.participant import StepParticipant, run


class StandinAccPlant(StepParticipant):
    def __init__(self, lead_position_m: float):
        self.ego = (EGO_POSITION_M, EGO_SPEED_MPS)
        self.lead = (lead_position_m, LEAD_SPEED_MPS)
        self.inputs = {"accel_mps2": 0.0, "lead_accel_mps2": 0.0}

    def on_step(self, t, dt, inputs):
        for message in inputs:
            self.inputs.update(message.data)
        h = dt / 1e9
        self.ego = advance(*self.ego, self.inputs["accel_mps2"], h)
        self.lead = advance(*self.lead, self.inputs["lead_accel_mps2"], h)
        return [("loop.truth", {
            "ego_position_m": self.ego[0], "ego_speed_mps": self.ego[1],
            "lead_position_m": self.lead[0], "lead_speed_mps": self.lead[1],
            "gap_m": self.lead[0] - self.ego[0],
            "relative_speed_mps": self.lead[1] - self.ego[1]})]


if __name__ == "__main__":
    run(StandinAccPlant(float(sys.argv[1])))
