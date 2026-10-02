"""Drive OSMPDummySource into OSMPDummySensor through the Importer (issue #244).

Runs inside the pinned tool image with no network on Linux x86-64. Usage:
prove.py <work-directory>. It writes `evidence/` there:

1. Builds both FMUs as `proofs/osmp-sensor` builds them, and inspects each
   with `sil-fmi-inspect` and the mapping the Runs use.
2. Nominal: one Run, each FMU in its own Process participant. The source's
   `OSMPSensorViewOut` is published on a Channel as bytes, and the sensor's
   `OSMPSensorViewIn` is handed those bytes by address in the same Slot
   (Latency 0). Every recorded SensorView must equal the closed-form source
   motion, and every recorded SensorData, decoded here and never in SiL,
   must equal the independently computed expected slice of #230.
3. Identity: the Run again; the two Recordings must be byte-identical.
4. Failing controls: with the default Latency the sensor sees each
   SensorView one Period late and must leave the slice; with a SensorData
   Channel bound below the sensor's output, the Run must fail (exit 1) and
   name the variable and the size.
5. Cleanup: nothing is left in the runner's directory after any Run.

Every check raises, so a failed proof cannot write a passing report.
"""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "proofs" / "osmp-sensor"))

import drive  # noqa: E402
import prepare as osmp_prepare  # noqa: E402
import scene  # noqa: E402
from sil import schema  # noqa: E402
from sil.fmi.inspection import inspect  # noqa: E402
from sil.manifest import Manifest, SubscriberRoute  # noqa: E402
from sil.testing import RunResult  # noqa: E402

SIL_RUN = Path("/build/sil-run")
NS_PER_S = 1_000_000_000
STEPS = osmp_prepare.STEPS
NOMINAL_RANGE_M = osmp_prepare.NOMINAL_RANGE_M
# Above the largest SensorView (2004 B) and SensorData (3014 B) of this Run.
BOUND_BYTES = 4096
# Below the smallest SensorData of this Run (2438 B): the size error.
SMALL_BOUND_BYTES = 1024

VIEW, DATA, STATUS = "osi.SensorView", "osi.SensorData", "sensor.Status"
SCHEMAS = {
    "osi.Payload": {"fields": [
        {"name": "payload", "type": "u8", "count": BOUND_BYTES},
        {"name": "payload_length", "type": "u32"},
    ]},
    "osi.SmallPayload": {"fields": [
        {"name": "payload", "type": "u8", "count": SMALL_BOUND_BYTES},
        {"name": "payload_length", "type": "u32"},
    ]},
    "sensor.Status": {"fields": [
        {"name": "valid", "type": "u8"},
        {"name": "count", "type": "i32"},
    ]},
}
SOURCE_BINDS = [f"{VIEW}:payload=OSMPSensorViewOut"]
SENSOR_BINDS = [
    f"{VIEW}:payload=OSMPSensorViewIn",
    f"{DATA}:payload=OSMPSensorDataOut",
    f"{STATUS}:valid=valid",
    f"{STATUS}:count=count",
]


def step_end_s(t_ns: int) -> float:
    """The FMU time at the end of the Step from the Slot at `t_ns`.

    The Importer hands `fmi2DoStep` the communication point `t / 1e9` and
    the step `dt / 1e9`, each from integer nanoseconds, and both FMUs stamp
    their output with the sum of the two doubles. That sum truncates to
    whole nanoseconds, so the expected values are computed at the same
    double rather than at `(t + dt) / 1e9`.
    """
    return t_ns / NS_PER_S + scene.STEP_NS / NS_PER_S


def expected_slice() -> list[dict]:
    """The #230 expected slice, at the Importer's communication points."""
    steps = []
    for step in range(STEPS):
        now = step_end_s(step * scene.STEP_NS)
        seconds, nanos = scene.timestamp(now)
        steps.append({
            "t_ns": (step + 1) * scene.STEP_NS, "seconds": seconds,
            "nanos": nanos,
            "detections": scene.detections(
                scene.ground_truth(now), scene.HOST_ID, NOMINAL_RANGE_M),
        })
    return steps


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def binds(arguments: list[str]) -> list[str]:
    return [argument for bind in arguments for argument in ("--bind", bind)]


