"""Prepare the OSMP qualification bundles (issue #233). Usage: prepare.py <prepared>.

Runs in the preparation stage of the Dockerfile, with the pinned OSMP
sources, the compiler, `protoc` and installed SiL. It runs no FMU code:

1. Builds `OSMPDummySource` and `OSMPDummySensor` exactly as
   `proofs/osmp-sensor` does and compares them with the #230 pins.
2. Inspects both archives statically: `sil fmi inspect` with every mapping
   the Runs use, the declared instantiation restrictions, the archive
   members and the native libraries the loader resolves.
3. Writes the independent references of `references.py` and converts them
   with `sil recording csv`. Compares the predicted late reference with the nominal one
   under the bundle's contract: that first divergence is the prediction the
   late control must meet.
4. Writes one bundle per matrix case into /bundles, the cost Manifests, the
   initialization check of each FMU instance and the expectations the
   runtime checks.

Every check raises, so a failed preparation cannot write a bundle.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "proofs" / "osmp-sensor"))

import prepare as osmp_prepare  # noqa: E402
import references  # noqa: E402
from sil.compare import compare, read_contract  # noqa: E402
from sil.csv_recording import convert  # noqa: E402
from sil.fmi.inspection import inspect  # noqa: E402
from sil.manifest import Manifest, SubscriberRoute  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bundles = _load("osmp_bundle_prepare", ROOT / "examples" / "bundle" / "prepare.py")

BUNDLES = Path("/bundles")
RUNTIME = bundles.Runtime(Path("/opt/sil/python/bin"), Path("/opt/sil/native/bin"))
# The decoder and its OSI classes import Protobuf; upb is its native module.
MODULES = [*bundles.MODULES, "google.protobuf", "google._upb"]
EXCLUDED = {"executables": [*bundles.EXCLUDED["executables"], "protoc", "cmake"],
            "modules": ["fmpy"]}
SOURCE, SENSOR = "OSMPDummySource", "OSMPDummySensor"
VIEW = references.VIEW
STATUS_SCHEMA = "sensor.Status"
# Above the largest SensorView (2004 B) and SensorData (3014 B) of #244.
BOUND_BYTES = 4096
# Below the smallest SensorData of #244 (2438 B): the size control.
SMALL_BOUND_BYTES = 1024
PAYLOAD_SCHEMAS = {
    "osi.Payload": {"fields": [{"name": "payload", "type": "u8", "count": BOUND_BYTES},
                               {"name": "payload_length", "type": "u32"}]},
    "osi.SmallPayload": {"fields": [
        {"name": "payload", "type": "u8", "count": SMALL_BOUND_BYTES},
        {"name": "payload_length", "type": "u32"}]},
}
PARTICIPANT_TIMEOUT_MS = 30_000
MISBOUND_VARIABLE = "objectcount"
COST_STEPS = {"startup": 1, "long": references.STEPS}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


write_json = references.write_json


# Build and static inspection --------------------------------------------------

def build(work: Path) -> tuple[dict[str, Path], dict]:
    epoch = osmp_prepare.command(
        "git", "-C", osmp_prepare.OSMP_CHECKOUT, "log", "-1", "--format=%ct").strip()
    archives, _ = osmp_prepare.build(work / "osmp-build", epoch)
    pinned = json.loads(
        (ROOT / "proofs" / "osmp-sensor" / "evidence" / "fmu-inspection.json").read_text())
    record = {}
    for name, archive in archives.items():
        with zipfile.ZipFile(archive) as opened:
            built = hashlib.sha256(opened.read(f"binaries/linux64/{name}.so")).hexdigest()
        record[name] = {"archive_sha256": sha256(archive), "binary_sha256": built,
                        # Reported, not required: the #230 build is the pin.
                        "binary_matches_230_pin": built == pinned[name]["binary"]["sha256"]}
    return archives, record


def sensor_binds(sensor: str, count_variable: str = "count") -> list[str]:
    return [f"{VIEW}:payload=OSMPSensorViewIn",
            f"{sensor}.SensorData:payload=OSMPSensorDataOut",
            f"{sensor}.Status:valid=valid",
            f"{sensor}.Status:count={count_variable}"]


SOURCE_BINDS = [f"{VIEW}:payload=OSMPSensorViewOut"]


def sensor_starts(nominal_range: float) -> list[str]:
    """`nominalrange` is set only where it differs from its start value."""
    if nominal_range == references.SENSORS["sensor"]:
        return []
    return [f"nominalrange={nominal_range!r}"]


def inspection(fmus: dict[str, Path]) -> dict:
    """Both archives are compatible with the mappings the Runs use."""
    schemas = {**PAYLOAD_SCHEMAS, STATUS_SCHEMA: references.SCHEMAS[STATUS_SCHEMA]}
    mappings = {
        SOURCE: ({VIEW: ("osi.Payload", "out")}, SOURCE_BINDS),
        SENSOR: ({VIEW: ("osi.Payload", "in"), "sensor.SensorData": ("osi.Payload", "out"),
                  "sensor.Status": (STATUS_SCHEMA, "out")}, sensor_binds("sensor")),
    }
    reports = {}
    for name, (channels, bound) in mappings.items():
        mapping = {"sil_fmi_mapping": 1, "schemas": schemas, "bind": bound,
                   "channels": {channel: {"schema": schema, "direction": direction}
                                for channel, (schema, direction) in channels.items()}}
        report = inspect(fmus[name], mapping)
        require(report["verdict"] == "compatible",
                f"{name}: {report['unusable']} {report['mapping']}")
        reports[name] = {"verdict": report["verdict"], "osmp": report["osmp"],
                         "unbound": report["mapping"]["unbound"]}
    return reports


def packaging(archive: Path, name: str, work: Path) -> dict:
    """What the archive declares and needs beyond itself, read without running it."""
    with zipfile.ZipFile(archive) as opened:
        members = sorted(opened.namelist())
        root = ElementTree.fromstring(opened.read("modelDescription.xml"))
        binary = work / f"{name}.so"
        binary.write_bytes(opened.read(f"binaries/linux64/{name}.so"))
    co_simulation = root.find("CoSimulation").attrib
    flags = ("canBeInstantiatedOnlyOncePerProcess", "needsExecutionTool",
             "canGetAndSetFMUstate", "canSerializeFMUstate")
    return {
        "members": members,
        "resources": [m for m in members if m.startswith("resources/")],
        "platforms": sorted({m.split("/")[1] for m in members
                             if m.startswith("binaries/") and m.count("/") > 1}),
        # An absent flag is false (FMI 2.0 §4.3.1).
        "declared": {flag: co_simulation.get(flag, "false") for flag in flags},
        "native_libraries": bundles.native_libraries(binary),
    }


# Manifests ------------------------------------------------------------------

def manifest(fmus: dict[str, Path], edge: Path, *, sensors: dict[str, float],
             steps: int = references.STEPS, source: str = "fmu",
             view_latency_ns: int | None = 0, data_schema: str = "osi.Payload",
             count_variable: str = "count", decode: bool = True) -> Manifest:
    """The source, one Process participant per sensor and the decoder.

    The source publishes the SensorView of the end of its Step in the Slot
    it is activated in. With Latency 0 each sensor, ordered after it, steps
    over the same interval with that SensorView. The decoder, ordered last,
    republishes each payload as typed fields in the Slot it arrives in.
    """
    m = Manifest(duration_ns=steps * references.PERIOD_NS)
    m.add_schemas({"osi.Payload": PAYLOAD_SCHEMAS["osi.Payload"],
                   **({data_schema: PAYLOAD_SCHEMAS[data_schema]}
                      if data_schema != "osi.Payload" else {}),
                   STATUS_SCHEMA: references.SCHEMAS[STATUS_SCHEMA],
                   **({"osi.GroundTruth": references.SCHEMAS["osi.GroundTruth"],
                       "osi.Detections": references.SCHEMAS["osi.Detections"]}
                      if decode else {})})
    m.add_channel(VIEW, schema="osi.Payload", latency_ns=view_latency_ns)
    decoded_view = decode and source == "fmu" and view_latency_ns == 0
    if decoded_view:
        m.add_channel(references.TRUTH, schema="osi.GroundTruth")
    if source == "fmu":
        command = ["python3", "-m", "sil.fmi", str(fmus[SOURCE]),
                   *(a for b in SOURCE_BINDS for a in ("--bind", b))]
    else:
        command = ["python3", "-m", "sil.participant", f"{edge}:UnparseableView"]
    m.add_process("source", command=command, step_period_ns=references.PERIOD_NS,
                  publishes=[VIEW], priority=0)
    for sensor, nominal_range in sensors.items():
        m.add_channel(f"{sensor}.SensorData", schema=data_schema, latency_ns=0)
        m.add_channel(references.status_channel(sensor), schema=STATUS_SCHEMA)
        if decode:
            m.add_channel(references.detections_channel(sensor), schema="osi.Detections")
        starts = [a for start in sensor_starts(nominal_range) for a in ("--start", start)]
        m.add_process(
            sensor,
            command=["python3", "-m", "sil.fmi", str(fmus[SENSOR]), *starts,
                     *(a for b in sensor_binds(sensor, count_variable)
                       for a in ("--bind", b))],
            step_period_ns=references.PERIOD_NS,
            subscribes=[SubscriberRoute(VIEW, capacity=2)],
            publishes=[f"{sensor}.SensorData", references.status_channel(sensor)],
            priority=1)
    if decode:
        inputs = ([VIEW] if decoded_view else []) + [f"{s}.SensorData" for s in sensors]
        m.add_process(
            "decoder",
            command=["python3", "-m", "sil.participant", f"{edge}:Decoder"],
            step_period_ns=references.PERIOD_NS,
            subscribes=[SubscriberRoute(channel, capacity=2) for channel in inputs],
            publishes=([references.TRUTH] if decoded_view else [])
            + [references.detections_channel(s) for s in sensors],
            priority=2)
    return m


# References -----------------------------------------------------------------

REFERENCE_SENSORS = {"nominal": references.SENSORS, "late": references.SENSORS,
                     "unparseable": {"sensor": references.SENSORS["sensor"]}}


def write_references(work: Path) -> dict[str, dict[str, Path]]:
    """Each reference as CSV, mapping, Recording and receipt, and its contract."""
    written = {}
    for kind, sensors in REFERENCE_SENSORS.items():
        files = {"csv": work / f"{kind}.csv", "mapping": work / f"{kind}.mapping.json",
                 "contract": work / f"{kind}.contract.json",
                 "recording": work / f"{kind}.mcap", "receipt": work / f"{kind}.receipt.json"}
        references.write_csv(files["csv"], kind, sensors)
        write_json(files["mapping"], references.mapping(kind, sensors))
        write_json(files["contract"], references.contract(kind, sensors))
        write_json(files["receipt"], convert(files["mapping"], files["csv"], files["recording"]))
        written[kind] = files
    return written


def predicted_late_divergence(written: dict, work: Path) -> dict:
    """Where a SensorView one Period late must first leave the nominal reference.

    Both sides are references here, so neither has the Importer's offset.
    The fields that identify a divergence, not the recorded times, are kept.
    """
    contract = work / "late.reference-contract.json"
    write_json(contract, references.contract("late", references.SENSORS, actual_offset_ns=0))
    rules = read_contract(contract)
    for kind in ("late", "nominal"):
        same = compare(rules, written[kind]["recording"], written[kind]["recording"])
        require(same["verdict"] == "pass", f"the {kind} reference does not equal itself")
    report = compare(rules, written["late"]["recording"], written["nominal"]["recording"])
    first = report["first_divergence"]
    require(report["verdict"] == "fail" and first is not None,
            "the late prediction does not differ from the nominal reference")
    return {key: first[key] for key in ("kind", "channel", "field", "observation_ns",
                                        "actual", "expected")}


# Bundles --------------------------------------------------------------------

class Case:
    """One bundle under /bundles, built from the shared prepared files."""

    def __init__(self, name: str, fmus: dict[str, Path], shared: dict):
        self.name = name
        self.bundle = bundles.Bundle(BUNDLES / name)
        self.fmus = {target: self.bundle.copy(archive, "target")
                     for target, archive in fmus.items()}
        self.bundle.copy(shared["license"], "resource", "LICENSE")
        self.edge = self.bundle.copy(HERE / "osi_edge.py", "participant")
        (self.bundle.root / "osi").mkdir()
        for module in sorted(shared["osi"].glob("*.py")):
            self.bundle.copy(module, "participant", f"osi/{module.name}")
        self.shared = shared
        self.runs = []

    def reference(self, kind: str) -> str:
        files = self.shared["references"][kind]
        if f"{kind}.mcap" not in self.bundle.artifacts:
            for key, role in (("csv", "conversion-input"), ("mapping", "conversion-input"),
                              ("recording", "reference"), ("receipt", "receipt")):
                self.bundle.copy(files[key], role)
        return f"{kind}.mcap"

    def contract(self, kind: str) -> str:
        name = f"{kind}.contract.json"
        if name not in self.bundle.artifacts:
            self.bundle.copy(self.shared["references"][kind]["contract"], "contract")
        return name

    def run(self, name: str, *, determinism: bool = True,
            comparisons: tuple[tuple[str, str, str], ...] = (), **options) -> None:
        """One Run; each comparison is (name, contract kind, reference kind)."""
        document = f"{name}.json"
        manifest(self.fmus, self.edge, **options).write(self.bundle.path(document, "manifest"))
        self.runs.append({
            "name": name, "manifest": document, "determinism": determinism,
            "participant_timeout_ms": PARTICIPANT_TIMEOUT_MS,
            "comparisons": [{"name": compared, "contract": self.contract(contract),
                             "reference": self.reference(reference)}
                            for compared, contract, reference in comparisons]})

    def declare(self) -> None:
        declaration = RUNTIME.declaration(self.name, self.bundle.artifacts, self.runs,
                                          self.shared["files"])
        declaration["dependencies"]["python"]["modules"] = MODULES
        declaration["excluded"] = EXCLUDED
        write_json(self.bundle.root / "bundle.json", declaration)


def write_bundles(fmus: dict[str, Path], shared: dict) -> None:
    nominal = Case("nominal", fmus, shared)
    nominal.run("nominal", sensors=references.SENSORS,
                comparisons=(("independent", "nominal", "nominal"),))
    nominal.run("unparseable", source="unparseable",
                sensors=REFERENCE_SENSORS["unparseable"],
                comparisons=(("independent", "unparseable", "unparseable"),))
    nominal.declare()
    late = Case("control-late", fmus, shared)
    late.run("late", view_latency_ns=None, sensors=references.SENSORS,
             comparisons=(("independent", "late", "nominal"),
                          ("predicted", "late", "late")))
    late.declare()
    binding = Case("control-binding", fmus, shared)
    binding.run("binding", determinism=False, sensors=REFERENCE_SENSORS["unparseable"],
                count_variable=MISBOUND_VARIABLE)
    binding.declare()
    size = Case("control-size", fmus, shared)
    size.run("size", determinism=False, sensors=REFERENCE_SENSORS["unparseable"],
             data_schema="osi.SmallPayload")
    size.declare()


def expectations(predicted: dict) -> dict:
    """What the runtime requires of each case, decided before any Run."""
    return {
        "nominal": {"status": "pass", "timeout_s": 600},
        "control-late": {"status": "behavioral-failure", "timeout_s": 600,
                         "run_exit_codes": [0],
                         "comparisons": {"independent": "fail", "predicted": "pass"},
                         "first_divergence": predicted},
        "control-binding": {"status": "manifest-error", "timeout_s": 300,
                            "run_exit_codes": [2],
                            "diagnostic": ["'sensor'", MISBOUND_VARIABLE]},
        "control-size": {"status": "behavioral-failure", "timeout_s": 300,
                         "run_exit_codes": [1],
                         "diagnostic": ["OSMPSensorDataOut",
                                        f"the Channel carries {SMALL_BOUND_BYTES}"]},
    }


def initial_spec(fmus: dict[str, Path]) -> dict:
    """Each FMU instance of the nominal Run, for `initial.py`.

    The bindings and start values are the nominal Manifest's. Each sensor
    also binds its configuration request, a calculated parameter, so that
    the check reads it too.
    """
    installed = {name: str(BUNDLES / "nominal" / archive.name) for name, archive in fmus.items()}
    payload = PAYLOAD_SCHEMAS["osi.Payload"]
    schemas = {"osi.Payload": payload, STATUS_SCHEMA: references.SCHEMAS[STATUS_SCHEMA]}
    expected = references.initial_outputs(references.SENSORS)
    spec = {"source": {"fmu": installed[SOURCE], "binds": SOURCE_BINDS, "starts": [],
                       "schemas": schemas,
                       "channels": {VIEW: {"schema": "osi.Payload", "direction": "out"}},
                       "expected": expected["source"]}}
    for sensor, nominal_range in references.SENSORS.items():
        request = references.config_request_channel(sensor)
        spec[sensor] = {
            "fmu": installed[SENSOR],
            "binds": [*sensor_binds(sensor), f"{request}:payload=OSMPSensorViewInConfigRequest"],
            "starts": sensor_starts(nominal_range), "schemas": schemas,
            "channels": {VIEW: {"schema": "osi.Payload", "direction": "in"},
                         f"{sensor}.SensorData": {"schema": "osi.Payload", "direction": "out"},
                         references.status_channel(sensor): {"schema": STATUS_SCHEMA,
                                                             "direction": "out"},
                         request: {"schema": "osi.Payload", "direction": "out"}},
            "expected": expected[sensor]}
    return spec


def cost_manifests(fmus: dict[str, Path], out: Path) -> None:
    """The source and one sensor, the #244 shape without the decoder."""
    out.mkdir(parents=True)
    installed = {name: BUNDLES / "nominal" / archive.name for name, archive in fmus.items()}
    for name, steps in COST_STEPS.items():
        manifest(installed, BUNDLES / "nominal" / "osi_edge.py", steps=steps,
                 sensors=REFERENCE_SENSORS["unparseable"], decode=False
                 ).write(out / f"{name}.json")


