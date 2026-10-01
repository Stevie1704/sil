"""Run one closed-loop case through FMPy, not SiL (issue #227).

The independent execution path of the mixed loop. It imports nothing from
SiL: it reads the case declaration that loop.py writes, drives the plant
archive (`AccPlant.fmu`) and the controller archive (`AdasReference.fmu`)
with FMPy, derives the sensor observations with its own code, and writes
one row per Sample time with the Command and the truth it describes.

Its schedule, implemented here from the declaration, is the one the SiL
Manifest declares:

- the truth at t is the plant's output after the step that ends at t; at
  t = 0 it is the plant's output after initialization, which must equal
  the declared initial truth;
- each sensor samples the truth at t when its Period divides t: the lead
  is one object at the gap, straight ahead, unless the Maneuver hides it;
  every float is rounded once to binary32;
- a sensor observation published at t is due at t + the sensor Latency; a
  dropped one (`drops`, half-open windows of publication time) is never
  delivered, but its sequence number is used;
- the controller steps [t, t + 10 ms] after the due observations are
  written, radar, camera, then ego;
- the plant steps [t, t + 10 ms] on the lead acceleration of the Maneuver
  at t and on the Command whose Sample time is t (the initial acceleration
  before the first one).

    python independent.py AccPlant.fmu AdasReference.fmu CASE.json OUT.csv \
        INITIAL.json
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import struct
import sys
from pathlib import Path

CAPACITY = 8
SENSORS = ("radar", "camera", "ego")
SENSOR_IDS = {"radar": 1, "camera": 2}
TRUTH = ("ego_position_m", "ego_speed_mps", "lead_position_m",
         "lead_speed_mps", "gap_m", "relative_speed_mps")
COMMAND = ("sequence", "mode", "selected_object_id",
           "target_acceleration_mps2", "acceleration_mps2", "radar_age_ns",
           "camera_age_ns", "ego_age_ns", "ignored_observations")


class Mismatch(RuntimeError):
    """The archives do not start where the declaration says."""


def binary32(value: float) -> float:
    """The nearest binary32; `struct` raises past its range."""
    if not math.isfinite(value):
        raise ValueError(f"{value!r} has no finite binary32")
    return struct.unpack("<f", struct.pack("<f", value))[0]


def within(windows: list, t: int) -> bool:
    return any(start <= t < end for start, end in windows)


def lead_accel(case: dict, t: int) -> float:
    return float(sum(a for start, end, a in case["lead_accel"]
                     if start <= t < end))


def observation(case: dict, sensor: str, t: int, sequence: int,
                truth: dict) -> dict:
    """The variable values of one sensor's observation at t."""
    if sensor == "ego":
        speed = binary32(truth["ego_speed_mps"])
        if speed < 0:
            raise ValueError(f"ego speed {speed!r} is negative; profile 3 "
                             "has no negative speed")
        return {"sample_time_ns": [t], "sequence": [sequence],
                "validity": [1], "speed_mps": [speed]}
    visible = not within(case["hidden"], t)
    obj = case["objects"][sensor]
    active = {"object_id": obj["object_id"],
              "x_m": binary32(truth["gap_m"]), "y_m": 0.0,
              "relative_vx_mps": (binary32(truth["relative_speed_mps"])
                                  if sensor == "radar" else 0.0),
              "confidence": binary32(obj["confidence"])}
    values = {"sample_time_ns": [t], "sensor_id": [SENSOR_IDS[sensor]],
              "frame_id": [1], "sequence": [sequence],
              "count": [int(visible)], "validity": [1]}
    for name, value in active.items():
        values[name] = ([value] if visible else []) + \
            [0] * (CAPACITY - int(visible))
    return values


def published(case: dict, t: int, sequences: dict, truth: dict) -> list:
    """The observations the sensors publish at t that reach the
    controller: (due time, sensor, values), in publication order."""
    out = []
    for sensor in SENSORS:
        if t % case["sensor_periods_ns"][sensor]:
            continue
        values = observation(case, sensor, t, sequences[sensor], truth)
        sequences[sensor] += 1
        if not within(case["drops"].get(sensor, []), t):
            out.append((t + case["sensor_latency_ns"], sensor, values))
    return out


