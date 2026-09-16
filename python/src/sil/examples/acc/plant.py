"""ACC reference Run plant process participant."""

from sil.participant import StepParticipant, run

NS_PER_S = 1e9
LEAD_POSITION_M = 60.0
LEAD_SPEED_MPS = 25.0
LEAD_ACCEL_MPS2 = 0.0
EGO_POSITION_M = 0.0
EGO_SPEED_MPS = 25.0
INITIAL_COMMAND_MPS2 = 0.0


def advance(position_m: float, speed_mps: float, accel_mps2: float,
            dt_s: float) -> tuple[float, float]:
    """Advance one vehicle with constant-acceleration kinematics."""
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
        if inputs:
            self.commanded_accel_mps2 = inputs[-1].data["accel_mps2"]
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
