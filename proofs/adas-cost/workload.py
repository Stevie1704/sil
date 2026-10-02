"""The declared reference workload of the cost measurement (issue #229).

One experiment, three execution forms of the same library sources:

| Form | Controller entry | Where the application runs |
| --- | --- | --- |
| `native` | `add_native`, the library's `sil_participant_init` | inside `sil-run`, on the kernel thread |
| `process` | `add_process`, `process_adapter.py` loads the same library with ctypes | a Python child process, Step protocol |
| `fmu` | `add_process`, SiL's FMI Importer over `AdasReference.fmu` | a Python child process, Step protocol, FMI 3.0 |

All Manifests come from `reference_manifest` in examples/adas-reference/:
the same Schemas, Channels, Latencies, Replay participants, routes and
Duration. Only the controller entry differs (`only_the_controller_differs`).

Every pinned quantity is in `declaration()`, which the measurement writes
into its results: Periods, list capacity, active object counts, Message
sizes, route capacity (Burst depth), fan-out, Run lengths, instance counts,
Recording on/off, warm-up and repeats.

Nothing here runs anything.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path

from sil.manifest import Manifest
from sil.schema import MessageType

PROOF_DIR = Path(__file__).resolve().parent
ROOT = PROOF_DIR.parents[1]
EXAMPLE_DIR = ROOT / "examples" / "adas-reference"


def load(name: str, path: Path):
    """An example's module, by its file and under a name of its own, and
    registered, so its own dataclasses can find it."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


manifest = load("adas_cost_manifest", EXAMPLE_DIR / "manifest.py")
prepare = load("adas_cost_prepare", EXAMPLE_DIR / "prepare.py")
equivalence = load("adas_cost_equivalence",
                   ROOT / "proofs" / "adas-equivalence" / "experiment.py")

MS = manifest.MS
PERIOD_NS = manifest.PERIOD_NS
FORMS = ("native", "process", "fmu")
# Each sensor's publication Period, as in the `cadence` maneuver.
SENSOR_PERIODS_NS = {"radar": 20 * MS, "camera": 40 * MS, "ego": 10 * MS}
# Every published list is full: the capacity is the active object count.
ACTIVE_OBJECTS = {"radar": prepare.CAPACITY, "camera": prepare.CAPACITY}
# The lead object approaches from 50 m to 0.25 m every 2 s, so the Run
# passes through both the clear and the hazard mode.
CYCLE_ACTIVATIONS = 200
EGO_SPEED_MPS = 20.0
INSTANCES = (1, 4)
RECORDING = (False, True)
# The observational policy: discarded warm-up Runs, then timed repeats.
WARMUP = 1
REPEATS = 5
# The default simulated Duration of the long Run.
LONG_S = 60


@dataclass(frozen=True)
class Workload:
    """A Run length over the same generated maneuver."""

    name: str
    activations: int

    @property
    def duration_ns(self) -> int:
        return self.activations * PERIOD_NS


def workloads(long_s: int = LONG_S) -> dict[str, Workload]:
    """`startup`: one activation, which a Run cannot be shorter than.
    `ci`: the 200 ms of every authored maneuver. `long`: `long_s` seconds."""
    long_activations = long_s * 1_000_000_000 // PERIOD_NS
    return {w.name: w for w in (Workload("startup", 1), Workload("ci", 20),
                                Workload("long", long_activations))}


@dataclass(frozen=True)
class Row:
    """One measured configuration."""

    form: str
    workload: Workload
    instances: int
    recording: bool

    @property
    def name(self) -> str:
        return (f"{self.form}-{self.workload.name}-x{self.instances}-"
                f"rec{'on' if self.recording else 'off'}")


def matrix(long_s: int = LONG_S) -> list[Row]:
    return [Row(form, workload, instances, recording)
            for workload in workloads(long_s).values()
            for instances in INSTANCES
            for recording in RECORDING
            for form in FORMS]


def instance_names(instances: int) -> tuple[str, ...]:
    return tuple(f"load{i}" for i in range(instances))


# --- the generated maneuver ---------------------------------------------------


def _lead_x_m(k: int) -> float:
    return 50.0 - 0.25 * (k % CYCLE_ACTIVATIONS)


def _radar(k: int) -> str:
    lead = _lead_x_m(k)
    return ";".join(
        f"{i} {lead + 6 * i:.2f} {-1.2 + 0.3 * i:.2f} "
        f"{-5.0 if i == 0 else -1.0:.1f} 0.9"
        for i in range(ACTIVE_OBJECTS["radar"]))


def _camera(k: int) -> str:
    lead = _lead_x_m(k)
    return ";".join(
        f"{100 + i} {lead + 6 * i + 0.5:.2f} {-1.2 + 0.3 * i:.2f} 0.8"
        for i in range(ACTIVE_OBJECTS["camera"]))


