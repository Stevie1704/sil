"""Independent FMPy coupling driver. No SiL, routing or comparison imports."""
import json
import sys
import tempfile
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path

from fmpy import extract, read_model_description
from fmpy.fmi3 import FMU3Slave


@dataclass(frozen=True)
class Coupling:
    sensing_age: int = 0
    command_age: int = 1
    initial_command: float = 0.0
    sensing_at_interval_start: bool = False
    first_command_publication: int = 0


COUPLINGS = {
    "nominal": Coupling(),
    "shift-command": Coupling(command_age=2),
    "shift-sensing": Coupling(sensing_age=1),
    "initial-command": Coupling(initial_command=0.5),
    "original": Coupling(sensing_age=1, sensing_at_interval_start=True,
                         first_command_publication=1),
}


def run(mode, configuration):
    coupling = COUPLINGS[mode]
    step_ns, steps = configuration["step_ns"], configuration["steps"]
    if type(step_ns) is not int or step_ns <= 0 or type(steps) is not int or steps <= 0:
        raise ValueError("communication period and interval count must be positive integers")
    step_seconds = step_ns / 1e9
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
            references = {v.name: v.valueReference for v in description.modelVariables}
            if model == "AccPlant":
                fmu.setFloat64([references["accel_mps2"]], [coupling.initial_command])
            fmu.enterInitializationMode(startTime=0, stopTime=steps * step_seconds)
            fmu.exitInitializationMode()
            instances[model] = fmu, references
        controller, controller_refs = instances["AccController"]
        plant, plant_refs = instances["AccPlant"]
        sensing_names = ["gap_m", "relative_speed_mps", "ego_speed_mps"]
        state_names = ["ego_position_m", "lead_position_m", "lead_speed_mps"]

        def plant_values(names):
            return plant.getFloat64([plant_refs[name] for name in names])

        initial = dict(sensing=plant_values(sensing_names), state=plant_values(state_names),
                       command=controller.getFloat64([controller_refs["accel_mps2"]]))
        states_at_interval_start = []
        published_commands = []
        for interval in range(steps):
            before = plant_values(sensing_names)
            states_at_interval_start.append(before)
            command_index = interval - coupling.command_age
            delivered = published_commands[command_index] if command_index >= 0 else None
            applied = coupling.initial_command if delivered is None else delivered
            plant.setFloat64([plant_refs["accel_mps2"]], [applied])
            flags = plant.doStep(interval * step_seconds, step_seconds)
            if any(flags[:3]):
                raise RuntimeError(f"plant doStep: {flags}")
            sampled = states_at_interval_start[max(0, interval - coupling.sensing_age)]
            controller.setFloat64([controller_refs[name] for name in sensing_names], sampled)
            flags = controller.doStep(interval * step_seconds, step_seconds)
            if any(flags[:3]):
                raise RuntimeError(f"controller doStep: {flags}")
            controller_output = controller.getFloat64([controller_refs["accel_mps2"]])
            # The original participant receives no sensing Message at zero, so
            # it publishes nothing. Preserve the real FMU output separately;
            # absence of a publication leaves the plant's initial input held.
            published = controller_output[0] if interval >= coupling.first_command_publication else None
            published_commands.append(published)
            trace.append(dict(slot_ns=interval * step_ns, interval_end_ns=(interval + 1) * step_ns,
                              sensing=before if coupling.sensing_at_interval_start else plant_values(sensing_names),
                              state=plant_values(state_names),
                              command=None if published is None else [published],
                              controller_output=controller_output, sampled=sampled, applied=[applied]))
        controller.terminate()
        plant.terminate()
    return dict(grid=dict(step_ns=step_ns, steps=steps), coupling=asdict(coupling),
                initialization=initial, intervals=trace)


if __name__ == "__main__":
    configuration = json.loads(Path(sys.argv[3]).read_text())
    Path(sys.argv[2]).write_text(json.dumps(run(sys.argv[1], configuration), indent=2, allow_nan=False) + "\n")
