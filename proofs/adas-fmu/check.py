"""Check the ADAS reference FMU with FMPy, an importer independent of SiL.

FMPy loads the archive's binary and drives its FMI 3.0 Co-Simulation
interface: instantiate, initialize, set typed inputs, step, read outputs,
terminate and free. Every result is compared with an expectation authored
without the C application:

- the ten maneuvers' trajectories in `examples/adas-reference/maneuvers/`,
  enumerated by hand from the profile, and expanded to inputs by the same
  `prepare.expand` the native example uses;
- the cases below, whose expected rows are written out here from
  docs/adas-reference.md.

The native example and this FMU share the application, so their agreement
alone would prove little; agreement with these expectations is the check.

    python proofs/adas-fmu/check.py AdasReference.fmu OUT_DIR

writes `OUT_DIR/check.json`, and exits 1 when any case fails.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import struct
import sys
import tempfile
from pathlib import Path

import fmpy
from fmpy import extract, read_model_description
from fmpy.fmi1 import FMICallException
from fmpy.fmi3 import FMU3Slave

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_DIR = ROOT / "examples" / "adas-reference"
sys.path.insert(0, str(EXAMPLE_DIR))
import prepare  # noqa: E402  (the native example's input expansion)

PERIOD_NS = 10_000_000
CAPACITY = 8
OUTPUTS = ("sample_time_ns", "sequence", "mode", "selected_object_id",
           "target_acceleration_mps2", "acceleration_mps2", "radar_age_ns",
           "camera_age_ns", "ego_age_ns", "ignored_observations")
FLOAT_OUTPUTS = {"target_acceleration_mps2", "acceleration_mps2"}
OBJECT_FIELDS = ("object_id", "x_m", "y_m", "relative_vx_mps", "confidence")
CLEAR, HAZARD, UNAVAILABLE = 0, 1, 2
FMI3_ERROR = 3
NO_OBJECT = NO_AGE = -1


def f32(value: float) -> float:
    """`value` rounded once to binary32."""
    return struct.unpack("<f", struct.pack("<f", value))[0]


class CaseFailed(AssertionError):
    """A result differs from its expectation."""


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise CaseFailed(message)


# --- driving the FMU -------------------------------------------------------


class Refused(Exception):
    """The FMU returned no instance; `messages` is what it logged."""

    def __init__(self, error: Exception, messages: list[str]):
        super().__init__(str(error))
        self.messages = messages


class Archive:
    """The FMU extracted once, and what its description declares."""

    def __init__(self, path: Path, directory: Path):
        self.path = path
        self.description = read_model_description(str(path))
        self.directory = extract(str(path), unzipdir=str(directory))
        self.variables = {v.name: v for v in self.description.modelVariables}

    @property
    def token(self) -> str:
        return self.description.instantiationToken


class Instance:
    """One FMPy instance of the FMU, and every message it logged."""

    def __init__(self, archive: Archive, name: str, *, token: str | None = None,
                 event_mode: bool = False):
        self.archive = archive
        self.messages: list[str] = []
        self.slave = FMU3Slave(
            guid=archive.token if token is None else token,
            unzipDirectory=archive.directory,
            modelIdentifier=archive.description.coSimulation.modelIdentifier,
            instanceName=name)
        try:
            self.slave.instantiate(loggingOn=True, eventModeUsed=event_mode,
                                   logMessage=self._log)
        except Exception as error:  # FMPy's "Failed to instantiate FMU"
            self.slave.freeLibrary()
            raise Refused(error, self.messages) from error

    def _log(self, environment, status, category, message):
        self.messages.append(message.decode())

    def initialize(self, start_ns: int = 0, **parameters: float) -> None:
        for name, value in parameters.items():
            self.set(name, [value])
        self.slave.enterInitializationMode(startTime=start_ns / 1e9)
        self.slave.exitInitializationMode()

    def set(self, name: str, values: list) -> None:
        variable = self.archive.variables[name]
        getattr(self.slave, f"set{variable.type}")(
            [variable.valueReference], values)

    def get(self, name: str) -> list:
        variable = self.archive.variables[name]
        return getattr(self.slave, f"get{variable.type}")(
            [variable.valueReference], _count(variable))

    def apply(self, inputs: dict[str, list]) -> None:
        for name, values in inputs.items():
            self.set(name, values)

    def step(self, t_ns: int, step_ns: int = PERIOD_NS) -> dict:
        self.slave.doStep(t_ns / 1e9, step_ns / 1e9)
        return self.command()

    def command(self) -> dict:
        return {field: self.get(f"command.{field}")[0] for field in OUTPUTS}

    def free(self) -> None:
        self.slave.freeInstance()


def _count(variable) -> int:
    return math.prod(d.start for d in variable.dimensions)


def rejected(call, *args) -> bool:
    """Whether the FMI call answered fmi3Error."""
    try:
        call(*args)
    except FMICallException as error:
        return error.status == FMI3_ERROR
    return False


# --- inputs ---------------------------------------------------------------


def object_list(sensor: str, sample_time_ns: int, sequence: int,
                objects: list[tuple] = (), validity: int = 1,
                sensor_id: int | None = None) -> dict[str, list]:
    """The inputs of one list. A radar object is (id, x, y, vx,
    confidence), a camera object (id, x, y, confidence)."""
    if sensor_id is None:
        sensor_id = {"radar": 1, "camera": 2}[sensor]
    arrays = {field: [0] * CAPACITY for field in OBJECT_FIELDS}
    for i, values in enumerate(objects):
        if sensor == "camera":
            values = (*values[:3], 0.0, values[3])
        for field, value in zip(OBJECT_FIELDS, values):
            arrays[field][i] = value
    return {f"{sensor}.sample_time_ns": [sample_time_ns],
            f"{sensor}.sensor_id": [sensor_id], f"{sensor}.frame_id": [1],
            f"{sensor}.sequence": [sequence],
            f"{sensor}.count": [len(objects)],
            f"{sensor}.validity": [validity],
            **{f"{sensor}.{field}": values
               for field, values in arrays.items()}}


def ego(sample_time_ns: int, sequence: int, speed_mps: float = 20.0,
        validity: int = 1) -> dict[str, list]:
    return {"ego.sample_time_ns": [sample_time_ns],
            "ego.sequence": [sequence], "ego.validity": [validity],
            "ego.speed_mps": [speed_mps]}


def maneuver_inputs(maneuver: str) -> list[dict[str, list]]:
    """Each activation's inputs, from the expanded authored maneuver."""
    with tempfile.TemporaryDirectory() as scratch:
        expanded = Path(scratch) / f"{maneuver}.inputs.csv"
        prepare.expand(prepare.MANEUVER_DIR / f"{maneuver}.csv", expanded)
        with expanded.open(newline="") as f:
            rows = list(csv.DictReader(f))
    steps = []
    for k, row in enumerate(rows):
        expect(int(row["time_ms"]) * 1_000_000 == k * PERIOD_NS,
               f"{maneuver}: row {k} is not the activation at {k * 10} ms")
        inputs = {}
        for sensor in ("radar", "camera"):
            if row[f"{sensor}_t_ns"] == "":
                continue
            inputs.update({
                f"{sensor}.sample_time_ns": [int(row[f"{sensor}_t_ns"])],
                **{f"{sensor}.{field}": [int(row[f"{sensor}_{field}"])]
                   for field in ("sensor_id", "frame_id", "sequence",
                                 "count", "validity")},
                f"{sensor}.object_id": [
                    int(row[f"{sensor}_object_id_{i}"])
                    for i in range(CAPACITY)],
                **{f"{sensor}.{field}": [
                    float(row[f"{sensor}_{field}_{i}"])
                    for i in range(CAPACITY)]
                   for field in OBJECT_FIELDS[1:]}})
        if row["ego_t_ns"] != "":
            inputs.update(ego(int(row["ego_t_ns"]), int(row["ego_sequence"]),
                              float(row["ego_speed_mps"]),
                              int(row["ego_validity"])))
        steps.append(inputs)
    return steps