def manifest(fmus: dict[str, Path], *, view_latency_ns: int | None = 0,
             data_schema: str = "osi.Payload") -> Manifest:
    """The source and the sensor, each in its own Process participant.

    The source publishes the SensorView it reached at the end of its Step in
    the Slot it was activated in. With Latency 0 the sensor, ordered after
    it, steps over the same interval with that SensorView: the sensor of
    #230 consumes the ground truth of the instant its own Step ends at.
    """
    m = Manifest(duration_ns=STEPS * scene.STEP_NS)
    m.add_schemas({name: SCHEMAS[name]
                   for name in ("osi.Payload", data_schema, "sensor.Status")})
    m.add_channel(VIEW, schema="osi.Payload", latency_ns=view_latency_ns)
    m.add_channel(DATA, schema=data_schema)
    m.add_channel(STATUS, schema="sensor.Status")
    m.add_process(
        "source",
        command=[sys.executable, "-m", "sil.fmi",
                 str(fmus["OSMPDummySource"]), *binds(SOURCE_BINDS)],
        step_period_ns=scene.STEP_NS, publishes=[VIEW], priority=0,
    )
    m.add_process(
        "sensor",
        command=[sys.executable, "-m", "sil.fmi",
                 str(fmus["OSMPDummySensor"]), *binds(SENSOR_BINDS)],
        step_period_ns=scene.STEP_NS,
        subscribes=[SubscriberRoute(VIEW, capacity=2)],
        publishes=[DATA, STATUS], priority=1,
    )
    return m


@dataclass
class SilRun:
    returncode: int
    stderr: str
    mcap: Path
    leftovers: list[str]
    result: RunResult | None


def run_sil(m: Manifest, directory: Path) -> SilRun:
    """One Run at the run boundary, from a directory of its own.

    Whatever is left in that directory afterwards, beside the manifest and
    the Recording, is what the Run failed to remove.
    """
    directory.mkdir(parents=True)
    reference = m.write(directory / "manifest.json")
    mcap = directory / "out.mcap"
    process = subprocess.run(
        [str(SIL_RUN), str(reference.path), "-o", str(mcap)],
        capture_output=True, text=True, cwd=directory,
    )
    leftovers = sorted(
        str(path.relative_to(directory)) for path in directory.iterdir()
        if path.name not in ("manifest.json", "out.mcap",
                             "out.mcap.provenance.json")
    )
    result = None
    if process.returncode == 0:
        doc = m.to_doc()
        result = RunResult(
            mcap_path=mcap, manifest_hash=reference.hash,
            _types=schema.load(doc["schemas"]),
            _topic_schemas={n: c["schema"] for n, c in doc["channels"].items()},
        )
    require(not leftovers, f"the Run in {directory.name} left {leftovers}")
    return SilRun(process.returncode, process.stderr, mcap, leftovers, result)


def payloads(run: SilRun, channel: str) -> list[tuple[int, bytes]]:
    """Each recorded payload of one Channel, cut to its stated length."""
    return [
        (t, bytes(fields["payload"][:fields["payload_length"]]))
        for t, fields in run.result.messages(channel)
    ]


