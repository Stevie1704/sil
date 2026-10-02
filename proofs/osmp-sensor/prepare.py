"""Build, inspect and check the OSMP FMU target, and write its offline bundle.

Runs inside the pinned tool image with no network: the sources were fetched
and verified while the image was built. Usage: prepare.py <work-directory>.

1. Builds `OSMPDummySensor` and `OSMPDummySource` twice with the upstream
   CMake project, unmodified, and pins the first build.
2. Writes a static compatibility report for each archive (no FMU code runs).
3. Runs both FMUs under FMPy, an independent FMI 2.0 importer, for `STEPS`
   sensor Periods: within each communication step the source steps first and
   its three OSMP integers are copied to the sensor, as an OSMP connection
   does. Every SensorView is checked against the source's closed-form motion,
   and every SensorData against the expected slice from `scene.py`.

Every check raises, so a failed qualification cannot write a passing report.
"""
import ctypes
import hashlib
import importlib.metadata
import json
import os
import platform
import resource
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import inspect_fmu
import osmp
import scene

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SOURCES = Path("/sources")
OSMP_CHECKOUT = SOURCES / "osmp"
EXAMPLES = OSMP_CHECKOUT / "examples"
TARGETS = ("OSMPDummySensor", "OSMPDummySource")
CMAKE_ARGS = ("-DCMAKE_BUILD_TYPE=Release",)
# 30 s at the sensor's 20 ms Period: covers a cone entry, a range exit and
# vehicles falling behind the host (see `transitions` in report.json).
STEPS = 1500
STEP_NS = 20_000_000
NOMINAL_RANGE_M = 135.0
CONTROL_RANGE_M = 100.0


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def command(*arguments, **options):
    return subprocess.run(arguments, check=True, capture_output=True, text=True,
                          **options).stdout


def first_line(*arguments):
    return command(*arguments).splitlines()[0]


def toolchain():
    packages = ("g++", "cmake", "libprotobuf-dev", "protobuf-compiler", "libc6", "libstdc++6")
    return {
        "machine": platform.machine(),
        "libc": " ".join(platform.libc_ver()),
        "c++": first_line("c++", "--version"),
        "cmake": first_line("cmake", "--version"),
        "protoc": first_line("protoc", "--version"),
        "debian_packages": command("dpkg-query", "-W", "-f", "${Package} ${Version}\n",
                                   *packages).splitlines(),
        "python": platform.python_version(),
        "python_packages": {name: importlib.metadata.version(name)
                            for name in ("fmpy", "protobuf")},
        "source_revision": (ROOT / "source-revision.txt").read_text().strip(),
    }


# Build ------------------------------------------------------------------------

def build(directory, epoch):
    """The upstream CMake build; SOURCE_DATE_EPOCH fixes generationDateAndTime."""
    environment = {**os.environ, "SOURCE_DATE_EPOCH": epoch}
    log = command("cmake", "-S", EXAMPLES, "-B", directory, *CMAKE_ARGS, env=environment)
    log += command("cmake", "--build", directory, "--parallel", str(os.cpu_count()),
                   "--target", *TARGETS, env=environment)
    return {target: directory / target / f"{target}.fmu" for target in TARGETS}, log


def member_digests(archive):
    with zipfile.ZipFile(archive) as opened:
        return {name: hashlib.sha256(opened.read(name)).hexdigest()
                for name in sorted(opened.namelist()) if not name.endswith("/")}


def build_twice(work, bundle, evidence):
    epoch = command("git", "-C", OSMP_CHECKOUT, "log", "-1", "--format=%ct").strip()
    first, log = build(work / "build-1", epoch)
    second, _ = build(work / "build-2", epoch)
    (evidence / "build.log").write_text(log)
    pinned = {}
    for target in TARGETS:
        destination = bundle / "fmus" / f"{target}.fmu"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(first[target], destination)
        pinned[target] = destination
    shutil.copy(OSMP_CHECKOUT / "LICENSE", bundle / "fmus" / "LICENSE")
    return pinned, {
        "cmake_source": "examples/ (upstream CMakeLists.txt, unmodified)",
        "cmake_args": list(CMAKE_ARGS),
        "targets": list(TARGETS),
        "source_date_epoch": int(epoch),
        # Reported, not required: the bundle pins the first build's bytes.
        "rebuild_identical_members": {
            target: member_digests(first[target]) == member_digests(second[target])
            for target in TARGETS},
    }