def expected_rows(maneuver: str) -> list[dict]:
    path = prepare.MANEUVER_DIR / f"{maneuver}.expected.csv"
    with path.open(newline="") as f:
        return [{"sample_time_ns": int(row["time_ms"]) * 1_000_000,
                 **{field: (float(row[field]) if field in FLOAT_OUTPUTS
                            else int(row[field]))
                    for field in OUTPUTS[1:]}}
                for row in csv.DictReader(f)]


def command(t_ns: int, sequence: int, mode: int, selected: int,
            target: float, acceleration: float, ages: tuple[int, int, int],
            ignored: int = 0) -> dict:
    """The Command an activation at `t_ns` is expected to write."""
    return dict(zip(OUTPUTS, (t_ns + PERIOD_NS, sequence, mode, selected,
                              f32(target), f32(acceleration), *ages,
                              ignored)))


def run(instance: Instance, steps: list[dict], start_ns: int = 0) -> list:
    commands = []
    for k, inputs in enumerate(steps):
        instance.apply(inputs)
        commands.append(instance.step(start_ns + k * PERIOD_NS))
    return commands


def compare(actual: list[dict], expected: list[dict]) -> None:
    """Fails at the first field that differs; floats compare exactly."""
    expect(len(actual) == len(expected),
           f"{len(actual)} Commands, {len(expected)} expected")
    for row, (got, want) in enumerate(zip(actual, expected)):
        for field in OUTPUTS:
            expect(got[field] == want[field],
                   f"Command {row + 1} (Sample time {want['sample_time_ns']}"
                   f" ns): {field} is {got[field]!r}, expected "
                   f"{want[field]!r}")


