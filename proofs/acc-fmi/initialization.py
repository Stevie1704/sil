"""Retain the SiL Importer's initialization values, before the first Step.

Without --resources this also reproduces the pre-fix null-resource-path failure
on the unmodified PythonFMU3 archive. The full Run gate tests caller wiring.
"""
import argparse
import json
from pathlib import Path

from sil.fmi import CoSimulation, ModelDescription
from sil.fmi.archive import Extraction
from sil.fmi.runtime import ScalarBuffer
from cases import CASES, INPUTS, OUTPUTS
from expected import check, expected


def run(case_name, output, resources):
    case = CASES[case_name]
    model = case["model"]
    trace = []
    for i, spec in enumerate(case["instances"]):
        extraction = Extraction()
        fmu = None
        try:
            root = extraction.unpack(Path(f"/fmus/{model}.fmu"))
            description = ModelDescription.read(root)
            kwargs = {"resource_path": root / "resources"} if resources else {}
            fmu = CoSimulation(description.binary(root), description, **kwargs)
            inputs = ScalarBuffer("Float64", [description.variables[n].reference for n in INPUTS[model]])
            outputs = ScalarBuffer("Float64", [description.variables[n].reference for n in OUTPUTS[model]])
            inputs.write(fmu, spec["start"])
            fmu.initialize()
            values = outputs.read(fmu)
            check(values, expected(model, spec["start"], 0.0), "SiL initialized")
            trace.append(dict(instance=i, phase="initialized", time=0.0, values=values))
        finally:
            if fmu is not None:
                fmu.close()
            extraction.cleanup()
    output.write_text(json.dumps(trace, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("case")
    parser.add_argument("output", type=Path)
    parser.add_argument("--resources", action="store_true")
    args = parser.parse_args()
    run(args.case, args.output, args.resources)
