"""Run one experiment through the ADAS reference FMU with FMPy, not SiL.

This is the independent FMI execution of issue #226. It imports nothing
from SiL: it reads the expanded maneuver (`<maneuver>.inputs.csv`, which
`prepare.py` writes) and a JSON declaration of the case, applies the
declared faults itself, drives the FMU with FMPy and writes the Commands as
a CSV in the form of `maneuvers/<name>.expected.csv`.

The case declaration is
`{"interceptors": {"<role>": [...]}, "input_latency_ns": N, "start": [...]}`.
Its faults are read as the SiL Manifest documents them, by this module's own
code:

- `drop`: an observation published in [start_ns, end_ns) is not delivered;
- `override`: an observation published in [start_ns, end_ns) is delivered
  with `field` set to `value`;
- `input_latency_ns`: an observation published at t is delivered at the
  activation t + latency.

Each activation at t sets the inputs of every observation delivered at t,
radar, camera, then ego, steps the FMU over [t, t + 10 ms] and reads the
Command. More than one observation of one sensor at one activation is outside
the FMU interface and an error here.

    python independent.py AdasReference.fmu MANEUVER.inputs.csv CASE.json OUT.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path

from fmpy import extract, read_model_description
from fmpy.fmi3 import FMU3Slave

PERIOD_NS = 10_000_000
DURATION_NS = 200_000_000
CAPACITY = 8
SENSORS = ("radar", "camera", "ego")
HEADER = {"radar": ("sensor_id", "frame_id", "sequence", "count", "validity"),
          "camera": ("sensor_id", "frame_id", "sequence", "count", "validity"),
          "ego": ("sequence", "validity", "speed_mps")}
ARRAYS = ("object_id", "x_m", "y_m", "relative_vx_mps", "confidence")
OUTPUTS = ("sequence", "mode", "selected_object_id",
           "target_acceleration_mps2", "acceleration_mps2", "radar_age_ns",
           "camera_age_ns", "ego_age_ns", "ignored_observations")


class OutsideInterface(ValueError):
    """The case delivers what the FMU interface cannot take."""


def observations(expanded: Path) -> list[tuple[int, str, dict]]:
    """Each authored observation: its publication time, its sensor and its
    variable values, from the expanded maneuver."""
    with expanded.open(newline="") as f:
        rows = list(csv.DictReader(f))
    found = []
    for row in rows:
        t_ns = int(row["time_ms"]) * 1_000_000
        for sensor in SENSORS:
            if row[f"{sensor}_t_ns"] == "":
                continue
            values = {"sample_time_ns": [int(row[f"{sensor}_t_ns"])]}
            for name in HEADER[sensor]:
                values[name] = [row[f"{sensor}_{name}"]]
            if sensor != "ego":
                for name in ARRAYS:
                    values[name] = [row[f"{sensor}_{name}_{i}"]
                                    for i in range(CAPACITY)]
            found.append((t_ns, sensor, values))
    return found


def faulted(found: list, case: dict) -> list[tuple[int, str, dict]]:
    """The observations as the declared faults deliver them: (delivery time,
    sensor, values)."""
    delivered = []
    for t_ns, sensor, values in found:
        values = dict(values)
        dropped = False
        for fault in case["interceptors"].get(sensor, []):
            if not fault["start_ns"] <= t_ns < fault["end_ns"]:
                continue
            if fault["kind"] == "drop":
                dropped = True
            elif fault["kind"] == "override":
                values[fault["field"]] = [fault["value"]]
            else:
                raise OutsideInterface(f"fault kind {fault['kind']!r}")
        arrival = t_ns + case["input_latency_ns"]
        if not dropped and arrival < DURATION_NS:
            delivered.append((arrival, sensor, values))
    return delivered


class Fmu:
    """One FMPy instance of the archive."""

    def __init__(self, archive: Path, scratch: Path):
        self.description = read_model_description(str(archive))
        self.variables = {v.name: v for v in self.description.modelVariables}
        self.slave = FMU3Slave(
            guid=self.description.instantiationToken,
            unzipDirectory=extract(str(archive), unzipdir=str(scratch)),
            modelIdentifier=self.description.coSimulation.modelIdentifier,
            instanceName="independent")
        self.slave.instantiate(loggingOn=False)

    def set(self, name: str, values: list) -> None:
        variable = self.variables[name]
        parse = float if variable.type.startswith("Float") else int
        getattr(self.slave, f"set{variable.type}")(
            [variable.valueReference], [parse(v) for v in values])

    def get(self, name: str):
        variable = self.variables[name]
        count = math.prod(d.start for d in variable.dimensions)
        return getattr(self.slave, f"get{variable.type}")(
            [variable.valueReference], count)[0]


def run(archive: Path, expanded: Path, case: dict, scratch: Path) -> list:
    """The Command rows of every activation, 0 ms to 190 ms."""
    delivered = faulted(observations(expanded), case)
    fmu = Fmu(archive, scratch)
    for start in case["start"]:
        name, value = start.split("=", 1)
        fmu.set(name, [value])
    fmu.slave.enterInitializationMode(startTime=0.0)
    fmu.slave.exitInitializationMode()
    rows = []
    for t_ns in range(0, DURATION_NS, PERIOD_NS):
        now = [(sensor, values) for arrival, sensor, values in delivered
               if arrival == t_ns]
        sensors = [sensor for sensor, _ in now]
        if len(set(sensors)) != len(sensors):
            raise OutsideInterface(
                f"t={t_ns} ns delivers {sensors}: at most one observation "
                f"per sensor per step")
        for sensor in SENSORS:
            for got, values in now:
                if got == sensor:
                    for name, value in values.items():
                        fmu.set(f"{sensor}.{name}", value)
        fmu.slave.doStep(t_ns / 1e9, PERIOD_NS / 1e9)
        sample_ns = fmu.get("command.sample_time_ns")
        rows.append({"time_ms": _ms(sample_ns),
                     **{name: fmu.get(f"command.{name}") for name in OUTPUTS}})
    fmu.slave.terminate()
    fmu.slave.freeInstance()
    return rows


def _ms(ns: int) -> int:
    if ns % 1_000_000:
        raise ValueError(f"Sample time {ns} ns is not a whole millisecond")
    return ns // 1_000_000


def write(rows: list[dict], out: Path) -> None:
    with out.open("w", newline="") as f:
        writer = csv.DictWriter(f, ("time_ms", *OUTPUTS), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: repr(v) if isinstance(v, float) else v
                             for k, v in row.items()})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("archive", type=Path)
    parser.add_argument("expanded", type=Path)
    parser.add_argument("case", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    scratch = args.out.with_suffix(".fmu-extracted")
    try:
        rows = run(args.archive, args.expanded,
                   json.loads(args.case.read_text()), scratch)
    except OutsideInterface as error:
        sys.exit(f"independent.py: {error}")
    write(rows, args.out)