# --- cases ----------------------------------------------------------------


def maneuver_case(maneuver: str):
    def case(archive: Archive) -> dict:
        instance = Instance(archive, maneuver)
        instance.initialize()
        commands = run(instance, maneuver_inputs(maneuver))
        instance.slave.terminate()
        instance.free()
        compare(commands, expected_rows(maneuver))
        return {"commands": len(commands)}
    case.__name__ = f"maneuver_{maneuver}"
    return case


def start_values(archive: Archive) -> dict:
    """Every variable reads its declared start after initialization, and
    inputs left at their start values deliver no observation."""
    instance = Instance(archive, "start")
    instance.initialize()
    for name, variable in archive.variables.items():
        if variable.causality == "independent":
            continue
        declared = variable.start.split()
        values = instance.get(name)
        parse = float if variable.type.startswith("Float") else int
        expect(values == [parse(v) for v in declared],
               f"{name} reads {values}, declared start {declared}")
    commands = run(instance, [{}, {}])
    instance.free()
    compare(commands, [
        command(0, 1, UNAVAILABLE, NO_OBJECT, -3, -0.5, (NO_AGE,) * 3),
        command(PERIOD_NS, 2, UNAVAILABLE, NO_OBJECT, -3, -1.0, (NO_AGE,) * 3),
    ])
    return {"variables": len(archive.variables) - 1}


def float32_inputs(archive: Archive) -> dict:
    """A Float32 input holds the binary32 value of what is set: 7.9999999
    is 8 in binary32 and no hazard (x < 8 is strict); 7.9999995 stays
    below 8 and is one."""
    instance = Instance(archive, "float32-inputs")
    instance.initialize()
    camera = object_list("camera", 0, 0, [(5, 8.0, 0.0, 1.0)])
    commands = run(instance, [
        {**object_list("radar", 0, 0, [(1, 7.9999999, 0.0, 0.0, 1.0)]),
         **camera, **ego(0, 0)},
        object_list("radar", PERIOD_NS, 1, [(1, 7.9999995, 0.0, 0.0, 1.0)]),
    ])
    held = instance.get("radar.x_m")[0]
    instance.free()
    expect(held == f32(7.9999995) and held < 8.0,
           f"radar.x_m[0] holds {held!r}")
    compare(commands, [
        command(0, 1, CLEAR, 1, 0, 0, (0, 0, 0)),
        command(PERIOD_NS, 2, HAZARD, 1, -3, -0.5, (0, PERIOD_NS, PERIOD_NS)),
    ])
    return {"binary32_of_7.9999995": held}


def float32_outputs(archive: Archive) -> dict:
    """The accelerations are computed in binary64 and rounded once to
    binary32: with a change of 0.1 per step they are not decimal tenths."""
    instance = Instance(archive, "float32-outputs")
    instance.initialize(hazard_acceleration_mps2=-1.0, max_change_mps2=0.1)
    steps = [{**object_list("radar", t, k, [(4, 5.0, 0.0, 0.0, 1.0)]),
              **object_list("camera", t, k, [(9, 5.0, 0.0, 1.0)]),
              **ego(t, k)}
             for k, t in enumerate(range(0, 3 * PERIOD_NS, PERIOD_NS))]
    commands = run(instance, steps)
    instance.free()
    # binary64: 0 - 0.1, then - 0.1, then - 0.1.
    accelerations = (-0.1, -0.1 - 0.1, -0.1 - 0.1 - 0.1)
    compare(commands, [
        command(k * PERIOD_NS, k + 1, HAZARD, 4, -1.0, a, (0, 0, 0))
        for k, a in enumerate(accelerations)])
    expect(commands[0]["acceleration_mps2"] != -0.1,
           "the acceleration is not single precision")
    return {"accelerations": [c["acceleration_mps2"] for c in commands]}


