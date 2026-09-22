"""Independent FMPy driver for changing-input sensitivity rows.

This file intentionally imports no SiL module, production dynamics function,
Manifest builder, Recording reader, or comparison code.  It models only the
published Step contract: fixed-period activations, FIFO visibility, and
post-step output publication.
"""
from __future__ import annotations

import json
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

from fmpy import extract, read_model_description
from fmpy.fmi3 import FMU3Slave

SENSING_FIELDS = ["gap_m", "relative_speed_mps", "ego_speed_mps"]
STATE_FIELDS = [
    "ego_position_m", "ego_speed_mps", "lead_position_m", "lead_speed_mps",
    "gap_m", "relative_speed_mps",
]


def _row(document: dict, name: str) -> dict:
    for row in document["rows"] + document["independent_reference_rows"]:
        if row["name"] == name:
            return row
    raise KeyError(name)


def _flags(fmu: FMU3Slave, now_ns: int, period_ns: int) -> None:
    flags = fmu.doStep(now_ns / 1e9, period_ns / 1e9)
    if any(flags[:3]):
        raise RuntimeError(f"unexpected FMI doStep flags at {now_ns} ns: {flags}")


def _values(fmu: FMU3Slave, refs: dict[str, int], names: list[str]) -> list[float]:
    return fmu.getFloat64([refs[name] for name in names])


def _drain(queue: list[tuple[int, int, list[float]]], now_ns: int):
    visible = []
    while queue and queue[0][0] <= now_ns:
        visible.append(queue.pop(0))
    return visible[-1][2] if visible else None


def run(row: dict) -> dict:
    if row["scenario"] != "changing-input":
        raise ValueError("the independent changing-input driver cannot run a constant row")
    periods = row["periods_ns"]
    latencies = row["latencies_ns"]
    plant_period = periods["plant"]
    controller_period = periods["controller"]
    duration_ns = row["duration_ns"]
    with ExitStack() as stack:
        instances = {}
        for model in ("AccPlant", "AccController"):
            archive = f"/fmus/{model}.fmu"
            description = read_model_description(archive, validate=True)
            directory = stack.enter_context(tempfile.TemporaryDirectory())
            extract(archive, unzipdir=directory)
            fmu = FMU3Slave(
                guid=description.guid,
                unzipDirectory=directory,
                modelIdentifier=description.coSimulation.modelIdentifier,
                instanceName=f"sensitivity-{model}",
            )
            fmu.instantiate()
            stack.callback(fmu.freeInstance)
            refs = {variable.name: variable.valueReference
                    for variable in description.modelVariables}
            if model == "AccPlant":
                fmu.setFloat64([refs["accel_mps2"]], [row["initial_command_mps2"]])
            fmu.enterInitializationMode(startTime=0.0, stopTime=duration_ns / 1e9)
            fmu.exitInitializationMode()
            instances[model] = (fmu, refs)

        plant, plant_refs = instances["AccPlant"]
        controller, controller_refs = instances["AccController"]
        initialization = {
            "state": _values(plant, plant_refs, STATE_FIELDS),
            "sensing": _values(plant, plant_refs, SENSING_FIELDS),
            "command": _values(controller, controller_refs, ["accel_mps2"]),
        }
        queues = {"sensing": [], "command": []}
        held_command = row["initial_command_mps2"]
        held_sensing = initialization["sensing"]
        messages = {"sensing": [], "state": [], "command": []}
        next_plant = next_controller = 0
        while next_plant < duration_ns or next_controller < duration_ns:
            now_ns = min(
                value for value in (next_plant, next_controller)
                if value < duration_ns
            )
            if next_plant == now_ns:
                delivered = _drain(queues["command"], now_ns)
                if delivered is not None:
                    held_command = delivered[0]
                plant.setFloat64([plant_refs["accel_mps2"]], [held_command])
                _flags(plant, now_ns, plant_period)
                state = _values(plant, plant_refs, STATE_FIELDS)
                sensing = _values(plant, plant_refs, SENSING_FIELDS)
                endpoint_ns = now_ns + plant_period
                messages["state"].append({
                    "publish_ns": now_ns,
                    "endpoint_ns": endpoint_ns,
                    "values": state,
                })
                messages["sensing"].append({
                    "publish_ns": now_ns,
                    "endpoint_ns": endpoint_ns,
                    "values": sensing,
                })
                queues["sensing"].append(
                    (now_ns + latencies["sensing"], now_ns, sensing)
                )
                next_plant += plant_period
            if next_controller == now_ns:
                delivered = _drain(queues["sensing"], now_ns)
                if delivered is not None:
                    held_sensing = delivered
                controller.setFloat64(
                    [controller_refs[name] for name in SENSING_FIELDS],
                    held_sensing,
                )
                _flags(controller, now_ns, controller_period)
                command = _values(controller, controller_refs, ["accel_mps2"])
                endpoint_ns = now_ns + controller_period
                messages["command"].append({
                    "publish_ns": now_ns,
                    "endpoint_ns": endpoint_ns,
                    "values": command,
                })
                queues["command"].append(
                    (now_ns + latencies["command"], now_ns, command)
                )
                next_controller += controller_period
        controller.terminate()
        plant.terminate()
    return {
        "row": row["name"],
        "duration_ns": duration_ns,
        "initialization": initialization,
        "messages": messages,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        raise SystemExit("usage: sensitivity_reference.py ROW OUTPUT CONFIGURATION")
    row_name, output, config_path = argv
    document = json.loads(Path(config_path).read_text())
    trace = run(_row(document, row_name))
    Path(output).write_text(json.dumps(trace, indent=2, allow_nan=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