def osi_bindings(build_directory, output):
    """Python OSI classes from the same .proto files the FMUs were built from."""
    output.mkdir(parents=True, exist_ok=True)
    protos = sorted((EXAMPLES / "osi-cpp/open-simulation-interface").glob("*.proto"))
    generated = build_directory / "osi-cpp"
    command("protoc", f"-I{generated}", f"-I{protos[0].parent}", f"--python_out={output}",
            generated / "osi_version.proto", *protos)
    sys.path.insert(0, str(output))


# Execution --------------------------------------------------------------------

class Fmu:
    """One FMI 2.0 Co-Simulation instance under FMPy, addressed by variable name."""

    def __init__(self, archive, name, reals=None):
        from fmpy import extract, read_model_description
        from fmpy.fmi2 import FMU2Slave
        description = read_model_description(str(archive))
        with zipfile.ZipFile(archive) as opened:
            text = opened.read("modelDescription.xml").decode()
        self.binary = osmp.binary_variables(text)
        self.refs = {v.name: v.valueReference for v in description.modelVariables}
        self.fmu = FMU2Slave(guid=description.guid, unzipDirectory=extract(str(archive)),
                             modelIdentifier=description.coSimulation.modelIdentifier,
                             instanceName=name)
        self.fmu.instantiate()
        for variable, value in (reals or {}).items():
            self.fmu.setReal([self.refs[variable]], [value])
        self.fmu.setupExperiment(startTime=0.0)
        self.fmu.enterInitializationMode()
        self.fmu.exitInitializationMode()

    def integers(self, binary):
        refs = self.binary[binary]["value_references"]
        return self.fmu.getInteger([refs[role] for role in osmp.ROLES])

    def set_integers(self, binary, values):
        refs = self.binary[binary]["value_references"]
        self.fmu.setInteger([refs[role] for role in osmp.ROLES], values)

    def message(self, binary, message):
        """The OSI message behind one binary variable, and its size in bytes."""
        lo, hi, size = self.integers(binary)
        require(size > 0, f"{binary} is empty")
        message.ParseFromString(ctypes.string_at(osmp.decode_pointer(lo, hi), size))
        return message, size

    def scalar(self, name):
        reference = [self.refs[name]]
        return (self.fmu.getBoolean(reference) if name == "valid"
                else self.fmu.getInteger(reference))[0]

    def close(self):
        self.fmu.terminate()
        self.fmu.freeInstance()


def view_divergence(view, time):
    """The first difference between one SensorView and the closed-form source."""
    want = {"host": scene.HOST_ID, "sensor": scene.SENSOR_ID, "mounting": False,
            "timestamp": scene.timestamp(time), "objects": scene.ground_truth(time)}
    truth = view.global_ground_truth
    got = {"host": truth.host_vehicle_id.value, "sensor": view.sensor_id.value,
           "mounting": view.HasField("mounting_position"),
           "timestamp": (view.timestamp.seconds, view.timestamp.nanos),
           "objects": [{"id": o.id.value, **pose(o.base),
                        "length": o.base.dimension.length, "width": o.base.dimension.width,
                        "height": o.base.dimension.height} for o in truth.moving_object]}
    for field in ("host", "sensor", "mounting", "timestamp"):
        if want[field] != got[field]:
            return {"field": field, "expected": want[field], "actual": got[field]}
    if [o["id"] for o in want["objects"]] != [o["id"] for o in got["objects"]]:
        return {"field": "ids", "actual": [o["id"] for o in got["objects"]]}
    for expected, actual in zip(want["objects"], got["objects"]):
        for field, value in expected.items():
            if abs(actual[field] - value) > scene.TOL:
                return {"object": expected["id"], "field": field, "expected": value,
                        "actual": actual[field]}
    return None


