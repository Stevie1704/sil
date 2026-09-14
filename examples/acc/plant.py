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

NS_PER_S = 1e9

# The lead vehicle holds its speed; the ego starts one gap behind it, at the
# same speed, so every metre the gap moves comes from the commanded
# acceleration rather than from the initial conditions.
LEAD_POSITION_M = 60.0
LEAD_SPEED_MPS = 25.0
LEAD_ACCEL_MPS2 = 0.0
EGO_POSITION_M = 0.0
EGO_SPEED_MPS = 25.0

# What the ego holds until the first command arrives. Under the default unit
# latency that is two Steps: the controller sees the first sensing Message one
# Step after it is published, and its answer arrives one Step after that.
INITIAL_COMMAND_MPS2 = 0.0


def advance(position_m: float, speed_mps: float, accel_mps2: float,
            dt_s: float) -> tuple[float, float]:
    """One vehicle's exact constant-acceleration motion over one step."""
    return (
        position_m + speed_mps * dt_s + 0.5 * accel_mps2 * dt_s * dt_s,
        speed_mps + accel_mps2 * dt_s,
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
        # is what the default unit latency costs around a loop. The newest
        # command wins, and the last one is held while none arrives.
        for message in inputs:
            self.commanded_accel_mps2 = message.data["accel_mps2"]
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
