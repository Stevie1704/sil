"""ACC example: the plant, carrying the longitudinal motion of both vehicles.

The kinematics are deliberately trivial — constant-acceleration integration of
two positions along one axis. The example teaches the framework, not vehicle
dynamics: if you have to understand the plant to understand the example, the
plant is too big.

The plant is the environment half of the closed loop. It publishes the sensing
Channel the controller subscribes to, and integrates the acceleration the
controller commands back.
"""

from sil.participant import StepParticipant, run

from sil.examples.acc.dynamics import (
    EGO_POSITION_M, EGO_SPEED_MPS, INITIAL_COMMAND_MPS2, LEAD_ACCEL_MPS2,
    LEAD_POSITION_M, LEAD_SPEED_MPS, NS_PER_S, advance,
)

class Plant(StepParticipant):
    def __init__(self):
        self.lead_position_m = LEAD_POSITION_M
        self.lead_speed_mps = LEAD_SPEED_MPS
        self.ego_position_m = EGO_POSITION_M
        self.ego_speed_mps = EGO_SPEED_MPS
        self.commanded_accel_mps2 = INITIAL_COMMAND_MPS2

    def on_step(self, t, dt, inputs):
        # The command in hand answers sensing published two Steps ago, which
        # is what the default Latency costs around a loop. The newest command
        # wins, and the last one is held while none arrives.
        if inputs:
            self.commanded_accel_mps2 = inputs[-1].data["accel_mps2"]
        # The Message carries the state at t; integrating one step of dt comes
        # after it, so the next Step publishes the state it reaches.
        sensing = {
            "gap_m": self.lead_position_m - self.ego_position_m,
            "relative_speed_mps": self.lead_speed_mps - self.ego_speed_mps,
            "ego_speed_mps": self.ego_speed_mps,
        }
        dt_s = dt / NS_PER_S
        self.lead_position_m, self.lead_speed_mps = advance(
            self.lead_position_m, self.lead_speed_mps, LEAD_ACCEL_MPS2, dt_s
        )
        self.ego_position_m, self.ego_speed_mps = advance(
            self.ego_position_m, self.ego_speed_mps,
            self.commanded_accel_mps2, dt_s,
        )
        return [("acc.Sensing", sensing)]


if __name__ == "__main__":
    run(Plant())
