"""Authored inputs, communication grid and tolerances, shared by both drivers."""
from dataclasses import dataclass


@dataclass(frozen=True)
class InstanceCase:
    start: tuple[float, ...]
    updates: dict[int, tuple[float, ...]]

    def update_at(self, step: int) -> tuple[float, ...] | None:
        return self.updates.get(step)

    def inputs_at(self, step: int) -> tuple[float, ...]:
        latest = max((point for point in self.updates if point <= step), default=None)
        return self.start if latest is None else self.updates[latest]


@dataclass(frozen=True)
class Case:
    model: str
    instances: tuple[InstanceCase, ...]


STEP_NS = 100_000_000
STEPS = 10
# Fixed before execution; SI absolute tolerance plus a small relative allowance.
ABS_TOL = 1e-10
REL_TOL = 1e-12
INPUTS = {
    "AccController": ["gap_m", "relative_speed_mps", "ego_speed_mps"],
    "AccPlant": ["accel_mps2"],
}
OUTPUTS = {
    "AccController": ["accel_mps2"],
    "AccPlant": ["ego_position_m", "ego_speed_mps", "lead_position_m",
                 "lead_speed_mps", "gap_m", "relative_speed_mps"],
}
# Changes at communication points; absent updates deliberately exercise hold.
CASES = {
    "controller": Case("AccController", (
        InstanceCase((60.0, 0.0, 25.0), {
            0: (60.0, 0.0, 25.0), 2: (10.0, -5.0, 25.0),
            4: (43.5, 0.0, 25.0), 6: (42.5, 0.0, 25.0),
            8: (42.5, 1.0, 25.0)}),
        InstanceCase((42.5, -1.0, 25.0), {
            0: (42.5, -1.0, 25.0), 3: (100.0, 0.0, 25.0),
            7: (0.0, 0.0, 25.0)}),
    )),
    "plant-accelerate": Case("AccPlant", (
        InstanceCase((1.5,), {0: (1.5,)}),
        InstanceCase((-3.0,), {0: (-3.0,)}),
    )),
    "plant-coast": Case("AccPlant", (
        InstanceCase((0.0,), {0: (0.0,)}),
        InstanceCase((0.5,), {0: (0.5,)}),
    )),
}