def pose(base):
    return {"x": base.position.x, "y": base.position.y, "z": base.position.z,
            "yaw": base.orientation.yaw, "pitch": base.orientation.pitch,
            "roll": base.orientation.roll}


def observation(step, data):
    return {"t_ns": (step + 1) * STEP_NS, "seconds": data.timestamp.seconds,
            "nanos": data.timestamp.nanos, "detections": [{
                "ground_truth_id": o.header.ground_truth_id[0].value,
                "tracking_id": o.header.tracking_id.value, **pose(o.base),
                "existence_probability": o.header.existence_probability,
                "length": o.base.dimension.length, "width": o.base.dimension.width,
                "height": o.base.dimension.height} for o in data.moving_object]}


def run(fmus, nominal_range, lag=False):
    """Source and sensor for STEPS Periods. With `lag`, the sensor receives the
    previous step's SensorView: a connection delayed by one Period. OSMP keeps
    the previous output buffer valid for exactly that one step."""
    from osi_sensordata_pb2 import SensorData
    from osi_sensorview_pb2 import SensorView
    from osi_sensorviewconfiguration_pb2 import SensorViewConfiguration
    source = Fmu(fmus["OSMPDummySource"], "source")
    sensor = Fmu(fmus["OSMPDummySensor"], "sensor", {"nominalrange": nominal_range})
    request, _ = sensor.message("OSMPSensorViewInConfigRequest", SensorViewConfiguration())
    steps, view_bytes, data_bytes, previous = [], [], [], None
    started = time.perf_counter()
    for step in range(STEPS):
        point = step * scene.STEP_S
        source.fmu.doStep(point, scene.STEP_S)
        view, size = source.message("OSMPSensorViewOut", SensorView())
        divergence = view_divergence(view, point + scene.STEP_S)
        require(divergence is None, f"SensorView at step {step} leaves the closed form: "
                f"{divergence}")
        view_bytes.append(size)
        current = source.integers("OSMPSensorViewOut")
        sensor.set_integers("OSMPSensorViewIn", previous if lag and previous else current)
        sensor.fmu.doStep(point, scene.STEP_S)
        data, size = sensor.message("OSMPSensorDataOut", SensorData())
        data_bytes.append(size)
        require(sensor.scalar("valid") and sensor.scalar("count") == len(data.moving_object)
                and all(o.header.sensor_id[0].value == scene.SENSOR_ID
                        for o in data.moving_object), f"sensor outputs at step {step}")
        steps.append(observation(step, data))
        previous = current
    elapsed = time.perf_counter() - started
    for fmu in (sensor, source):
        fmu.close()
    return steps, {
        "config_request": {"update_cycle_time_ns": request.update_cycle_time.seconds * 10**9
                           + request.update_cycle_time.nanos,
                           "range_m": request.range,
                           "field_of_view_horizontal_rad": request.field_of_view_horizontal},
        "sensor_view_bytes": {"min": min(view_bytes), "max": max(view_bytes)},
        "sensor_data_bytes": {"min": min(data_bytes), "max": max(data_bytes)},
        "wall_s": round(elapsed, 3),
        "steps_per_wall_s": round(STEPS / elapsed),
    }


def expected_slice(nominal_range):
    steps = []
    for step in range(STEPS):
        now = step * scene.STEP_S + scene.STEP_S
        seconds, nanos = scene.timestamp(now)
        steps.append({"t_ns": (step + 1) * STEP_NS, "seconds": seconds, "nanos": nanos,
                      "detections": scene.detections(scene.ground_truth(now), scene.HOST_ID,
                                                     nominal_range)})
    return steps


def transitions(steps):
    """Each time the set of reported vehicles changes."""
    found, last = [], None
    for step in steps:
        ids = [d["ground_truth_id"] for d in step["detections"]]
        if ids != last:
            found.append({"t_ns": step["t_ns"], "ground_truth_ids": ids})
            last = ids
    return found


