"""FMPy 0.3.26 call path. No sil imports, model functions or Importer bindings."""
import json
import sys
import tempfile
from pathlib import Path

from fmpy import extract, read_model_description
from fmpy.fmi3 import FMU3Slave
from cases import CASES, INPUTS, OUTPUTS, STEPS, STEP_NS
from expected import check, expected


def run(case_name, output):
    case = CASES[case_name]
    model = case.model
    archive = Path("/fmus") / f"{model}.fmu"
    description = read_model_description(archive, validate=True)
    refs = {v.name: v.valueReference for v in description.modelVariables}
    ins = [refs[n] for n in INPUTS[model]]
    outs = [refs[n] for n in OUTPUTS[model]]
    trace = []
    # Two live instances of the SAME loaded library, stepped interleaved with
    # different inputs: catches exporter/module globals masked by processes.
    with tempfile.TemporaryDirectory() as directory:
        extract(archive, unzipdir=directory)
        instances = []
        try:
            for i, spec in enumerate(case.instances):
                fmu = FMU3Slave(guid=description.guid, unzipDirectory=directory,
                                modelIdentifier=description.coSimulation.modelIdentifier,
                                instanceName=f"instance{i}")
                fmu.instantiate()
                instances.append(fmu)
                fmu.setFloat64(ins, spec.start)
                fmu.enterInitializationMode(startTime=0.0, stopTime=1.0)
                fmu.exitInitializationMode()
                values = fmu.getFloat64(outs)
                check(values, expected(model, spec.start, 0.0), "initialization")
                trace.append(dict(instance=i, phase="initialized", time=0.0, values=values))
            held = [spec.start for spec in case.instances]
            for step in range(STEPS):
                t = step * STEP_NS / 1e9
                for i, fmu in enumerate(instances):
                    before = fmu.getFloat64(outs)
                    check(before, expected(model, held[i], t), "before set")
                    update = case.instances[i].update_at(step)
                    if update is not None:
                        fmu.setFloat64(ins, update)
                        held[i] = update
                    # This controller samples only at doStep; writes do not
                    # recompute its held output. Plant inputs do not jump state.
                    check(fmu.getFloat64(outs), before, "after set, before doStep")
                    flags = fmu.doStep(t, STEP_NS / 1e9)
                    if any(flags[:3]):
                        raise AssertionError(f"unexpected event/termination/early return: {flags}")
                    time = (step + 1) * STEP_NS / 1e9
                    values = fmu.getFloat64(outs)
                    check(values, expected(model, held[i], time), "after doStep")
                    trace.append(dict(instance=i, phase="step", start=t, time=time,
                                      inputs=held[i], before=before, values=values,
                                      do_step_flags=list(flags)))
            for i, fmu in enumerate(instances):
                fmu.terminate()
                trace.append(dict(instance=i, phase="terminated", time=1.0))
        finally:
            for fmu in instances:
                fmu.freeInstance()
    output.write_text(json.dumps(trace, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    run(sys.argv[1], Path(sys.argv[2]))