class Fmu:
    """One FMPy instance of an archive."""

    def __init__(self, archive: Path, scratch: Path, name: str):
        from fmpy import extract, read_model_description
        from fmpy.fmi3 import FMU3Slave

        self.description = read_model_description(str(archive),
                                                  validate=True)
        self.variables = {v.name: v for v in self.description.modelVariables}
        self.slave = FMU3Slave(
            guid=self.description.instantiationToken,
            unzipDirectory=extract(str(archive), unzipdir=str(scratch)),
            modelIdentifier=self.description.coSimulation.modelIdentifier,
            instanceName=name)
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

    def step(self, t_ns: int, h_ns: int) -> None:
        flags = self.slave.doStep(t_ns / 1e9, h_ns / 1e9)
        if any(flags[:3]):
            raise RuntimeError(f"doStep at {t_ns} ns: {flags}")

    def close(self) -> None:
        self.slave.terminate()
        self.slave.freeInstance()


def run(plant_fmu: Path, controller_fmu: Path, case: dict,
        scratch: Path) -> tuple[list[dict], dict]:
    """One row per Sample time, 10 ms through the Duration, and the
    plant's initial outputs."""
    h = case["step_ns"]
    plant = Fmu(plant_fmu, scratch / "plant", "plant")
    controller = Fmu(controller_fmu, scratch / "controller", "controller")
    plant.set("initial_lead_position_m", [case["initial_lead_position_m"]])
    for start in case["controller_start"]:
        name, value = start.split("=", 1)
        controller.set(name, [value])
    for fmu in (plant, controller):
        fmu.slave.enterInitializationMode(startTime=0.0)
        fmu.slave.exitInitializationMode()
    truth = {name: plant.get(name) for name in TRUTH}
    initial = dict(truth)
    if initial != case["initial_truth"]:
        raise Mismatch(f"plant initial outputs {initial}, declared "
                       f"{case['initial_truth']}")
    sequences = dict.fromkeys(SENSORS, 0)
    pending: list = []
    commands: dict[int, float] = {}
    rows = []
    for t in range(0, case["duration_ns"], h):
        pending += published(case, t, sequences, truth)
        due = [p for p in pending if p[0] <= t]
        pending = [p for p in pending if p[0] > t]
        for sensor in SENSORS:
            for _, got, values in due:
                if got == sensor:
                    for name, value in values.items():
                        controller.set(f"{sensor}.{name}", value)
        controller.step(t, h)
        command = {name: controller.get(f"command.{name}")
                   for name in ("sample_time_ns", *COMMAND)}
        if command["sample_time_ns"] != t + h:
            raise RuntimeError(f"Command at {t} ns sampled at "
                               f"{command['sample_time_ns']} ns")
        commands[t + h] = command["acceleration_mps2"]
        plant.set("lead_accel_mps2", [lead_accel(case, t)])
        plant.set("accel_mps2", [commands[t] if t
                                 else case["initial_accel_mps2"]])
        plant.step(t, h)
        truth = {name: plant.get(name) for name in TRUTH}
        rows.append({"time_ms": (t + h) // 1_000_000,
                     **{n: command[n] for n in COMMAND},
                     **{f"truth.{n}": truth[n] for n in TRUTH}})
    plant.close()
    controller.close()
    return rows, initial


def write(rows: list[dict], out: Path) -> None:
    with out.open("w", newline="") as f:
        writer = csv.DictWriter(f, list(rows[0]), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: repr(v) if isinstance(v, float) else v
                             for k, v in row.items()})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("plant", type=Path)
    parser.add_argument("controller", type=Path)
    parser.add_argument("case", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("initial", type=Path)
    args = parser.parse_args()
    try:
        rows, initial = run(args.plant, args.controller,
                            json.loads(args.case.read_text()),
                            args.out.with_suffix(".fmu-extracted"))
    except Mismatch as error:
        sys.exit(f"independent.py: {error}")
    write(rows, args.out)
    args.initial.write_text(json.dumps(initial, indent=2) + "\n")
