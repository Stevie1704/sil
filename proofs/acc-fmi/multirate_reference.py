"""Independent FMPy stepping and independently authored delivery schedule."""
from __future__ import annotations

import json
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

from fmpy import extract, read_model_description
from fmpy.fmi3 import FMU3Slave

from multirate_contract import (DURATION_NS, FIELDS, INITIAL, ROWS, CONTROLS,
                                delivery_schedule, publication_slots)


def run(row, archive_dir=Path('/fmus'), *, wrong_zero_order=False):
    schedules = {name: delivery_schedule(row, name) for name in ('sensing', 'command')}
    if wrong_zero_order:
        if row.sensing_ms != 0:
            raise ValueError("wrong-zero-order control requires zero sensing Latency")
        for activation, publication in schedules["sensing"].items():
            if publication == activation:
                prior = activation - row.plant_ms * 1_000_000
                schedules["sensing"][activation] = prior if prior >= 0 else None
    with ExitStack() as stack:
        instances = {}
        for name, model in (("plant", "AccPlant"), ("controller", "AccController")):
            archive = archive_dir / f"{model}.fmu"
            description = read_model_description(str(archive), validate=True)
            directory = stack.enter_context(tempfile.TemporaryDirectory())
            extract(str(archive), unzipdir=directory)
            if description.coSimulation is None:
                raise RuntimeError(f'{model}: Co-Simulation is missing')
            fmu = FMU3Slave(guid=description.guid, unzipDirectory=directory,
                            modelIdentifier=description.coSimulation.modelIdentifier,
                            instanceName=model)
            fmu.instantiate()
            stack.callback(fmu.freeInstance)
            refs = {v.name: v.valueReference for v in description.modelVariables}
            starts = INITIAL[name].copy()
            if name == "plant":
                starts["initial_lead_position_m"] = row.initial_lead_m
            for field, value in starts.items():
                fmu.setFloat64([refs[field]], [value])
            fmu.enterInitializationMode(startTime=0, stopTime=DURATION_NS / 1e9)
            fmu.exitInitializationMode()
            instances[name] = (fmu, refs)
        outputs = {name: {} for name in FIELDS}
        applied = {"sensing": {}, "command": {}}
        periods = row.periods()
        for slot in range(0, DURATION_NS, min(periods.values())):
            for name in ("plant", "controller"):
                period = periods[name]
                if slot % period:
                    continue
                fmu, refs = instances[name]
                incoming = "command" if name == "plant" else "sensing"
                publication = schedules[incoming][slot]
                if publication is not None:
                    values = outputs[incoming][publication]
                    for field, value in zip(FIELDS[incoming], values):
                        fmu.setFloat64([refs[field]], [value])
                applied[incoming][slot] = publication
                flags = fmu.doStep(slot / 1e9, period / 1e9)
                if any(flags[:3]):
                    raise RuntimeError(f"{name} doStep at {slot}: {flags}")
                for channel in (("sensing", "state") if name == "plant" else ("command",)):
                    outputs[channel][slot] = list(fmu.getFloat64([refs[field] for field in FIELDS[channel]]))
        for fmu, _ in instances.values():
            fmu.terminate()
    return {"row": row.__dict__, "duration_ns": DURATION_NS,
            "schedule": applied, "outputs": outputs,
            "publication_slots": {channel: list(publication_slots(row, channel))
                                  for channel in FIELDS}}


if __name__ == '__main__':
    name, path = sys.argv[1:3]
    row = next(row for row in (*ROWS, *CONTROLS) if row.name == name)
    Path(path).write_text(json.dumps(run(row, wrong_zero_order=len(sys.argv) > 3 and sys.argv[3] == "wrong-zero-order"), indent=2, allow_nan=False) + '\n')
