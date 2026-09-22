"""Independent FMPy coupling driver. No SiL, routing or comparison imports."""
import json
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

from fmpy import extract, read_model_description
from fmpy.fmi3 import FMU3Slave


def run(mode="nominal"):
    h = 0.01
    trace = []
    with ExitStack() as stack:
        instances = {}
        for model in ("AccController", "AccPlant"):
            archive = f"/fmus/{model}.fmu"
            description = read_model_description(archive, validate=True)
            directory = stack.enter_context(tempfile.TemporaryDirectory())
            extract(archive, unzipdir=directory)
            fmu = FMU3Slave(guid=description.guid, unzipDirectory=directory,
                           modelIdentifier=description.coSimulation.modelIdentifier,
                           instanceName=model)
            fmu.instantiate()
            stack.callback(fmu.freeInstance)
            refs = {v.name: v.valueReference for v in description.modelVariables}
            fmu.enterInitializationMode(startTime=0, stopTime=5)
            fmu.exitInitializationMode()
            instances[model] = fmu, refs
        controller, cr = instances["AccController"]
        plant, pr = instances["AccPlant"]
        sensing_names = ["gap_m", "relative_speed_mps", "ego_speed_mps"]
        state_names = ["ego_position_m", "lead_position_m", "lead_speed_mps"]
        def get(names):
            return plant.getFloat64([pr[n] for n in names])
        sensing = get(sensing_names)
        previous_sensing = sensing
        command = 0.5 if mode == "initial-command" else 0.0
        previous_command = command
        initial = dict(sensing=sensing, state=get(state_names),
                       command=controller.getFloat64([cr["accel_mps2"]]))
        for n in range(500):
            before = get(sensing_names)
            applied = previous_command if mode == "shift-command" else command
            plant.setFloat64([pr["accel_mps2"]], [applied])
            flags = plant.doStep(n * h, h)
            if any(flags[:3]):
                raise RuntimeError(f"plant doStep: {flags}")
            sampled = previous_sensing if mode == "shift-sensing" else sensing
            if mode == "original":
                sampled = previous_sensing
            controller.setFloat64([cr[k] for k in sensing_names], sampled)
            flags = controller.doStep(n * h, h)
            if any(flags[:3]):
                raise RuntimeError(f"controller doStep: {flags}")
            previous_command = command
            command = controller.getFloat64([cr["accel_mps2"]])[0]
            if mode == "original" and n == 0:
                command = 0.0
            trace.append(dict(slot_ns=n * 10_000_000,
                              communication_ns=(n + 1) * 10_000_000,
                              sensing=before if mode == "original" else get(sensing_names),
                              state=get(state_names), command=[command],
                              sampled=sampled, applied=[applied]))
            previous_sensing = before if mode == "original" else sensing
            sensing = get(sensing_names)
        controller.terminate()
        plant.terminate()
    return dict(initialization=initial, samples=trace)


if __name__ == "__main__":
    Path(sys.argv[2]).write_text(json.dumps(run(sys.argv[1]), indent=2, allow_nan=False) + "\n")
