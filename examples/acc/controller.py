"""ACC example: the controller, the vECU half of the closed loop.

It subscribes to the sensing Channel, compares the measured gap against the
gap its headway policy asks for, and publishes the commanded acceleration the
plant integrates. The law is a proportional term on the gap error plus a
damping term on the relative speed, clamped to a comfort envelope — small
enough to read in one sitting, because the example teaches the framework
rather than control design.

Latency at the loop boundary: under the default Latency a Message published
at `t` is visible at the subscriber's next activation. The sensing
this participant answers is therefore one Step old, and the command it
publishes reaches the plant one Step later still. A reader who assumes
same-slot feedthrough will write a wrong controller.

The participant is declared with the clock shim enabled (see `manifest.py`).
It reads no clock itself, but a real vECU does, and the example should carry
what a real vECU costs rather than what an ideal one costs.
"""

from sil.participant import StepParticipant, run

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


class Controller(StepParticipant):
    def on_step(self, t, dt, inputs):
        # At matched Step periods the route delivers exactly one sensing
        # Message per activation, except at t = 0 where none has become
        # visible yet. The newest wins if the plant ever runs faster.
        if not inputs:
            return None
        sensing = inputs[-1].data
        return [("acc.Command", {"accel_mps2": command_for(**sensing)})]


if __name__ == "__main__":
    run(Controller())