def uint64_sample_times(archive: Archive) -> dict:
    """Sample times above 2^53 ns stay exact: a list sampled 1 ns before
    the activation has age 1 ns."""
    start = 10**16
    assert start > 2**53
    instance = Instance(archive, "uint64")
    instance.initialize(start_ns=start)
    sample = start - 1
    commands = run(instance, [
        {**object_list("radar", sample, 0, [(2, 40.0, 0.0, 0.0, 1.0)]),
         **object_list("camera", sample, 0, [(3, 40.0, 0.0, 1.0)]),
         **ego(sample, 0)},
        {},
    ], start_ns=start)
    held = instance.get("radar.sample_time_ns")[0]
    instance.free()
    expect(held == sample, f"radar.sample_time_ns holds {held}")
    compare(commands, [
        command(start, 1, CLEAR, 2, 0, 0, (1, 1, 1)),
        command(start + PERIOD_NS, 2, CLEAR, 2, 0, 0,
                (PERIOD_NS + 1,) * 3),
    ])
    return {"start_ns": start, "final_sample_time_ns":
            commands[-1]["sample_time_ns"]}


def late_start(archive: Archive) -> dict:
    """Near the 2^63 ns start limit a double second is coarser than 1 us.
    Steps at the points the FMU itself reports are still accepted, and the
    Sample times stay exact."""
    start = 9_000_000_000_000_000_000
    instance = Instance(archive, "late-start")
    instance.initialize(start_ns=start)
    point = start / 1e9
    commands = []
    for _ in range(5):
        _, _, _, point = instance.slave.doStep(point, PERIOD_NS / 1e9)
        commands.append(instance.command())
    instance.free()
    compare(commands, [
        command(start + k * PERIOD_NS, k + 1, UNAVAILABLE, NO_OBJECT, -3,
                max(-0.5 * (k + 1), -3.0), (NO_AGE,) * 3)
        for k in range(5)])
    return {"start_ns": start, "final_sample_time_ns":
            commands[-1]["sample_time_ns"]}


def held_inputs(archive: Archive) -> dict:
    """An unchanged header delivers nothing; a changed header with a
    sequence that does not increase is delivered, ignored and counted."""
    instance = Instance(archive, "held")
    instance.initialize()
    radar = object_list("radar", 0, 0, [(6, 40.0, 0.0, 0.0, 1.0)])
    camera = object_list("camera", 0, 0, [(6, 40.0, 0.0, 1.0)])
    t1, t2, t3 = PERIOD_NS, 2 * PERIOD_NS, 3 * PERIOD_NS
    commands = run(instance, [
        {**radar, **camera, **ego(0, 0)},
        {},                                  # nothing new: ages grow
        {**radar, **ego(t2, 0)},             # radar unchanged; ego sequence 0 again
        ego(t3, 3),
    ])
    instance.free()
    compare(commands, [
        command(0, 1, CLEAR, 6, 0, 0, (0, 0, 0)),
        command(t1, 2, CLEAR, 6, 0, 0, (t1, t1, t1)),
        # The ignored ego motion does not refresh its age: 20 ms, still fresh.
        command(t2, 3, CLEAR, 6, 0, 0, (t2, t2, t2), ignored=1),
        # Radar age 30 ms is within its 40 ms limit.
        command(t3, 4, CLEAR, 6, 0, 0, (t3, t3, 0), ignored=1),
    ])
    return {"ignored": commands[-1]["ignored_observations"]}


