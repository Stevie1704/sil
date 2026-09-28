"""Check the FMU's initialization and shutdown outside a Run (#194).

`sil-fmi-inspect` cannot verify statically that the FMU accepts its start
values, that it initializes from the files in `resources/`, or that it
terminates. This drives the installed importer's own FMI calls directly, one
lifecycle per process, and writes each phase it completes:

    lifecycle.py <fmu> <out.json> [--no-resources] --start <variable>=<value> ...

With the resource path, the FMU must instantiate, take the start values,
initialize, step one period, and then terminate and be freed without an
error. Without it, PythonFMU3 cannot find its model source, and
instantiation must fail: the phases then end at `described`, which shows the
initialization depends on the resources and that the importer supplies them.

`sil.fmi.CoSimulation` has no public value accessor, so the start values and
the command go through `ScalarBuffer`, the buffer the importer itself uses.
That module is not covered by the compatibility policy; the proof's workflow
therefore runs on every change to `sil.fmi`, so a change there fails here
instead of drifting.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import zipfile
from pathlib import Path

from controller_replay import INPUTS, OUTPUT, PERIOD_NS

from sil.fmi import CoSimulation, ModelDescription
from sil.fmi.runtime import ScalarBuffer


def lifecycle(archive: Path, starts: dict[str, float], resources: bool,
              phases: list[dict]) -> None:
    with tempfile.TemporaryDirectory() as unpacked:
        root = Path(unpacked)
        with zipfile.ZipFile(archive) as opened:
            opened.extractall(root)
        description = ModelDescription.read(root)
        phases.append({"phase": "described"})
        fmu = CoSimulation(description.binary(root), description,
                           resource_path=root / "resources" if resources else None)
        try:
            phases.append({"phase": "instantiated"})
            references = description.variables
            inputs = ScalarBuffer("Float64", [references[n].reference for n in INPUTS])
            output = ScalarBuffer("Float64", [references[OUTPUT].reference])
            inputs.write(fmu, [starts[n] for n in INPUTS])
            fmu.initialize()
            phases.append({"phase": "initialized", OUTPUT: output.read(fmu)[0]})
            if fmu.do_step(0.0, PERIOD_NS / 1e9):
                raise RuntimeError("the FMU asked for an event in its first Step")
            phases.append({"phase": "stepped", "t_ns": PERIOD_NS,
                           OUTPUT: output.read(fmu)[0]})
            fmu, closing = None, fmu
            closing.close()
            phases.append({"phase": "terminated"})
        finally:
            if fmu is not None:
                fmu.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("fmu", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--no-resources", action="store_true")
    parser.add_argument("--start", action="append", default=[])
    args = parser.parse_args()
    starts = {name: float(value) for name, value in
              (item.split("=", 1) for item in args.start)}
    phases: list[dict] = []
    try:
        lifecycle(args.fmu, starts, not args.no_resources, phases)
    finally:
        # The phases reached are kept on failure too.
        args.output.write_text(json.dumps(phases, indent=2) + "\n")


if __name__ == "__main__":
    main()
