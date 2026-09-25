"""Run the example on the bus FMU through FMPy alone, with no SiL code.

    python independent.py EXAMPLE.json SilCanBus.fmu OUTPUT.json

This event-driven FMI 3.0 master stops at every request instant and at every
countdown instant that the bus gives, then records every Tx activation. It shares
only `scenario.py` with the SiL path: the configuration, not the execution.
The caller sets the deadline.
"""

import ctypes
import hashlib
import json
import platform
import shutil
import sys
from pathlib import Path

import fmpy
from fmpy import extract, read_model_description
from fmpy.fmi3 import FMU3Slave

import scenario

NS = 1_000_000_000
CHANGED, NOT_YET_KNOWN = 2, 0


def run(config, archive):
    limits = scenario.packaged_limits(archive)
    inputs = scenario.requests(config, limits)
    description = read_model_description(archive, validate=True)
    references = {v.name: v.valueReference for v in description.modelVariables}
    unpacked = extract(archive)
    bus = FMU3Slave(
        guid=description.guid,
        unzipDirectory=unpacked,
        modelIdentifier=description.coSimulation.modelIdentifier,
        instanceName="bus",
    )
    bus.instantiate(eventModeUsed=True)
    try:
        assignments = [
            start.removeprefix("bus.").split("=")
            for start in scenario.start_values(config)
        ]
        bus.setFloat64([references[name] for name, _ in assignments],
                       [float(value) for _, value in assignments])
        bus.enterInitializationMode(startTime=0)
        bus.exitInitializationMode()
        nodes = len(config["nodes"])
        terminal = [
            {member: references[f"Node{n + 1}.{member}"]
             for member in ("Rx_Data", "Tx_Data", "Rx_Clock", "Tx_Clock")}
            for n in range(nodes)
        ]
        return drive(bus, terminal, inputs, config["duration_ns"])
    finally:
        bus.freeInstance()
        shutil.rmtree(unpacked)


def drive(bus, terminal, inputs, until_ns):
    trace, now, due = [], 0, None
    while True:
        for _, node, buffer in (i for i in inputs if i[0] == now):
            bus.setClock([terminal[node]["Rx_Clock"]], [True])
            bus.setBinary([terminal[node]["Rx_Data"]], [buffer])
        if due == now:
            bus.setClock([t["Tx_Clock"] for t in terminal], [True] * len(terminal))
            outputs = bus.getBinary([t["Tx_Data"] for t in terminal])
            trace += [[now, n + 1, data.hex()] for n, data in enumerate(outputs) if data]
        bus.updateDiscreteStates()
        due = next_bus_event(bus, terminal, now, due)
        if due is not None and due <= now:
            raise RuntimeError(f"the bus stated no future event at {now} ns")
        if now == until_ns:
            return sorted(trace)
        stops = [i[0] for i in inputs if i[0] > now] + [until_ns]
        end = min(stops + ([due] if due is not None else []))
        bus.enterStepMode()
        bus.doStep(currentCommunicationPoint=now / NS,
                   communicationStepSize=(end - now) / NS)
        bus.enterEventMode()
        now = end


def next_bus_event(bus, terminal, now, due):
    """All active Tx Clocks state one countdown; keep it until it changes."""
    count = len(terminal)
    refs = (ctypes.c_uint32 * count)(*(t["Tx_Clock"] for t in terminal))
    counters = (ctypes.c_uint64 * count)()
    resolutions = (ctypes.c_uint64 * count)()
    qualifiers = (ctypes.c_int * count)()
    bus.fmi3GetIntervalFraction(bus.component, refs, count, counters,
                                resolutions, qualifiers)
    if len(set(qualifiers)) != 1 or len(set(counters)) != 1:
        raise RuntimeError("active Tx Clocks disagree on the next bus event")
    if qualifiers[0] == CHANGED:
        if resolutions[0] != NS:
            raise RuntimeError("the bus stated a countdown not in nanoseconds")
        return now + counters[0]
    return None if qualifiers[0] == NOT_YET_KNOWN else due


def main(config_path, archive_path, output_path):
    archive = Path(archive_path)
    trace = run(scenario.load(config_path), archive)
    Path(output_path).write_text(json.dumps({
        "execution_path": "FMPy FMI 3.0 calls, no SiL Importer or runner",
        "reference_tool": {"fmpy": fmpy.__version__,
                           "python": platform.python_version()},
        "fmu_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "trace": trace,
    }, indent=2) + "\n")


if __name__ == "__main__":
    main(*sys.argv[1:])
