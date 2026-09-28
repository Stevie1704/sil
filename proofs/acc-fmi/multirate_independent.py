"""Independent FMPy stepping with its own Message delivery (#197).

An activation takes the newest publication that already exists and whose
Latency has passed. This does not use ``delivery_schedule``: the preparation
step requires both derivations to agree before it pins the bundle.
"""
from __future__ import annotations

import copy
import tempfile
from contextlib import ExitStack
from dataclasses import asdict

from fmpy import extract, read_model_description
from fmpy.fmi3 import FMU3Slave

from multirate_contract import (DURATION_NS, FAULTS, FIELDS, FMU_DIR, INITIAL,
                                INPUT, MODELS, PUBLISHER, SLOT_ORDER,
                                WRONG_LEAD_START_M, publication_slots)


def _instantiate(stack, name, starts):
    model = MODELS[name]
    archive = FMU_DIR / f"{model}.fmu"
    description = read_model_description(str(archive), validate=True)
    if description.coSimulation is None:
        raise RuntimeError(f'{model}: Co-Simulation is missing')
    directory = stack.enter_context(tempfile.TemporaryDirectory())
    extract(str(archive), unzipdir=directory)
    fmu = FMU3Slave(guid=description.guid, unzipDirectory=directory,
                    modelIdentifier=description.coSimulation.modelIdentifier,
                    instanceName=model)
    fmu.instantiate()
    stack.callback(fmu.freeInstance)
    refs = {v.name: v.valueReference for v in description.modelVariables}
    for field, value in starts.items():
        fmu.setFloat64([refs[field]], [value])
    fmu.enterInitializationMode(startTime=0, stopTime=DURATION_NS / 1e9)
    fmu.exitInitializationMode()
    return fmu, refs


def run(row, fault=None):
    """Step both archives for one Row, optionally with one declared fault."""
    if fault not in (None, *FAULTS):
        raise ValueError(f"unknown fault {fault!r}")
    starts = copy.deepcopy(INITIAL)
    if fault == "wrong-initialization":
        starts["plant"]["initial_lead_position_m"] = WRONG_LEAD_START_M
    # The wrong order runs the controller before the plant in a shared Slot.
    order = SLOT_ORDER[::-1] if fault == "wrong-zero-order" else SLOT_ORDER
    delays = row.latencies()
    if fault == "one-period-shift":
        delays["command"] += row.periods()["plant"]
    periods = row.periods()
    outputs = {channel: {} for channel in FIELDS}
    applied = {channel: {} for channel in INPUT.values()}
    with ExitStack() as stack:
        instances = {name: _instantiate(stack, name, starts[name]) for name in SLOT_ORDER}
        for slot in range(0, DURATION_NS, min(periods.values())):
            for name in order:
                if slot % periods[name]:
                    continue
                fmu, refs = instances[name]
                channel = INPUT[name]
                publication = max((p for p in outputs[channel]
                                   if p + delays[channel] <= slot), default=None)
                if publication is not None:
                    for field, value in zip(FIELDS[channel], outputs[channel][publication]):
                        fmu.setFloat64([refs[field]], [value])
                applied[channel][slot] = publication
                flags = fmu.doStep(slot / 1e9, periods[name] / 1e9)
                if any(flags[:3]):
                    raise RuntimeError(f"{name} doStep at {slot}: {flags}")
                for published in (ch for ch, publisher in PUBLISHER.items() if publisher == name):
                    outputs[published][slot] = list(
                        fmu.getFloat64([refs[field] for field in FIELDS[published]]))
        for fmu, _ in instances.values():
            fmu.terminate()
    return {"row": asdict(row), "fault": fault, "duration_ns": DURATION_NS,
            "schedule": applied, "outputs": outputs,
            "publication_slots": {channel: list(publication_slots(row, channel))
                                  for channel in FIELDS}}
