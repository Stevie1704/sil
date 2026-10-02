"""Build, inspect and check the OSMP FMU target, and write its offline bundle.

Runs inside the pinned tool image with no network: the sources were fetched
and verified while the image was built. Usage: prepare.py <work-directory>.

1. Builds `OSMPDummySensor` and `OSMPDummySource` twice with the upstream
   CMake project, unmodified, and pins the first build.
2. Writes a static compatibility report for each archive (no FMU code runs).
3. Runs each FMU under FMPy, an independent FMI 2.0 importer, for `STEPS`
   sensor Periods, each in its own process (see `drive.py` for why). Every
   SensorView of the source must equal the closed-form motion; the sensor,
   fed closed-form SensorViews, must produce the expected slice of `scene.py`.
4. Loads both FMUs into one process and records how that fails.

Every check raises, so a failed qualification cannot write a passing report.
"""
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

import drive
import inspect_fmu
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

def observation(step, data):
    return {"t_ns": (step + 1) * scene.STEP_NS, "seconds": data.timestamp.seconds,
            "nanos": data.timestamp.nanos, "detections": [{
                "ground_truth_id": o.header.ground_truth_id[0].value,
                "tracking_id": o.header.tracking_id.value, **drive.pose(o.base),
                "existence_probability": o.header.existence_probability,
                "length": o.base.dimension.length, "width": o.base.dimension.width,
                "height": o.base.dimension.height} for o in data.moving_object]}


def run_sensor(archive, unzip, nominal_range, lag=False):
    """The sensor alone for STEPS Periods, fed closed-form SensorViews from
    importer-owned buffers. With `lag`, it receives the previous step's
    SensorView: a connection delayed by one Period."""
    from osi_sensordata_pb2 import SensorData
    from osi_sensorviewconfiguration_pb2 import SensorViewConfiguration
    sensor = drive.Fmu(archive, unzip, "sensor", {"nominalrange": nominal_range})
    # The request is published only in initialization mode; upstream resets
    # it to zero once the simulation starts.
    request, _ = sensor.message("OSMPSensorViewInConfigRequest", SensorViewConfiguration())
    sensor.start()
    steps, data_bytes, view_bytes, step_s, previous = [], [], [], 0.0, None
    for step in range(STEPS):
        point = step * scene.STEP_S
        current = drive.Buffer(drive.sensor_view(point + scene.STEP_S))
        view_bytes.append(current.size)
        sensor.set_integers("OSMPSensorViewIn", (previous if lag and previous else current)
                            .integers())
        started = time.perf_counter()
        sensor.fmu.doStep(point, scene.STEP_S)
        step_s += time.perf_counter() - started
        data, size = sensor.message("OSMPSensorDataOut", SensorData())
        data_bytes.append(size)
        valid = sensor.fmu.getBoolean([sensor.refs["valid"]])[0]
        count = sensor.fmu.getInteger([sensor.refs["count"]])[0]
        require(valid and count == len(data.moving_object)
                and all(o.header.sensor_id[0].value == scene.SENSOR_ID
                        for o in data.moving_object), f"sensor outputs at step {step}")
        steps.append(observation(step, data))
        previous = current
    sensor.close()
    return steps, {
        "config_request": {"update_cycle_time_ns": request.update_cycle_time.seconds * 10**9
                           + request.update_cycle_time.nanos,
                           "range_m": request.range,
                           "field_of_view_horizontal_rad": request.field_of_view_horizontal},
        "sensor_view_bytes": {"min": min(view_bytes), "max": max(view_bytes)},
        "sensor_data_bytes": {"min": min(data_bytes), "max": max(data_bytes)},
        "sensor_do_step_s": round(step_s, 4),
        "sensor_do_step_mean_us": round(step_s / STEPS * 1e6, 1),
    }


def child(*arguments, bindings):
    """One drive.py mode in its own process, so that it loads its own FMU."""
    return subprocess.run([sys.executable, HERE / "drive.py", *map(str, arguments)],
                          capture_output=True, text=True, cwd=HERE,
                          env={**os.environ, "PYTHONPATH": f"{HERE}:{bindings}"})


def co_load(fmus, work, bindings):
    """Both FMUs in one process must fail for the documented reason. If this
    changes upstream, the proof stops so that the finding is reviewed again."""
    loaded = child("co-load", fmus["OSMPDummySource"], fmus["OSMPDummySensor"],
                   work / "co-load", bindings=bindings)
    fatal = [line for line in loaded.stderr.splitlines() if "libprotobuf" in line]
    require(loaded.returncode != 0 and any("File already exists in database" in line
                                           for line in fatal),
            f"co-loading both FMUs: exit {loaded.returncode}, stderr {loaded.stderr[-2000:]}")
    return {"one_process": False, "returncode": loaded.returncode, "libprotobuf": fatal}