def parameters(archive: Archive) -> dict:
    """A parameter outside its range fails initialization; a parameter
    cannot change after it; an output, a wrong type and a wrong number of
    values are refused. Reset recovers a failed instance."""
    diagnostics = {}
    for name, value, message in (
            ("hazard_acceleration_mps2", 0.0, "initialization failed: "
             "hazard_acceleration_mps2 0 is outside [-10, 0)"),
            ("max_change_mps2", math.nan, "initialization failed: "
             "max_change_mps2 nan is outside (0, 10]")):
        instance = Instance(archive, f"bad-{name}")
        instance.set(name, [value])
        instance.slave.enterInitializationMode()
        expect(rejected(instance.slave.exitInitializationMode),
               f"{name}={value} was accepted")
        expect(rejected(instance.slave.doStep, 0.0, 0.01),
               "a failed instance stepped")
        instance.slave.reset()
        instance.initialize()
        steps = run(instance, [{}])
        instance.free()
        expect(steps[0]["sequence"] == 1, "reset did not recover")
        expect(instance.messages == [
            message, "fmi3DoStep is not allowed in the failed phase"],
            f"diagnostics {instance.messages}")
        diagnostics[name] = instance.messages

    instance = Instance(archive, "refusals")
    instance.initialize()
    refusals = (
        (instance.slave.setFloat64, [2], [1.0],
         "fmi3SetFloat64: max_change_mps2 cannot be set in the Step phase"),
        (instance.slave.setUInt32, [402], [1],
         "fmi3SetUInt32: command.mode cannot be set in the Step phase"),
        (instance.slave.setFloat64, [107], [0.0] * CAPACITY,
         "fmi3SetFloat64: radar.x_m is a Float32 variable"),
        (instance.slave.setFloat32, [107], [0.0] * (CAPACITY - 1),
         "fmi3SetFloat32: the references hold 8 values, not 7"),
        (instance.slave.getUInt32, [999], 1,
         "fmi3GetUInt32: no variable has value reference 999"),
    )
    for call, references, values, _ in refusals:
        expect(rejected(call, references, values), f"{call.__name__} "
               f"{references} was accepted")
    expect(instance.messages == [r[3] for r in refusals],
           f"diagnostics {instance.messages}")
    expect(instance.get("max_change_mps2") == [0.5],
           "a refused set changed the parameter")
    diagnostics["refusals"] = instance.messages
    instance.free()
    return {"diagnostics": diagnostics}


def fixed_step(archive: Archive) -> dict:
    """The step is 10 ms at the FMU's next point; any other is an error."""
    diagnostics = []
    for name, point_ns, step_ns, message in (
            ("size", 0, 2 * PERIOD_NS,
             "communicationStepSize 0.02 s is not the fixed step 10000000 ns; "
             "the FMU has no variable step"),
            ("point", PERIOD_NS, PERIOD_NS,
             "currentCommunicationPoint 0.01 s is not the next point 0 ns")):
        instance = Instance(archive, f"step-{name}")
        instance.initialize()
        expect(rejected(instance.step, point_ns, step_ns),
               f"a step of {step_ns} ns at {point_ns} ns was accepted")
        instance.free()
        expect(instance.messages == [message],
               f"diagnostics {instance.messages}")
        diagnostics += instance.messages
    return {"diagnostics": diagnostics}


def malformed_inputs(archive: Archive) -> dict:
    """Malformed input fails the step with the field named; the instance
    then accepts no step until it is reset."""
    radar = [(1, 20.0, 0.0, 0.0, 1.0)]

    def with_(inputs: dict, **changes) -> dict:
        return {**inputs, **{k.replace("__", "."): v
                             for k, v in changes.items()}}

    cases = {
        "count above capacity": (
            with_(object_list("radar", 0, 0, radar), radar__count=[9]),
            "t=0 ns: radar.count 9 exceeds the capacity 8"),
        "inactive element not zero": (
            with_(object_list("radar", 0, 0, radar),
                  radar__x_m=[20.0, 0, 0, 1.0, 0, 0, 0, 0]),
            "t=0 ns: radar.x_m[3] is inactive (count 1) but not zero; "
            "inactive elements must be zero"),
        "nonfinite position": (
            object_list("radar", 0, 0, [(1, math.nan, 0.0, 0.0, 1.0)]),
            "t=0 ns: radar.x_m[0] is not finite: nan"),
        "negative ID": (
            object_list("radar", 0, 0, [(-5, 20.0, 0.0, 0.0, 1.0)]),
            "t=0 ns: radar.object_id[0] -5 is negative; active IDs are >= 0 "
            "and -1 is reserved for no selection"),
        "future Sample time": (
            object_list("radar", PERIOD_NS, 0, radar),
            "t=0 ns: radar.sample_time_ns 10000000 is after the activation "
            "time 0; a future Sample time is malformed"),
        "wrong sensor": (
            object_list("camera", 0, 0, [], sensor_id=1),
            "t=0 ns: camera.sensor_id 1 is not the camera sensor 2"),
        "validity out of range": (
            object_list("radar", 0, 0, radar, validity=2),
            "t=0 ns: radar.validity 2 is neither 0 (invalid) nor 1 (valid)"),
    }
    diagnostics = {}
    for name, (inputs, message) in cases.items():
        instance = Instance(archive, name)
        instance.initialize()
        instance.apply(inputs)
        expect(rejected(instance.step, 0), f"{name} was accepted")
        expect(rejected(instance.step, 0), f"{name}: the failed instance "
               "stepped again")
        expect(rejected(instance.slave.terminate),
               f"{name}: the failed instance terminated")
        instance.free()
        expect(instance.messages == [
            message, "fmi3DoStep is not allowed in the failed phase",
            "fmi3Terminate is not allowed in the failed phase"],
            f"{name}: diagnostics {instance.messages}")
        diagnostics[name] = instance.messages[0]
    return {"diagnostics": diagnostics}


