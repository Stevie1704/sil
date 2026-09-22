"""Independent FMI lifecycle, maneuver qualification and FIFO fault policy.

No SiL, production dynamics, scenario authoring or KPI imports.
"""
import json
import math
import sys
import tempfile
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from fmpy import extract, read_model_description
from fmpy.fmi3 import FMU3Slave


@contextmanager
def instance(model, duration):
    archive = f"/fmus/{model}.fmu"
    description = read_model_description(archive, validate=True)
    with tempfile.TemporaryDirectory() as directory:
        extract(archive, unzipdir=directory)
        fmu = FMU3Slave(guid=description.guid, unzipDirectory=directory,
                       modelIdentifier=description.coSimulation.modelIdentifier,
                       instanceName=model)
        fmu.instantiate()
        try:
            fmu.enterInitializationMode(startTime=0, stopTime=duration)
            fmu.exitInitializationMode()
            yield fmu, {v.name: v.valueReference for v in description.modelVariables}
            fmu.terminate()
        finally:
            fmu.freeInstance()


def step(fmu, t, h):
    flags = fmu.doStep(t, h)
    if any(flags[:3]):
        raise RuntimeError(f"unexpected doStep result: {flags}")


def qualify():
    """Analytic two-segment initial-value motion, including release/input hold."""
    rows = []
    with instance("AccPlant", 2) as (plant, refs):
        names = ["ego_position_m", "ego_speed_mps", "lead_position_m", "lead_speed_mps"]
        initial = plant.getFloat64([refs[n] for n in names])
        if initial != [0, 25, 60, 25]:
            raise RuntimeError(f"maneuver initialization: {initial}")
        for i in range(200):
            if i == 0:
                plant.setFloat64([refs["accel_mps2"], refs["lead_accel_mps2"]], [1.5, -4])
            elif i == 100:
                plant.setFloat64([refs["lead_accel_mps2"]], [0])
            step(plant, i * .01, .01)
            t = (i + 1) * .01
            expected = [25*t + .75*t*t, 25 + 1.5*t,
                        60 + 25*t - 2*t*t if t <= 1 else 83 + 21*(t-1),
                        25 - 4*t if t <= 1 else 21]
            actual = plant.getFloat64([refs[n] for n in names])
            for name, a, e in zip(names, actual, expected):
                if not math.isfinite(a) or not math.isclose(a, e, abs_tol=1e-10, rel_tol=1e-12):
                    raise RuntimeError(f"qualification {name} time={t} value={a} expected={e}")
            rows.append(dict(time=t, actual=actual, expected=expected))
    return dict(initialization=initial, intervals=rows)


def run(config):
    h_ns, count = config["step_ns"], config["steps"]
    h = h_ns / 1e9
    rows, pending = [], deque()
    names = ["gap_m", "relative_speed_mps", "ego_speed_mps",
             "ego_position_m", "lead_position_m", "lead_speed_mps"]
    with instance("AccPlant", count*h) as (plant, pr), instance("AccController", count*h) as (controller, cr):
        initial = plant.getFloat64([pr[n] for n in names])
        sampled = initial[:3]
        sampled_source_ns = None
        command, last_delivery_ns = 0.0, 0
        for i in range(count):
            t = i*h_ns
            m = config["maneuver"]
            lead = m["lead_accel_mps2"] if m["start_ns"] <= t < m["end_ns"] else 0.0
            plant.setFloat64([pr["accel_mps2"], pr["lead_accel_mps2"]], [command, lead])
            step(plant, i*h, h)
            truth = plant.getFloat64([pr[n] for n in names])
            visible, dropped = t, False
            f = config["fault"]
            if f and f["start_ns"] <= t < f["end_ns"]:
                dropped = f["kind"] == "drop"
                if f["kind"] == "delay":
                    visible += f["delay_ns"]
            if not dropped and visible < count*h_ns:
                pending.append((visible + h_ns, truth[:3], t))
            while pending and pending[0][0] <= t:
                _, sampled, sampled_source_ns = pending.popleft()
                last_delivery_ns = t
            controller.setFloat64([cr[n] for n in names[:3]], sampled)
            step(controller, i*h, h)
            command = controller.getFloat64([cr["accel_mps2"]])[0]
            rows.append(dict(slot_ns=t, truth=truth, command=[command], maneuver=[lead],
                             sensing=None if dropped else truth[:3], sensing_publication_ns=visible,
                             sampled=sampled, sampled_source_ns=sampled_source_ns,
                             freshness=[float(t-last_delivery_ns)]))
    return dict(initialization=initial, step_ns=h_ns, steps=count, intervals=rows)


if __name__ == "__main__":
    config = json.loads(Path(sys.argv[1]).read_text())
    result = dict(qualification=qualify(), trajectory=run(config))
    Path(sys.argv[2]).write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
