"""The repository's public ACC FMUs driven by recorded OpenACC data, under FMPy.

FMPy is the independent execution these workloads hand to #194: the same
archives SiL imports, stepped by a different importer. The models are
repository-authored (`proofs/acc-fmi`), not supplier models, and the recorded
vehicles' own responses are never used as the models' expected output.

Single FMU, open loop: `AccController` receives one recorded sample per
100 ms communication interval, held for the whole interval. The sample at
t_k is set before the step [t_k, t_k+1); the command it produces is observed
at t_k+1. 501 samples therefore give 501 observed commands, the last at
50.1 s, so the final recorded sample is covered.

Coupled FMUs, closed loop: `AccPlant` and `AccController` on the qualified
10 ms baseline grid with its one-period sensing and command delays. Only the
lead vehicle's measured acceleration comes from the recording. The ego is
the plant's, driven by the controller: the recorded ego trajectory is never
replayed into the loop, because that would remove the feedback under test.
"""
import hashlib
import math
import subprocess
import tempfile
import zipfile
from contextlib import ExitStack
from pathlib import Path
from xml.etree import ElementTree

CONTROLLER_INPUTS = ("gap_m", "relative_speed_mps", "ego_speed_mps")
TRUTH = ("gap_m", "relative_speed_mps", "ego_speed_mps",
         "ego_position_m", "lead_position_m", "lead_speed_mps")
ABS_TOL = 1e-10
REL_TOL = 1e-12
RECORDED_PERIOD_NS = 100_000_000
LOOP_PERIOD_NS = 10_000_000


def shifted(samples, periods):
    """The samples delayed by whole periods; the first sample is held."""
    return [samples[max(index - periods, 0)] for index in range(len(samples))]


def held(samples, t_ns, period_ns):
    """The recorded sample in force at t: zero-order hold over its interval."""
    return samples[t_ns // period_ns]


def numeric_divergence(reference, candidate, abs_tol, rel_tol):
    for index, (expected, actual) in enumerate(zip(reference, candidate)):
        for field, want in expected.items():
            got = actual.get(field)
            if field == "t_ns":
                close = got == want
            else:
                close = (got is not None and math.isfinite(got)
                         and math.isclose(got, want, abs_tol=abs_tol, rel_tol=rel_tol))
            if not close:
                return {"index": index, "t_ns": expected["t_ns"], "field": field,
                        "expected": want, "actual": got}
    if len(reference) != len(candidate):
        return {"index": min(len(reference), len(candidate)), "field": "coverage",
                "expected": len(reference), "actual": len(candidate)}
    return None


def audit(archive):
    """Identity, interface and runtime dependencies of one FMU archive."""
    from fmpy import read_model_description
    read_model_description(archive, validate=True)
    with zipfile.ZipFile(archive) as opened, tempfile.TemporaryDirectory() as directory:
        opened.extractall(directory)
        root = ElementTree.fromstring(opened.read("modelDescription.xml"))
        co_simulation = root.find("CoSimulation")
        identifier = co_simulation.get("modelIdentifier")
        platforms = sorted({Path(n).parts[1] for n in opened.namelist()
                            if n.startswith("binaries/") and len(Path(n).parts) > 2})
        binary = Path(directory) / "binaries/x86_64-linux" / f"{identifier}.so"
        libraries = subprocess.run(["ldd", binary], capture_output=True, text=True).stdout
    variables = [{"name": v.get("name"), "type": v.tag, "causality": v.get("causality", "local"),
                  "unit": v.get("unit"), "start": v.get("start"),
                  "dimensions": len(v.findall("Dimension"))}
                 for v in root.find("ModelVariables")]
    return {
        "archive_sha256": hashlib.sha256(Path(archive).read_bytes()).hexdigest(),
        "fmi_version": root.get("fmiVersion"),
        "model_name": root.get("modelName"),
        "instantiation_token": root.get("instantiationToken"),
        "generation_tool": root.get("generationTool"),
        "interfaces": [e.tag for e in root if e.tag in ("CoSimulation", "ModelExchange",
                                                        "ScheduledExecution")],
        "capabilities": dict(co_simulation.attrib),
        "platforms": platforms,
        "variables": variables,
        "runtime_libraries": libraries.split("\n"),
        "unresolved_libraries": "not found" in libraries,
    }


def _instance(stack, archive, name, stop_s, starts=None):
    from fmpy import extract, read_model_description
    from fmpy.fmi3 import FMU3Slave
    description = read_model_description(archive, validate=True)
    directory = stack.enter_context(tempfile.TemporaryDirectory())
    extract(archive, unzipdir=directory)
    fmu = FMU3Slave(guid=description.guid, unzipDirectory=directory,
                    modelIdentifier=description.coSimulation.modelIdentifier, instanceName=name)
    fmu.instantiate()
    stack.callback(fmu.freeInstance)
    refs = {v.name: v.valueReference for v in description.modelVariables}
    for variable, value in (starts or {}).items():
        fmu.setFloat64([refs[variable]], [value])
    fmu.enterInitializationMode(startTime=0.0, stopTime=stop_s)
    fmu.exitInitializationMode()
    return fmu, refs


def _step(fmu, t_s, h_s):
    flags = fmu.doStep(t_s, h_s)
    if any(flags[:3]):
        raise RuntimeError(f"unexpected doStep result {flags} at {t_s} s")


def single(archive, inputs):
    """Open-loop controller trace: the command observed after each held sample."""
    h_s = RECORDED_PERIOD_NS / 1e9
    trace = []
    with ExitStack() as stack:
        fmu, refs = _instance(stack, archive, "controller", len(inputs) * h_s,
                              {name: inputs[0][name] for name in CONTROLLER_INPUTS})
        for k, sample in enumerate(inputs):
            fmu.setFloat64([refs[n] for n in CONTROLLER_INPUTS],
                           [sample[n] for n in CONTROLLER_INPUTS])
            _step(fmu, k * h_s, h_s)
            trace.append({"t_ns": (k + 1) * RECORDED_PERIOD_NS,
                          "accel_mps2": fmu.getFloat64([refs["accel_mps2"]])[0]})
        fmu.terminate()
    return trace


def coupled(controller_archive, plant_archive, lead_accelerations):
    """Closed loop with the measured lead acceleration held per recorded interval."""
    h_ns = LOOP_PERIOD_NS
    h_s = h_ns / 1e9
    steps = len(lead_accelerations) * RECORDED_PERIOD_NS // h_ns
    trace = []
    with ExitStack() as stack:
        plant, pr = _instance(stack, plant_archive, "plant", steps * h_s)
        controller, cr = _instance(stack, controller_archive, "controller", steps * h_s)
        sensing = plant.getFloat64([pr[n] for n in CONTROLLER_INPUTS])
        command = 0.0
        for i in range(steps):
            t_ns = i * h_ns
            lead = held(lead_accelerations, t_ns, RECORDED_PERIOD_NS)
            plant.setFloat64([pr["accel_mps2"], pr["lead_accel_mps2"]], [command, lead])
            _step(plant, i * h_s, h_s)
            truth = plant.getFloat64([pr[n] for n in TRUTH])
            # Sensing published in this Slot becomes visible one period later.
            controller.setFloat64([cr[n] for n in CONTROLLER_INPUTS], sensing)
            _step(controller, i * h_s, h_s)
            sensing = truth[:3]
            command = controller.getFloat64([cr["accel_mps2"]])[0]
            trace.append({"t_ns": t_ns, "lead_accel_mps2": lead, "accel_mps2": command,
                          **dict(zip(TRUTH, truth))})
        plant.terminate()
        controller.terminate()
    return trace