def _due(sensor: str, k: int) -> bool:
    return k * PERIOD_NS % SENSOR_PERIODS_NS[sensor] == 0


def authored_rows(activations: int) -> list[dict[str, str]]:
    """The authored maneuver form of prepare.py, one row per activation."""
    return [{"time_ms": str(k * PERIOD_NS // MS),
             "radar": _radar(k) if _due("radar", k) else "",
             "camera": _camera(k) if _due("camera", k) else "",
             "ego_speed_mps": str(EGO_SPEED_MPS) if _due("ego", k) else ""}
            for k in range(activations)]


def write_inputs(workload: Workload, instances: int, out_dir: Path) -> dict:
    """The authored maneuver of `workload` and one input Recording per
    instance, each with its own Channel prefix. Returns their digests."""
    out_dir.mkdir(parents=True, exist_ok=True)
    authored = out_dir / f"{workload.name}.authored.csv"
    with authored.open("w", newline="") as f:
        writer = csv.DictWriter(
            f, ("time_ms", "radar", "camera", "ego_speed_mps"),
            lineterminator="\n")
        writer.writeheader()
        writer.writerows(authored_rows(workload.activations))
    recordings = {name: prepare.prepare_inputs(authored, name, out_dir)
                  for name in instance_names(instances)}
    return {"authored": {"path": authored.name, "sha256": sha256(authored)},
            "recordings": {name: sha256(path)
                           for name, path in recordings.items()}}


# --- the Manifests --------------------------------------------------------------


@dataclass(frozen=True)
class Artifacts:
    library: Path
    fmu: Path


def controller(form: str, artifacts: Artifacts) -> manifest.Controller:
    if form == "native":
        return manifest.native_controller(artifacts.library)
    if form == "process":
        return manifest.process_controller(artifacts.library)
    if form == "fmu":
        return equivalence.fmu_controller(artifacts.fmu)
    raise ValueError(f"unknown form {form!r}")


def row_manifest(row: Row, inputs: Path, artifacts: Artifacts) -> Manifest:
    """`inputs` holds the input Recordings of `row`'s workload."""
    return manifest.reference_manifest(
        inputs, None, maneuvers=instance_names(row.instances),
        controller=controller(row.form, artifacts),
        duration_ns=row.workload.duration_ns)


class FormError(AssertionError):
    """Two forms of one row differ in more than the controller entries."""


def only_the_controller_differs(docs: dict[str, dict], instances: int) -> None:
    """Raises FormError unless the Manifest documents of the forms are equal
    once each controller entry is removed."""
    stripped = {}
    for form, doc in docs.items():
        participants = dict(doc["participants"])
        for name in instance_names(instances):
            participants.pop(name)
        stripped[form] = {**doc, "participants": participants}
    first, *others = stripped
    for form in others:
        if stripped[form] != stripped[first]:
            raise FormError(f"the {form} Manifest differs from the {first} "
                            "Manifest outside the controller entries")


# --- the declaration --------------------------------------------------------------


def message_sizes() -> dict[str, int]:
    return {name: MessageType(name, spec).size
            for name, spec in manifest.SCHEMAS.items()}


def declaration(long_s: int = LONG_S, warmup: int = WARMUP,
                repeats: int = REPEATS) -> dict:
    """Every pinned quantity of the measurement."""
    return {
        "profile": {"name": manifest.PROFILE,
                    "version": manifest.PROFILE_VERSION},
        "controller_period_ns": PERIOD_NS,
        "sensor_periods_ns": SENSOR_PERIODS_NS,
        "list_capacity": prepare.CAPACITY,
        "active_objects": ACTIVE_OBJECTS,
        "message_bytes": message_sizes(),
        "input_latency_ns": manifest.INPUT_LATENCY_NS,
        "output_latency_ns": manifest.OUTPUT_LATENCY_NS,
        # Each input route holds this many Messages and fails on overflow:
        # the Burst depth a route can take. The workload publishes at most
        # one Message per sensor per activation.
        "route_capacity": manifest.INPUT_ROUTE_CAPACITY,
        "max_messages_per_route_per_activation": 1,
        # Each input Channel has one subscriber, its controller; a Command
        # Channel has none in the Run and is recorded.
        "fan_out": {"input": 1, "command": 0},
        "parameters": manifest.PARAMETERS,
        "forms": list(FORMS),
        "workloads": {w.name: {"activations": w.activations,
                               "simulated_s": w.duration_ns / 1e9}
                      for w in workloads(long_s).values()},
        "instances": list(INSTANCES),
        "recording": ["off", "on"],
        "policy": {"warmup_runs": warmup, "timed_repeats": repeats,
                   "statistic": "median of the timed repeats, with min "
                                "and max; peak RSS is the max"},
    }


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
