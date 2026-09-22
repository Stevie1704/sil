"""Independent FMPy driver for the changing-input sensitivity rows.

This file intentionally imports no SiL module, production dynamics function,
Manifest builder, Recording reader, or comparison code. It models the
published Step contract, including the fixed-grid Maneuver participant.
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


def _maneuver_acceleration(segments: list[dict], time_ns: int) -> float:
    for segment in segments:
        if segment["start_ns"] <= time_ns < segment["end_ns"]:
            return segment["lead_accel_mps2"]
    return segments[-1]["lead_accel_mps2"]


def _step(fmu: FMU3Slave, now_ns: int, period_ns: int) -> None:
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


def run(row: dict, config: dict, segments: list[dict]) -> dict:
    periods = row["periods_ns"]
    latencies = row["latencies_ns"]
    plant_period = periods["plant"]
    controller_period = periods["controller"]
    maneuver_period = periods["maneuver"]
    duration_ns = row["duration_ns"]
    closed_loop = controller_period is not None
    with ExitStack() as stack:
        instances = {}
        for model in (["AccPlant", "AccController"] if closed_loop else ["AccPlant"]):
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
            refs = {
                variable.name: variable.valueReference
                for variable in description.modelVariables
            }
            if model == "AccPlant":
                fmu.setFloat64(
                    [refs["accel_mps2"], refs["lead_accel_mps2"],
                     refs["initial_lead_position_m"]],
                    [row["initial_command_mps2"],
                     _maneuver_acceleration(segments, 0),
                     config["initial_state"]["lead_position_m"]],
                )
            else:
                initial = config["initial_outputs"]["sensing"]
                fmu.setFloat64(
                    [refs[name] for name in SENSING_FIELDS],
                    [initial[name] for name in SENSING_FIELDS],
                )
            fmu.enterInitializationMode(
                startTime=0.0, stopTime=duration_ns / 1e9,
            )
            fmu.exitInitializationMode()
            instances[model] = (fmu, refs)

        plant, plant_refs = instances["AccPlant"]
        initialization = {
            "state": _values(plant, plant_refs, STATE_FIELDS),
        }
        queues = {"maneuver": [], "sensing": [], "command": []}
        held_command = row["initial_command_mps2"]
        held_maneuver = _maneuver_acceleration(segments, 0)
        messages = {"maneuver": [], "state": []}
        if closed_loop:
            controller, controller_refs = instances["AccController"]
            initialization.update({
                "sensing": _values(plant, plant_refs, SENSING_FIELDS),
                "command": _values(controller, controller_refs, ["accel_mps2"]),
            })
            held_sensing = initialization["sensing"]
            messages.update({"sensing": [], "command": []})
            next_controller = 0
        else:
            controller = controller_refs = None
            next_controller = None
        next_maneuver = 0
        next_plant = 0
        while (
            next_maneuver < duration_ns
            or next_plant < duration_ns
            or (next_controller is not None and next_controller < duration_ns)
        ):
            due = [next_maneuver, next_plant]
            if next_controller is not None:
                due.append(next_controller)
            now_ns = min(value for value in due if value < duration_ns)
            if next_maneuver == now_ns:
                value = _maneuver_acceleration(segments, now_ns)
                messages["maneuver"].append({
                    "publish_ns": now_ns,
                    "endpoint_ns": now_ns,
                    "values": [value],
                })
                queues["maneuver"].append(
                    (now_ns + (latencies["maneuver"] or 0), now_ns, [value])
                )
                next_maneuver += maneuver_period
            if next_plant == now_ns:
                delivered = _drain(queues["maneuver"], now_ns)
                if delivered is not None:
                    held_maneuver = delivered[0]
                delivered = _drain(queues["command"], now_ns)
                if delivered is not None:
                    held_command = delivered[0]
                plant.setFloat64(
                    [plant_refs["accel_mps2"], plant_refs["lead_accel_mps2"]],
                    [held_command, held_maneuver],
                )
                _step(plant, now_ns, plant_period)
                state = _values(plant, plant_refs, STATE_FIELDS)
                endpoint_ns = now_ns + plant_period
                messages["state"].append({
                    "publish_ns": now_ns,
                    "endpoint_ns": endpoint_ns,
                    "values": state,
                })
                if closed_loop:
                    sensing = _values(plant, plant_refs, SENSING_FIELDS)
                    messages["sensing"].append({
                        "publish_ns": now_ns,
                        "endpoint_ns": endpoint_ns,
                        "values": sensing,
                    })
                    queues["sensing"].append(
                        (now_ns + (latencies["sensing"] or 0), now_ns, sensing)
                    )
                next_plant += plant_period
            if closed_loop and next_controller == now_ns:
                delivered = _drain(queues["sensing"], now_ns)
                if delivered is not None:
                    held_sensing = delivered
                controller.setFloat64(
                    [controller_refs[name] for name in SENSING_FIELDS],
                    held_sensing,
                )
                _step(controller, now_ns, controller_period)
                command = _values(controller, controller_refs, ["accel_mps2"])
                endpoint_ns = now_ns + controller_period
                messages["command"].append({
                    "publish_ns": now_ns,
                    "endpoint_ns": endpoint_ns,
                    "values": command,
                })
                queues["command"].append(
                    (now_ns + (latencies["command"] or 0), now_ns, command)
                )
                next_controller += controller_period
        if closed_loop:
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
    trace = run(
        _row(document, row_name), document, document["maneuver"]["segments"],
    )
    Path(output).write_text(json.dumps(trace, indent=2, allow_nan=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