def main(prepared: Path) -> None:
    work = prepared / "work"
    work.mkdir(parents=True)
    report = prepared / "preparation"
    report.mkdir()
    gate = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                           HERE], capture_output=True, text=True, cwd=HERE)
    (report / "gate-tests.log").write_text(gate.stdout + gate.stderr)
    require(gate.returncode == 0, f"gate tests failed:\n{gate.stdout[-2000:]}")
    fmus, built = build(work)
    static = {"inspection": inspection(fmus),
              "packaging": {name: packaging(archive, name, work)
                            for name, archive in fmus.items()}}
    osi = work / "osi-python"
    osmp_prepare.osi_bindings(work / "osmp-build", osi)
    written = write_references(work)
    predicted = predicted_late_divergence(written, work)
    files = sorted({library for record in static["packaging"].values()
                    for library in record["native_libraries"]})
    shared = {"license": osmp_prepare.OSMP_CHECKOUT / "LICENSE", "osi": osi,
              "references": written, "files": tuple(files)}
    write_bundles(fmus, shared)
    cost_manifests(fmus, prepared / "cost")
    write_json(prepared / "expected.json", expectations(predicted))
    write_json(prepared / "initial.json", initial_spec(fmus))
    write_json(report / "report.json", {
        "issue": 233,
        "source_revision": (ROOT / "source-revision.txt").read_text().strip(),
        "build": built, **static,
        "references": {kind: {"csv_sha256": sha256(f["csv"]),
                              "recording_sha256": sha256(f["recording"]),
                              "receipt": json.loads(f["receipt"].read_text())}
                       for kind, f in written.items()},
        "predicted_late_divergence": predicted,
    })
    shutil.rmtree(work)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
