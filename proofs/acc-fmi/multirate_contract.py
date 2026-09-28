"""Predeclared ACC multi-rate experiment (#197); independent of SiL's plan."""
from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE.parents[1] / "examples" / "fmu-coupling" / "acc.json"
FMU_DIR = Path("/fmus")
MS = 1_000_000
DURATION_NS = 5_000 * MS
GRID_NS = 20 * MS
FIELDS = {
    "sensing": ("gap_m", "relative_speed_mps", "ego_speed_mps"),
    "command": ("accel_mps2",),
    "state": ("ego_position_m", "lead_position_m", "lead_speed_mps"),
}
PUBLISHER = {"sensing": "plant", "state": "plant", "command": "controller"}
# The Channel each FMU takes its inputs from.
INPUT = {"controller": "sensing", "plant": "command"}
MODELS = {"plant": "AccPlant", "controller": "AccController"}
# Plant priority 0 precedes controller priority 1 inside a shared Slot.
SLOT_ORDER = ("plant", "controller")
# Declared before running: every field is judged at native Sample times, with
# abs(error) <= atol + rtol * abs(reference). Sensitivity uses SI absolute
# differences on the common 20 ms grid, not a fitted numerical tolerance.
ATOL = 1e-10
RTOL = 1e-12
# Redeclared before the second retained run; see MULTIRATE.md for the first.
ENVELOPE = {"gap_m": 0.25, "relative_speed_mps": 0.15, "ego_speed_mps": 0.15,
            "accel_mps2": 0.25, "ego_position_m": 0.25,
            "lead_position_m": 0.25, "lead_speed_mps": 0.15}
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
    # The Row this one differs from in exactly one Period or Latency.
    baseline: str | None = None

    def periods(self) -> dict[str, int]:
        return {"plant": self.plant_ms * MS, "controller": self.controller_ms * MS}

    def latencies(self) -> dict[str, int]:
        return {"sensing": self.sensing_ms * MS, "command": self.command_ms * MS}

    def publication_period(self, channel: str) -> int:
        return self.periods()[PUBLISHER[channel]]


ROWS = (
    Row("equal-10", 10, 10, 10, 10),
    Row("plant-20", 20, 10, 10, 10, "equal-10"),
    Row("controller-20", 10, 20, 10, 10, "equal-10"),
    Row("sensing-20", 20, 10, 20, 10, "plant-20"),
    Row("command-20", 20, 10, 10, 20, "plant-20"),
    Row("command-30", 20, 10, 10, 30, "plant-20"),
    Row("zero-sensing", 20, 10, 0, 20, "command-20"),
)
ROW = {row.name: row for row in ROWS}
# Each fault changes the independent FMPy execution of FAULT_ROW, never the
# SiL Run. The SiL Recording of FAULT_ROW has to be told apart from each one.
FAULT_ROW = ROW["zero-sensing"]
FAULTS = ("wrong-initialization", "wrong-zero-order", "one-period-shift")
WRONG_LEAD_START_M = 70.0


def varied(row: Row) -> str:
    """The one Period or Latency that separates a Row from its baseline."""
    if row.baseline is None:
        raise ValueError(f"{row.name} has no baseline")
    baseline = ROW[row.baseline]
    changed = [field.name for field in fields(Row)
               if field.name.endswith("_ms")
               and getattr(row, field.name) != getattr(baseline, field.name)]
    if len(changed) != 1:
        raise ValueError(f"{row.name} differs from {baseline.name} in {changed}")
    return changed[0]


def coupling(row: Row) -> dict:
    document = copy.deepcopy(json.loads(BASE.read_text()))
    document["duration_ns"] = DURATION_NS
    for name, period in row.periods().items():
        document["fmus"][name]["step_period_ns"] = period
        document["fmus"][name]["hold"] = []
        document["fmus"][name]["start"] = [
            {"variable": key, "value": str(value), "unit": UNITS[key]}
            for key, value in INITIAL[name].items()
        ]
    for name, latency in row.latencies().items():
        document["channels"][name]["latency_ns"] = latency
        for route in document["channels"][name]["subscribers"].values():
            route["capacity"] = 8
    return document


def publication_slots(row: Row, channel: str) -> range:
    return range(0, DURATION_NS, row.publication_period(channel))


def sample_times(row: Row, channel: str) -> range:
    """Every native Sample time, including Duration. No interpolation."""
    period = row.publication_period(channel)
    return range(period, DURATION_NS + 1, period)


def delivery_schedule(row: Row, channel: str) -> dict[int, int | None]:
    """Authored zero-order hold: newest visible publication or input start.

    A publication at p is visible to an activation at t >= p + Latency. At
    zero Latency p == t is one shared Slot, so the publication is visible only
    when its publisher runs first. Unchanged inputs remain held.
    """
    publisher = PUBLISHER[channel]
    subscriber = next(name for name, taken in INPUT.items() if taken == channel)
    source_period = row.periods()[publisher]
    latency = row.latencies()[channel]
    schedule = {}
    for activation in range(0, DURATION_NS, row.periods()[subscriber]):
        latest = (activation - latency) // source_period * source_period
        if latest == activation and SLOT_ORDER.index(publisher) > SLOT_ORDER.index(subscriber):
            latest -= source_period
        schedule[activation] = latest if latest >= 0 else None
    return schedule


def common_grid() -> range:
    return range(GRID_NS, DURATION_NS + 1, GRID_NS)


def declaration() -> dict:
    """The declared experiment, as pinned and as re-checked before any Run."""
    return {
        "rows": [asdict(row) for row in ROWS],
        "fault_row": FAULT_ROW.name, "faults": list(FAULTS),
        "duration_ns": DURATION_NS, "common_grid_ns": GRID_NS,
        "fields": {name: list(names) for name, names in FIELDS.items()},
        "atol": ATOL, "rtol": RTOL,
        "sensitivity_envelope": ENVELOPE, "initialization": INITIAL,
        "hold_policy": "latest delivered Message; input start until first delivery",
        "sample_time_mapping": "Sample time = publication Slot + publisher Period",
        "duration_policy": "half-open publication Slots; last Sample time equals Duration",
    }


def pinned_schedule(row: Row) -> dict:
    """The authored schedule as it is stored in the bundle."""
    return {channel: {str(t): publication
                      for t, publication in delivery_schedule(row, channel).items()}
            for channel in INPUT.values()}
