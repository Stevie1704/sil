"""Publish the authored lead-acceleration Maneuver on its fixed 1 ms grid."""
from __future__ import annotations

from sil.participant import StepParticipant, run

from sensitivity_contract import lead_acceleration_at_ns


class Maneuver(StepParticipant):
    def on_step(self, t: int, _dt: int, _inputs):
        return [("maneuver", {"lead_accel_mps2": lead_acceleration_at_ns(t)})]


if __name__ == "__main__":
    run(Maneuver())