def sensor_steps(run: SilRun) -> tuple[list[dict], dict]:
    """The decoded SensorData of every Step, as #230 records an observation.

    The Message published in the Slot at `t` is what the sensor reached at
    the end of its Step `[t, t + 20 ms]`: row `t_ns = t + 20 ms`.
    """
    from osi_sensordata_pb2 import SensorData
    steps, sizes = [], []
    status = run.result.messages(STATUS)
    for (t, data), (_, fields) in zip(payloads(run, DATA), status, strict=True):
        message = SensorData()
        message.ParseFromString(data)
        require(fields["valid"] == 1
                and fields["count"] == len(message.moving_object),
                f"sensor status at {t} ns: {fields}")
        steps.append(osmp_prepare.observation(t // scene.STEP_NS, message))
        sizes.append(len(data))
    return steps, {"min": min(sizes), "max": max(sizes)}


def check_views(run: SilRun) -> dict:
    """Every recorded SensorView must equal the closed-form source motion."""
    from osi_sensorview_pb2 import SensorView
    sizes = []
    for t, data in payloads(run, VIEW):
        view = SensorView()
        view.ParseFromString(data)
        divergence = drive.view_divergence(view, step_end_s(t))
        require(divergence is None,
                f"SensorView at {t} ns leaves the closed form: {divergence}")
        sizes.append(len(data))
    require(len(sizes) == STEPS, f"{len(sizes)} SensorViews for {STEPS} Steps")
    return {"messages": len(sizes), "agrees_with_closed_form": True,
            "bytes": {"min": min(sizes), "max": max(sizes)}}


def nominal(fmus: dict[str, Path], work: Path) -> dict:
    run = run_sil(manifest(fmus), work / "nominal")
    require(run.returncode == 0, f"nominal Run: {run.stderr[-2000:]}")
    views = check_views(run)
    steps, sizes = sensor_steps(run)
    expected = expected_slice()
    divergence = scene.first_divergence(expected, steps)
    require(divergence is None, f"sensor leaves the expected slice: {divergence}")
    repeat = run_sil(manifest(fmus), work / "repeat")
    require(repeat.returncode == 0, f"repeat Run: {repeat.stderr[-2000:]}")
    identical = run.mcap.read_bytes() == repeat.mcap.read_bytes()
    require(identical, "two Runs of one Manifest record different bytes")
    return {
        "steps": STEPS, "period_ns": scene.STEP_NS,
        "nominal_range_m": NOMINAL_RANGE_M,
        "sensor_view_latency_ns": 0,
        "sensor_views": views,
        "sensor_data": {"messages": len(steps), "bytes": sizes,
                        "expected_slice_agrees": True,
                        "detections": sum(len(s["detections"]) for s in steps)},
        "recording_sha256": sha256(run.mcap),
        "repeat_identical": identical,
    }


def late_connection(fmus: dict[str, Path], work: Path) -> dict:
    """With the default Latency the sensor sees each SensorView one Step late.

    Its first Step has no SensorView at all, so the comparison must fail.
    """
    run = run_sil(manifest(fmus, view_latency_ns=None), work / "late")
    require(run.returncode == 0, f"late Run: {run.stderr[-2000:]}")
    from osi_sensordata_pb2 import SensorData
    steps = []
    for t, data in payloads(run, DATA):
        message = SensorData()
        message.ParseFromString(data)
        steps.append(osmp_prepare.observation(t // scene.STEP_NS, message))
    divergence = scene.first_divergence(expected_slice(), steps)
    require(divergence is not None,
            "a SensorView one Period late was not detected")
    return {"sensor_view_latency": "default (next activation)",
            "first_divergence": divergence}


def size_error(fmus: dict[str, Path], work: Path) -> dict:
    """A SensorData Channel bound below the sensor's output fails the Run."""
    run = run_sil(manifest(fmus, data_schema="osi.SmallPayload"),
                  work / "size-error")
    diagnostic = [line for line in run.stderr.splitlines()
                  if "OSMPSensorDataOut" in line]
    require(run.returncode == 1, f"size error: exit {run.returncode}")
    require(diagnostic and any(
        f"the Channel carries {SMALL_BOUND_BYTES}" in line
        for line in diagnostic),
        f"size error: no diagnostic names the variable and the bound: "
        f"{run.stderr[-2000:]}")
    return {"bound_bytes": SMALL_BOUND_BYTES, "exit_code": run.returncode,
            "diagnostic": diagnostic}


def inspection(fmus: dict[str, Path]) -> dict:
    """Both archives are compatible and accept the mappings the Runs use."""
    channels = {
        "OSMPDummySource": ({VIEW: "out"}, SOURCE_BINDS),
        "OSMPDummySensor": ({VIEW: "in", DATA: "out", STATUS: "out"},
                            SENSOR_BINDS),
    }
    schemas_of = {VIEW: "osi.Payload", DATA: "osi.Payload",
                  STATUS: "sensor.Status"}
    reports = {}
    for name, (directions, bound) in channels.items():
        mapping = {
            "sil_fmi_mapping": 1, "schemas": SCHEMAS, "bind": bound,
            "channels": {channel: {"schema": schemas_of[channel],
                                   "direction": direction}
                         for channel, direction in directions.items()},
        }
        report = inspect(fmus[name], mapping)
        require(report["verdict"] == "compatible",
                f"{name}: {report['unusable']} {report['mapping']}")
        reports[name] = {"verdict": report["verdict"], "osmp": report["osmp"],
                         "unbound": report["mapping"]["unbound"]}
    return reports


def build(work: Path) -> tuple[dict[str, Path], dict]:
    """Both FMUs as proofs/osmp-sensor builds them, against the #230 pins."""
    epoch = osmp_prepare.command(
        "git", "-C", osmp_prepare.OSMP_CHECKOUT, "log", "-1", "--format=%ct"
    ).strip()
    archives, _ = osmp_prepare.build(work / "osmp-build", epoch)
    pinned = json.loads(
        (ROOT / "proofs" / "osmp-sensor" / "evidence" / "fmu-inspection.json")
        .read_text())
    record = {}
    for name, archive in archives.items():
        with zipfile.ZipFile(archive) as opened:
            built = hashlib.sha256(
                opened.read(f"binaries/linux64/{name}.so")).hexdigest()
        record[name] = {
            "archive_sha256": sha256(archive), "binary_sha256": built,
            # Reported, not required: the #230 build is the pin.
            "binary_matches_230_pin": built == pinned[name]["binary"]["sha256"],
        }
    return archives, record


def main(work: Path) -> None:
    require((platform.system(), platform.machine()) == ("Linux", "x86_64"),
            "the OSMP mapping is qualified on Linux x86-64")
    evidence = work / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    scratch = work / "scratch"
    scratch.mkdir(exist_ok=True)
    fmus, built = build(scratch)
    osmp_prepare.osi_bindings(scratch / "osmp-build", scratch / "osi-python")
    run = nominal(fmus, scratch)
    report = {
        "issue": 244,
        "source_revision": (ROOT / "source-revision.txt").read_text().strip(),
        "build": built,
        "inspection": inspection(fmus),
        "nominal": run,
        "failing_controls": {
            "late_connection": late_connection(fmus, scratch),
            "size_error": size_error(fmus, scratch),
        },
        "cleanup": "nothing left in the runner's directory after any Run",
    }
    write_json(evidence / "report.json", report)
    shutil.rmtree(scratch)
    print(json.dumps({"passed": True}, indent=2))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