def two_instances(archive: Archive) -> dict:
    """Two instances in one process, stepped alternately, each follow their
    own maneuver."""
    names = ("hazard", "clear")
    instances = [Instance(archive, name) for name in names]
    inputs = [maneuver_inputs(name) for name in names]
    commands = [[], []]
    for instance in instances:
        instance.initialize()
    for k in range(len(inputs[0])):
        for i, instance in enumerate(instances):
            instance.apply(inputs[i][k])
            commands[i].append(instance.step(k * PERIOD_NS))
    for i, instance in enumerate(instances):
        instance.slave.terminate()
        instance.free()
        compare(commands[i], expected_rows(names[i]))
    return {"instances": list(names)}


def instantiation_refused(archive: Archive) -> dict:
    """Another token, or Event Mode, refuses the instance with a cause."""
    diagnostics = []
    for options, message in (
            ({"token": "another"}, "the instantiation token another is not "
             f"this FMU's {archive.token}"),
            ({"event_mode": True},
             "the FMU has no Event Mode (hasEventMode=false)")):
        try:
            Instance(archive, "refused", **options)
        except Refused as refusal:
            expect(refusal.messages == [message],
                   f"{options}: diagnostics {refusal.messages}")
            diagnostics.append({"options": options, "importer": str(refusal),
                                "diagnostics": refusal.messages})
        else:
            raise CaseFailed(f"{options} instantiated")
    return {"refusals": diagnostics}


def termination(archive: Archive) -> dict:
    """Terminate ends stepping; outputs stay readable; free releases."""
    instance = Instance(archive, "terminate")
    expect(rejected(instance.slave.terminate),
           "terminate before initialization was accepted")
    instance.initialize()
    commands = run(instance, [{}, {}])
    instance.slave.terminate()
    after = instance.command()
    expect(rejected(instance.step, 2 * PERIOD_NS),
           "a terminated instance stepped")
    expect(rejected(instance.slave.terminate), "terminate was accepted twice")
    instance.free()
    expect(after == commands[-1], "terminate changed the outputs")
    expect(instance.messages == [
        "fmi3Terminate is not allowed in the Instantiated phase",
        "fmi3DoStep is not allowed in the Terminated phase",
        "fmi3Terminate is not allowed in the Terminated phase"],
        f"diagnostics {instance.messages}")
    return {"diagnostics": instance.messages}


CASES = [maneuver_case(m) for m in prepare.MANEUVERS] + [
    start_values, float32_inputs, float32_outputs, uint64_sample_times,
    late_start,
    held_inputs, parameters, fixed_step, malformed_inputs, two_instances,
    instantiation_refused, termination,
]


def check(fmu: Path, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory() as scratch:
        archive = Archive(fmu, Path(scratch))
        for case in CASES:
            try:
                detail = case(archive)
                results.append({"case": case.__name__, "passed": True,
                                "detail": detail})
            except (CaseFailed, FMICallException, Refused) as error:
                results.append({"case": case.__name__, "passed": False,
                                "detail": str(error)})
    report = {
        "fmu": fmu.name,
        "archive_sha256": hashlib.sha256(fmu.read_bytes()).hexdigest(),
        "instantiation_token": archive.token,
        "importer": f"FMPy {fmpy.__version__}",
        "platform": fmpy.platform_tuple,
        "passed": all(r["passed"] for r in results),
        "cases": results,
    }
    (out_dir / "check.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("fmu", type=Path, help="the FMU archive to check")
    parser.add_argument("out_dir", type=Path, help="where check.json goes")
    args = parser.parse_args()
    report = check(args.fmu, args.out_dir)
    for result in report["cases"]:
        state = "ok" if result["passed"] else "FAILED"
        print(f"{state:6} {result['case']}" + (
            "" if result["passed"] else f": {result['detail']}"))
    sys.exit(0 if report["passed"] else 1)