def execute(fmus):
    nominal, observed = run(fmus, NOMINAL_RANGE_M)
    repeat, _ = run(fmus, NOMINAL_RANGE_M)
    require(nominal == repeat, "two runs of the same FMUs differ")
    expected = expected_slice(NOMINAL_RANGE_M)
    divergence = scene.first_divergence(expected, nominal)
    require(divergence is None, f"sensor leaves the expected slice: {divergence}")
    ranged, _ = run(fmus, CONTROL_RANGE_M)
    divergence = scene.first_divergence(expected_slice(CONTROL_RANGE_M), ranged)
    require(divergence is None, f"nominalrange {CONTROL_RANGE_M} leaves its slice: {divergence}")
    require(scene.first_divergence(expected, ranged) is not None,
            "the nominal range parameter changes nothing")
    # The control must fail for its own reason: a position one Period old.
    lagged, _ = run(fmus, NOMINAL_RANGE_M, lag=True)
    late = scene.first_divergence(expected, lagged)
    require(late is not None and late["field"] in scene.POSE and late["step"] == 1,
            f"a one-Period input delay was not detected as a position error: {late}")
    return expected, {
        "steps": STEPS, "period_ns": STEP_NS, "nominal_range_m": NOMINAL_RANGE_M,
        "expected_slice_agrees": True, "repeat_identical": True,
        "detections": sum(len(step["detections"]) for step in expected),
        "transitions": transitions(expected),
        "range_parameter": {"nominal_range_m": CONTROL_RANGE_M, "agrees_with_its_slice": True,
                            "transitions": transitions(ranged)},
        "failing_control": {"one-period-input-delay": late},
        "observed": observed,
    }


def digests(bundle):
    return {str(p.relative_to(bundle)): sha256(p)
            for p in sorted(bundle.rglob("*")) if p.is_file() and p.name != "bundle.json"}


CONTRACT = {
    "units": "metres, radians, seconds; OSI conventions",
    "frame": "sensor frame = host vehicle frame here: the source sets no bbcenter_to_rear "
             "and no mounting position, so both offsets are zero; x forward, y left, z up",
    "sample_time": "row t_ns is the end of communication step [t_ns - 20 ms, t_ns]",
    "freshness": "the sensor consumes the SensorView the source produced in the same step",
    "host_vehicle_id": scene.HOST_ID,
    "generator": "OSMPDummySource: deterministic, synthetic closed-form motion, not a recording",
}


def main(work):
    require(platform.machine() == "x86_64", "the supported acceptance platform is Linux x86-64")
    bundle, evidence = work / "bundle", work / "evidence"
    for directory in (bundle, evidence):
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True)
    gate = subprocess.run([sys.executable, "-m", "pytest", "-q", HERE], capture_output=True,
                          text=True, cwd=HERE)
    (evidence / "gate-tests.log").write_text(gate.stdout + gate.stderr)
    require(gate.returncode == 0, "gate tests failed")
    fmus, build_record = build_twice(work, bundle, evidence)
    inspections = {target: inspect_fmu.inspect(archive, work / "inspect" / target)
                   for target, archive in fmus.items()}
    write_json(evidence / "fmu-inspection.json", inspections)
    osi_bindings(work / "build-1", work / "osi-python")
    expected, runs = execute(fmus)
    write_json(bundle / "references" / "expected-detections.json",
               {"contract": CONTRACT, "nominal_range_m": NOMINAL_RANGE_M, "steps": expected})
    digest = digests(bundle)
    write_json(bundle / "bundle.json", {
        "sources": json.loads((HERE / "sources.json").read_text()),
        "toolchain": toolchain(), "digests": digest})
    write_json(evidence / "report.json", {
        "bundle_sha256": sha256(bundle / "bundle.json"),
        "build": build_record,
        "binaries": {target: {"so_sha256": report["binary"]["sha256"],
                              "archive_sha256": report["archive_sha256"],
                              "needed": report["binary"]["needed"],
                              "sil_gaps": report["sil_gaps"]}
                     for target, report in inspections.items()},
        "runs": runs,
        "observed_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    })


if __name__ == "__main__":
    main(Path(sys.argv[1]))
