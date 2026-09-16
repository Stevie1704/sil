"""ACC reference Run controller process participant."""

from sil.participant import StepParticipant, run

STANDSTILL_GAP_M = 5.0
TIME_HEADWAY_S = 1.5
GAP_GAIN_PER_S2 = 0.35
RELATIVE_SPEED_GAIN_PER_S = 1.2
MAX_ACCEL_MPS2 = 1.5
MIN_ACCEL_MPS2 = -3.0


def command_for(gap_m: float, relative_speed_mps: float,
                ego_speed_mps: float) -> float:
    """Calculate the ACC command for one sensing Message."""
    desired_gap_m = STANDSTILL_GAP_M + TIME_HEADWAY_S * ego_speed_mps
    accel_mps2 = (
        GAP_GAIN_PER_S2 * (gap_m - desired_gap_m)
        + RELATIVE_SPEED_GAIN_PER_S * relative_speed_mps
    )
    return max(MIN_ACCEL_MPS2, min(MAX_ACCEL_MPS2, accel_mps2))


class Controller(StepParticipant):
    def on_step(self, t, dt, inputs):
        if not inputs:
            return None
        sensing = inputs[-1].data
        return [("acc.Command", {"accel_mps2": command_for(**sensing)})]


if __name__ == "__main__":
    run(Controller())
