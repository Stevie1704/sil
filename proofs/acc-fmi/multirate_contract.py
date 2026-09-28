"""Predeclared ACC multi-rate experiment (#197); independent of SiL's plan."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE.parents[1] / "examples" / "fmu-coupling" / "acc.json"
MS = 1_000_000
DURATION_NS = 5_000 * MS
GRID_NS = 20 * MS
FIELDS = {
    "sensing": ("gap_m", "relative_speed_mps", "ego_speed_mps"),
    "command": ("accel_mps2",),
    "state": ("ego_position_m", "lead_position_m", "lead_speed_mps"),
}
# Declared before running: every field is judged at native endpoints, with
# abs(error) <= atol + rtol * abs(reference). Sensitivity uses SI absolute
# differences on the common 20 ms grid, not a fitted numerical tolerance.
ATOL = 1e-10
RTOL = 1e-12
ENVELOPE = {"gap_m": 5.0, "ego_speed_mps": 1.0,
            "accel_mps2": 1.0}
INITIAL = {"plant": {"accel_mps2": 0.0, "lead_accel_mps2": 0.0,
                     "initial_lead_position_m": 60.0},
           "controller": {"gap_m": 60.0, "relative_speed_mps": 0.0,
                          "ego_speed_mps": 25.0}}
UNITS = {"accel_mps2": "m/s2", "lead_accel_mps2": "m/s2",
         "initial_lead_position_m": "m", "gap_m": "m",
         "relative_speed_mps": "m/s", "ego_speed_mps": "m/s"}


@dataclass(frozen=True)
class Row:
    name: str
    plant_ms: int
    controller_ms: int
    sensing_ms: int
    command_ms: int
    initial_lead_m: float = 60.0

    def periods(self) -> dict[str, int]:
        return {"plant": self.plant_ms * MS, "controller": self.controller_ms * MS}

    def latencies(self) -> dict[str, int]:
        return {"sensing": self.sensing_ms * MS, "command": self.command_ms * MS}

    def publication_period(self, channel: str) -> int:
        publisher = "controller" if channel == "command" else "plant"
        return self.periods()[publisher]


ROWS = (
    Row("equal-10", 10, 10, 10, 10),
    Row("plant-20", 20, 10, 10, 10),
    Row("controller-20", 10, 20, 10, 10),
    Row("zero-sensing", 20, 10, 0, 10),
    Row("command-20", 20, 10, 10, 20),
    Row("command-30", 20, 10, 10, 30),
)
CONTROLS = (
    Row("wrong-initialization", 20, 10, 0, 10, 70.0),
    Row("one-period-shift", 20, 10, 0, 30),
)


def coupling(row: Row) -> dict:
    document = copy.deepcopy(json.loads(BASE.read_text()))
    document["duration_ns"] = DURATION_NS
    for name, period in row.periods().items():
        document["fmus"][name]["step_period_ns"] = period
        document["fmus"][name]["hold"] = []
        values = INITIAL[name].copy()
        if name == "plant":
            values["initial_lead_position_m"] = row.initial_lead_m
        document["fmus"][name]["start"] = [
            {"variable": key, "value": str(value), "unit": UNITS[key]}
            for key, value in values.items()
        ]
    for name, latency in row.latencies().items():
        document["channels"][name]["latency_ns"] = latency
        for route in document["channels"][name]["subscribers"].values():
            route["capacity"] = 8
    return document


def publication_slots(row: Row, channel: str) -> range:
    return range(0, DURATION_NS, row.publication_period(channel))


def observation_times(row: Row, channel: str) -> range:
    """Every native FMU endpoint, including Duration. No interpolation."""
    period = row.publication_period(channel)
    return range(period, DURATION_NS + 1, period)


def delivery_schedule(row: Row, channel: str) -> dict[int, int | None]:
    """Authored zero-order hold: newest visible publication or input start.

    Plant priority 0 precedes controller priority 1. Only sensing may have
    zero Latency; a plant publication in a shared Slot is then visible there.
    A positive Latency takes effect at the first subscriber activation at or
    after publication plus Latency. Unchanged inputs remain held.
    """
    source = "controller" if channel == "command" else "plant"
    target = "plant" if channel == "command" else "controller"
    source_period = row.periods()[source]
    target_period = row.periods()[target]
    latency = row.latencies()[channel]
    schedule = {}
    for activation in range(0, DURATION_NS, target_period):
        latest = ((activation - latency) // source_period) * source_period
        if latest < 0 or (channel == "command" and latest == activation):
            # Controller follows plant inside a Slot; its current publication
            # cannot feed the earlier plant activation even at zero Latency.
            latest -= source_period if latest == activation else 0
        schedule[activation] = latest if latest >= 0 else None
    return schedule


def common_grid() -> range:
    return range(GRID_NS, DURATION_NS + 1, GRID_NS)
