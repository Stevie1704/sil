"""ACC example: the test participant carrying the safety KPI.

The KPI is the one an ACC function is judged on first: the gap to the lead
vehicle never falls below a safety floor. This participant evaluates it inside
the deterministic world, at the virtual times its own Step period defines, and
a violation aborts the Run with the message written here.

The same KPI is computed a second time post-hoc, in the example's pytest test,
from the recorded sensing Messages. The two halves are complementary rather
than redundant: the in-run half stops a Run the moment it leaves the safe
envelope, and the post-hoc half measures a Run that finished.

Under the default Latency the sensing this participant evaluates is one Step
old, exactly as it is for the controller. The violation it reports therefore
names the Message's publish time rather than the activation that caught it, so
the time in the message is the one the Recording carries.

The same Latency costs this half the last Message of the Run: sensing
published one Step before the Duration would become visible at the Duration
itself, where no activation is due. The in-run half therefore covers every
Step but the final one, and the post-hoc half — which reads the Recording,
where that Message is — covers all of them. That is a reason to keep both,
not a defect in either.
"""

from sil.participant import StepParticipant

# The floor the gap must hold: the controller's standstill margin plus one
# second of headway at the initial speed. It sits below the gap the control
# law settles at, so the KPI measures the Run rather than restating the
# controller's own setpoint.
SAFE_GAP_M = 30.0

# A floor the Run cannot hold, used by the example's test to prove the in-run
# assertion is load-bearing. The ego starts 60 m behind the lead and is
# commanded to close, so any floor above the gap it settles at is violated
# partway through — by the dynamics, not by the initial conditions.
UNMEETABLE_GAP_M = 55.0


class MinimumGapKPI(StepParticipant):
    """Fails the Run at the first sensing Message below the safety gap."""

    minimum_gap_m = SAFE_GAP_M

    def on_step(self, t, dt, inputs):
        for sensing in inputs:
            gap_m = sensing.data["gap_m"]
            assert gap_m >= self.minimum_gap_m, (
                f"gap {gap_m:.2f} m is below the {self.minimum_gap_m:.2f} m "
                f"safety gap at t={sensing.publish_ns}"
            )


class UnmeetableMinimumGapKPI(MinimumGapKPI):
    """The same KPI at a threshold the Run cannot meet."""

    minimum_gap_m = UNMEETABLE_GAP_M
