"""ACC reference Run safety KPI process participant."""

from sil.participant import StepParticipant

SAFE_GAP_M = 30.0
UNMEETABLE_GAP_M = 55.0


class MinimumGapKPI(StepParticipant):
    minimum_gap_m = SAFE_GAP_M

    def on_step(self, t, dt, inputs):
        for sensing in inputs:
            gap_m = sensing.data["gap_m"]
            assert gap_m >= self.minimum_gap_m, (
                f"gap {gap_m:.2f} m is below the {self.minimum_gap_m:.2f} m "
                f"safety gap at t={sensing.publish_ns}"
            )


class UnmeetableMinimumGapKPI(MinimumGapKPI):
    minimum_gap_m = UNMEETABLE_GAP_M
