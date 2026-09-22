"""Pure ACC control law and longitudinal motion, shared with the FMU proof."""

# The gap the controller holds at speed: a fixed standstill margin plus a time
# headway. Both are policy, not dynamics — an ACC reader recognizes them.
STANDSTILL_GAP_M = 5.0
TIME_HEADWAY_S = 1.5

# Proportional gain on the gap error, damping gain on the relative speed.
GAP_GAIN_PER_S2 = 0.35
RELATIVE_SPEED_GAIN_PER_S = 1.2

# The comfort envelope the command is clamped to.
MAX_ACCEL_MPS2 = 1.5
MIN_ACCEL_MPS2 = -3.0


def command_for(gap_m: float, relative_speed_mps: float,
                ego_speed_mps: float) -> float:
    """The control law over one sensing Message, as a pure function.

    Its parameters are the sensing schema's fields, so a test can apply the
    law to a recorded Message and compare it against the recorded command.
    """
    desired_gap_m = STANDSTILL_GAP_M + TIME_HEADWAY_S * ego_speed_mps
    accel_mps2 = (
        GAP_GAIN_PER_S2 * (gap_m - desired_gap_m)
        + RELATIVE_SPEED_GAIN_PER_S * relative_speed_mps
    )
    return max(MIN_ACCEL_MPS2, min(MAX_ACCEL_MPS2, accel_mps2))


NS_PER_S = 1e9

# The lead vehicle holds its speed; the ego starts one gap behind it, at the
# same speed, so every metre the gap moves comes from the commanded
# acceleration rather than from the initial conditions.
LEAD_POSITION_M = 60.0
LEAD_SPEED_MPS = 25.0
LEAD_ACCEL_MPS2 = 0.0
EGO_POSITION_M = 0.0
EGO_SPEED_MPS = 25.0

# What the ego holds until the first command arrives. Under the default
# Latency that is two Steps: the controller sees the first sensing Message one
# Step after it is published, and its answer arrives one Step after that.
INITIAL_COMMAND_MPS2 = 0.0


def advance(position_m: float, speed_mps: float, accel_mps2: float,
            dt_s: float) -> tuple[float, float]:
    """One vehicle's exact constant-acceleration motion over one step."""
    return (
        position_m + speed_mps * dt_s + 0.5 * accel_mps2 * dt_s * dt_s,
        speed_mps + accel_mps2 * dt_s,
    )
