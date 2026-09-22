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

from sil.examples.acc.dynamics import (
    GAP_GAIN_PER_S2, MAX_ACCEL_MPS2, MIN_ACCEL_MPS2,
    RELATIVE_SPEED_GAIN_PER_S, STANDSTILL_GAP_M, TIME_HEADWAY_S, command_for,
)

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