def expected_slice(nominal_range):
    steps = []
    for step in range(STEPS):
        now = step * scene.STEP_S + scene.STEP_S
        seconds, nanos = scene.timestamp(now)
        steps.append({"t_ns": (step + 1) * scene.STEP_NS, "seconds": seconds, "nanos": nanos,
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


def execute(fmus, work, bindings):
    sensor = (fmus["OSMPDummySensor"], work / "unzip" / "OSMPDummySensor")
    nominal, observed = run_sensor(*sensor, NOMINAL_RANGE_M)
    request = observed["config_request"]
    require(request["update_cycle_time_ns"] == scene.STEP_NS
            and request["range_m"] == NOMINAL_RANGE_M * scene.RANGE_FACTOR,
            f"the sensor's configuration request is {request}")
    repeat, _ = run_sensor(*sensor, NOMINAL_RANGE_M)
    require(nominal == repeat, "two runs of the same FMU differ")
    expected = expected_slice(NOMINAL_RANGE_M)
    divergence = scene.first_divergence(expected, nominal)
    require(divergence is None, f"sensor leaves the expected slice: {divergence}")
    ranged, _ = run_sensor(*sensor, CONTROL_RANGE_M)
    divergence = scene.first_divergence(expected_slice(CONTROL_RANGE_M), ranged)
    require(divergence is None, f"nominalrange {CONTROL_RANGE_M} leaves its slice: {divergence}")
    require(scene.first_divergence(expected, ranged) is not None,
            "the nominal range parameter changes nothing")
    # The control must fail for its own reason: a position one Period old.
    lagged, _ = run_sensor(*sensor, NOMINAL_RANGE_M, lag=True)
    late = scene.first_divergence(expected, lagged)
    require(late is not None and late["field"] in scene.POSE and late["step"] == 1,
            f"a one-Period input delay was not detected as a position error: {late}")
    source = child("source", fmus["OSMPDummySource"], work / "unzip" / "OSMPDummySource",
                   STEPS, bindings=bindings)
    require(source.returncode == 0, f"source check failed: {source.stderr[-2000:]}")
    return expected, {
        "steps": STEPS, "period_ns": scene.STEP_NS, "nominal_range_m": NOMINAL_RANGE_M,
        "expected_slice_agrees": True, "repeat_identical": True,
        "detections": sum(len(step["detections"]) for step in expected),
        "transitions": transitions(expected),
        "range_parameter": {"nominal_range_m": CONTROL_RANGE_M, "agrees_with_its_slice": True,
                            "transitions": transitions(ranged)},
        "failing_control": {"one-period-input-delay": late},
        "source": json.loads(source.stdout),
        "both_fmus_in_one_process": co_load(fmus, work, bindings),
        "observed": observed,
    }


def digests(bundle):
    return {str(p.relative_to(bundle)): sha256(p)
            for p in sorted(bundle.rglob("*")) if p.is_file() and p.name != "bundle.json"}


CONTRACT = {
    "units": "metres, radians, seconds; OSI conventions",
    "frame": "sensor frame = host vehicle frame here: the source sets no bbcenter_to_rear "
             "and no mounting position, so both offsets are zero; x forward, y left, z up",
    "calibration": "none: zero mounting position and orientation",
    "sample_time": "row t_ns is the end of communication step [t_ns - 20 ms, t_ns]",
    "freshness": "the sensor consumes the ground truth of the same instant t_ns",
    "host_vehicle_id": scene.HOST_ID,
    "generator": "OSMPDummySource: deterministic, synthetic closed-form motion, not a recording",
    "outputs": {
        "ground_truth_id": "the source vehicle this detection is",
        "tracking_id": "index of the detection in this step's output; not stable over time",
        "existence_probability": "cos((2 d - R) / R), R = 1.1 nominalrange; a demonstration "
                                 "value, not a calibrated probability",
        "x, y, z, yaw, pitch, roll": "pose relative to the host (sensor) frame",
        "length, width, height": "copied from ground truth",
        "valid, count": "fmi2 outputs: input was present; number of detections",
    },
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
    bindings = work / "osi-python"
    osi_bindings(work / "build-1", bindings)
    expected, runs = execute(fmus, work, bindings)
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
        # FMPy, pure-Python Protobuf and the sensor FMU; the source ran elsewhere.
        "sensor_process_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    })


if __name__ == "__main__":
    main(Path(sys.argv[1]))
