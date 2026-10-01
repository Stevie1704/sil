"""The mixed closed loop of issue #227: its cases, consumption table and
Manifests, declared before any Run.

The ADAS reference controller (docs/adas-reference.md), as its Native
participant or as `AdasReference.fmu`, closes a loop over the qualified ACC
plant FMU (proofs/acc-fmi) through edge Participants (edge.py):

    maneuver --lead--> plant --truth--> radar, camera, ego --lists--> controller
        \\--visibility--> radar, camera                                 |
    plant <--actuation-- actuator <--command----------------------------/

Both forms come from one declaration (`loop_manifest`); only the controller
entry differs. Nothing here runs a Participant or reads a Run's output: the
predicted transitions, the acceptance envelope and the deliberate failures
are fixed here, before measuring.

Consumption, at every Slot t (`consumption_table` states it per case):

1. `maneuver` publishes the lead acceleration for [t, t + 10 ms] and the
   lead visibility at t (Latency 0).
2. `radar`, `camera`, `ego` take the truth sampled at t (published by the
   plant at t - 10 ms; Latency 10 ms) and the visibility at t, and publish
   observations with Sample time t (Latency 0).
3. `controller` takes them at age 0 and publishes the Command for Sample
   time t + 10 ms (Latency 10 ms).
4. `actuator` takes the Command sampled at t (published at t - 10 ms) and
   publishes the plant's acceleration for [t, t + 10 ms] (Latency 0).
5. `plant` steps [t, t + 10 ms] on the held lead acceleration and
   acceleration, and publishes the truth sampled at t + 10 ms.

The feedback Latencies, truth and Command, are positive, so the plant and
the controller never consume a value computed in the same Slot: there is
no algebraic loop to solve. Every zero-Latency edge goes from a lower to a
higher priority, which states its within-Slot order.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from sil.manifest import Manifest, SubscriberRoute

PROOF_DIR = Path(__file__).resolve().parent
ROOT = PROOF_DIR.parents[1]
EXAMPLE_DIR = ROOT / "examples" / "adas-reference"
EDGE = PROOF_DIR / "edge.py"
# The faulted case of the deliberate comparison, run independently without
# its Interceptors.
UNFAULTED = "sensor_loss.unfaulted"
# The plant identity the ACC multi-rate evidence qualified.
QUALIFIED_PLANT = (ROOT / "proofs" / "acc-fmi" / "multirate-evidence"
                   / "report.json")


def load(name: str, path: Path):
    """A module by its file, registered so its dataclasses resolve."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


reference = load("adas_closed_loop_reference", EXAMPLE_DIR / "manifest.py")
edge = load("adas_closed_loop_edge", EDGE)
equivalence = load("adas_closed_loop_equivalence",
                   ROOT / "proofs" / "adas-equivalence" / "experiment.py")

MS = 1_000_000
STEP_NS = reference.PERIOD_NS  # plant, controller, maneuver, actuator
# A whole number of every Period, 40 ms included.
DURATION_NS = 3_520 * MS
CLEAR, HAZARD, UNAVAILABLE = 0, 1, 2

# --- Schemas and Channels ----------------------------------------------------

TRUTH_FIELDS = edge.TRUTH_FIELDS


def _f64(*names: str) -> dict:
    return {"fields": [{"name": n, "type": "f64"} for n in names]}


# The plant's Float64 variables are bound by field name.
SCHEMAS = {
    **reference.SCHEMAS,
    "loop.Truth": _f64(*TRUTH_FIELDS),
    "loop.Actuation": _f64("accel_mps2"),
    "loop.LeadMotion": _f64("lead_accel_mps2"),
    "loop.Visibility": {"fields": [{"name": "sample_time_ns", "type": "u64"},
                                   {"name": "lead_visible", "type": "u8"}]},
}


@dataclass(frozen=True)
class Channel:
    schema: str
    publisher: str
    # Sample time minus publication time of its Messages: an FMU or the
    # controller publishes, in Slot t, the value at t + one Step.
    sample_offset_ns: int


CHANNELS = {
    "loop.lead": Channel("loop.LeadMotion", "maneuver", 0),
    "loop.visibility": Channel("loop.Visibility", "maneuver", 0),
    "loop.truth": Channel("loop.Truth", "plant", STEP_NS),
    "adas.radar": Channel("adas.ObjectList", "radar", 0),
    "adas.camera": Channel("adas.ObjectList", "camera", 0),
    "adas.ego": Channel("adas.EgoMotion", "ego", 0),
    "adas.command": Channel("adas.Command", "controller", STEP_NS),
    "loop.actuation": Channel("loop.Actuation", "actuator", 0),
}
SENSORS = ("radar", "camera", "ego")
SENSOR_CHANNELS = {s: f"adas.{s}" for s in SENSORS}
# Lower runs first in a Slot. The Native controller registers priority 0.
PRIORITY = {"maneuver": -3, "radar": -2, "camera": -2, "ego": -2,
            "controller": 0, "actuator": 1, "plant": 2}
FEEDBACK_LATENCY_NS = STEP_NS
NOMINAL_PERIODS_NS = {"radar": 20 * MS, "camera": 40 * MS, "ego": 10 * MS}
# Each controller route holds two Messages: one Period of sensor Latency
# keeps the previous observation queued while the next is published.
CONTROLLER_ROUTE_CAPACITY = 2
# A route holds a Message from its publication. The controller publishes the
# next Command before the actuator takes the one due in the same Slot.
COMMAND_ROUTE_CAPACITY = 2

# --- the plant, the initial condition and the sensors ------------------------

# The plant's fixed initial state (python/src/sil/examples/acc/dynamics.py):
# ego at 0 m, both vehicles at 25 m/s. The case sets the lead position.
EGO_SPEED_MPS = 25.0
LEAD_SPEED_MPS = 25.0
INITIAL_ACCEL_MPS2 = 0.0
# Idealized sensing: the lead is one object at the plant gap, straight ahead.
# Radar and camera IDs are independent numbers.
SENSOR_OBJECTS = {"radar": {"object_id": 1, "confidence": 0.875},
                  "camera": {"object_id": 7, "confidence": 0.875}}


def initial_truth(initial_gap_m: float) -> dict:
    return {"ego_position_m": 0.0, "ego_speed_mps": EGO_SPEED_MPS,
            "lead_position_m": initial_gap_m, "lead_speed_mps": LEAD_SPEED_MPS,
            "gap_m": initial_gap_m,
            "relative_speed_mps": LEAD_SPEED_MPS - EGO_SPEED_MPS}


# --- cases -------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    """One Run: the initial state, the Maneuver, the sensing and the faults.

    `modes` is the expected sequence of Command modes, each with the Sample
    time it starts at, where that is predicted from the Maneuver and the
    profile rules, or None where only its order is predicted."""

    name: str
    why: str
    initial_gap_m: float
    modes: tuple[tuple[int, int | None], ...]
    lead_accel: tuple[tuple[int, int, float], ...] = ()
    hidden: tuple[tuple[int, int], ...] = ()
    interceptors: dict = field(default_factory=dict)
    sensor_periods_ns: dict = field(
        default_factory=lambda: dict(NOMINAL_PERIODS_NS))
    sensor_latency_ns: int = 0


BRAKING = ((200 * MS, 1_700 * MS, -6.0),)
CASES = {c.name: c for c in (
    Case("clear_road", "the lead is never visible: CLEAR, no braking", 60.0,
         ((CLEAR, STEP_NS),), hidden=((0, DURATION_NS),)),
    Case("approach", "the lead brakes at -6 m/s2 and closes in: HAZARD and "
         "braking, then CLEAR once the lead pulls away", 30.0,
         ((CLEAR, STEP_NS), (HAZARD, None), (CLEAR, None)),
         lead_accel=(*BRAKING, (3_000 * MS, 4_000 * MS, 4.0))),
    # The radar list at 2.6 s lists no object: CLEAR from Sample time 2.61 s.
    Case("cut_out", "the lead disappears during the hazard at 2.6 s and "
         "pulls away: CLEAR, and the braking is released", 30.0,
         ((CLEAR, STEP_NS), (HAZARD, None), (CLEAR, 2_610 * MS)),
         lead_accel=(*BRAKING, (2_600 * MS, DURATION_NS, 4.0)),
         hidden=((2_600 * MS, DURATION_NS),)),
    # Radar lists published in [1.0 s, 1.3 s) are dropped. The list sampled
    # at 0.98 s is fresh at age 40 ms (t = 1.02 s) and stale from 1.03 s:
    # SENSOR_UNAVAILABLE from Sample time 1.04 s. The list sampled at 1.3 s
    # recovers: CLEAR from Sample time 1.31 s.
    Case("sensor_loss", "radar lists lost for 300 ms: SENSOR_UNAVAILABLE "
         "and degraded braking, then recovery", 60.0,
         ((CLEAR, STEP_NS), (UNAVAILABLE, 1_040 * MS), (CLEAR, 1_310 * MS)),
         interceptors={"radar": [{"kind": "drop", "start_ns": 1_000 * MS,
                                  "end_ns": 1_300 * MS}]}),
    Case("near_start", "another initial state: the lead 7 m ahead, inside "
         "8 m, so HAZARD from the first activation until the gap opens", 7.0,
         ((HAZARD, STEP_NS), (CLEAR, None))),
)}

# Variants of `approach` that separate the reference-model behavior, the
# sampling and hold of the sensors, and the sensor Channel Latency. Each
# differs from its baseline in one declaration only.
VARIANTS = {
    # Every sensor sampled at every activation: no hold.
    "approach.ideal": replace(
        CASES["approach"], name="approach.ideal",
        why="every sensor every 10 ms: the reference model on unheld truth",
        sensor_periods_ns=dict.fromkeys(SENSORS, STEP_NS)),
    # One Step of Latency on every sensor Channel: the first activation
    # holds nothing.
    "approach.latency": replace(
        CASES["approach"], name="approach.latency",
        why="10 ms Latency on every sensor Channel",
        modes=((UNAVAILABLE, STEP_NS), (CLEAR, 2 * STEP_NS), (HAZARD, None),
               (CLEAR, None)),
        sensor_latency_ns=STEP_NS),
}
ALL_CASES = {**CASES, **VARIANTS}


@dataclass(frozen=True)
class Effect:
    """What a variant isolates against the case it differs from, and the
    Manifest paths it may change."""

    compared_with: str
    isolates: str
    paths: tuple[str, ...]


# The radar and camera Periods also size their truth and visibility routes.
EFFECTS = {
    "approach": Effect("approach.ideal", "sampling and hold",
                       ("participants.radar.", "participants.camera.")),
    "approach.latency": Effect("approach", "sensor Channel Latency",
                               tuple(f"channels.adas.{s}.latency_ns"
                                     for s in SENSORS)),
}

# --- the acceptance envelope, fixed before measuring -------------------------

# Behavior on unfaulted and faulted truth, in every case and form.
KPI = {"minimum_gap_m": 2.0, "accel_min_mps2": -3.0, "accel_max_mps2": 0.0,
       "minimum_ego_speed_mps": 0.0}
# A form against the independent FMPy execution of the same archives: the
# same Float64 plant and binary32 controller arithmetic on the same inputs,
# so the budget is far below one Step of plant motion.
TRUTH_TOLERANCE = {"atol": 1e-10, "rtol": 1e-12}
EXACT_FLOAT = {"atol": 0, "rtol": 0}
# How far a variant may move the behavior from the case it is compared
# with. The hazard comes from the radar x and relative speed; the camera
# only confirms the radar object within 2 m. A radar observation is held at
# most one Step, and sensor Latency adds one Step, so each variant moves the
# hazard onset by at most two Steps, and later only (closing speed grows and
# the gap shrinks until the onset). The minimum gap moves by far less than
# 0.5 m at the closing speeds of `approach`.
EFFECT_ENVELOPE = {"hazard_onset_shift_ns": (0, 2 * STEP_NS),
                   "minimum_gap_delta_m": 0.5}

# --- the controller and the plant --------------------------------------------

Controller = Callable[[Manifest, list[SubscriberRoute]], None]
Plant = Callable[[Case], list[str]]


def native_controller(library: Path) -> Controller:
    def add(m: Manifest, routes: list[SubscriberRoute]) -> None:
        m.add_native("controller", library=str(Path(library).resolve()),
                     config=reference.controller_config("adas"),
                     subscribes=routes, publishes=["adas.command"])
    return add


def fmu_controller(archive: Path) -> Controller:
    def add(m: Manifest, routes: list[SubscriberRoute]) -> None:
        command = ["python3", "-m", "sil.fmi", str(Path(archive).resolve())]
        for bind in equivalence.fmu_bindings("adas"):
            command += ["--bind", bind]
        for start in equivalence.FMU_STARTS:
            command += ["--start", start]
        m.add_process("controller", command=command,
                      step_period_ns=STEP_NS,
                      priority=PRIORITY["controller"], subscribes=routes,
                      publishes=["adas.command"])
    return add


def fmu_plant(archive: Path) -> Plant:
    """The ACC plant FMU through the importer. Its Float64 variables carry
    the Channel fields by name; the case sets the initial lead position."""
    def command(case: Case) -> list[str]:
        return ["python3", "-m", "sil.fmi", str(Path(archive).resolve()),
                "--start", f"initial_lead_position_m={case.initial_gap_m!r}"]
    return command


def _edge(role: str, config: dict) -> list[str]:
    return ["python3", str(EDGE), role,
            json.dumps(config, sort_keys=True, separators=(",", ":"))]


def _sensor_config(case: Case, sensor: str) -> dict:
    objects = SENSOR_OBJECTS.get(sensor, {"object_id": None,
                                          "confidence": None})
    return {"sensor": sensor, "truth": "loop.truth",
            "truth_sample_offset_ns": CHANNELS["loop.truth"].sample_offset_ns,
            "visibility": None if sensor == "ego" else "loop.visibility",
            "output": SENSOR_CHANNELS[sensor],
            "initial_truth": initial_truth(case.initial_gap_m), **objects}


def _capacity(case: Case, sensor: str) -> int:
    """A sensor's truth and visibility route: the Messages of one sensor
    Period, all drained at the sensor's activation."""
    return case.sensor_periods_ns[sensor] // STEP_NS


class TableError(ValueError):
    """A Manifest whose consumption the table does not state."""


def loop_document(case: Case, controller: Controller, plant: Plant) -> dict:
    """The Manifest document of `case`, unchecked."""
    m = Manifest(duration_ns=DURATION_NS)
    m.add_schemas(SCHEMAS)
    for name, channel in CHANNELS.items():
        latency = (case.sensor_latency_ns if name in SENSOR_CHANNELS.values()
                   else FEEDBACK_LATENCY_NS if channel.sample_offset_ns
                   else 0)
        m.add_channel(name, schema=channel.schema, latency_ns=latency)
    for sensor, interceptors in case.interceptors.items():
        for interceptor in interceptors:
            m.add_interceptor(SENSOR_CHANNELS[sensor], **interceptor)
    m.add_process(
        "maneuver", step_period_ns=STEP_NS, priority=PRIORITY["maneuver"],
        command=_edge("maneuver", {
            "lead": "loop.lead", "visibility": "loop.visibility",
            "lead_accel": [list(w) for w in case.lead_accel],
            "hidden": [list(w) for w in case.hidden]}),
        publishes=["loop.lead", "loop.visibility"])
    for sensor in SENSORS:
        capacity = _capacity(case, sensor)
        routes = [SubscriberRoute("loop.truth", capacity=capacity)]
        if sensor != "ego":
            routes.append(SubscriberRoute("loop.visibility", capacity=capacity))
        m.add_process(sensor, command=_edge("sensor",
                                            _sensor_config(case, sensor)),
                      step_period_ns=case.sensor_periods_ns[sensor],
                      priority=PRIORITY[sensor], subscribes=routes,
                      publishes=[SENSOR_CHANNELS[sensor]])
    controller(m, [SubscriberRoute(SENSOR_CHANNELS[s],
                                   capacity=CONTROLLER_ROUTE_CAPACITY)
                   for s in SENSORS])
    m.add_process(
        "actuator", step_period_ns=STEP_NS, priority=PRIORITY["actuator"],
        command=_edge("actuator", {"command": "adas.command",
                                   "actuation": "loop.actuation",
                                   "initial_accel_mps2": INITIAL_ACCEL_MPS2}),
        subscribes=[SubscriberRoute("adas.command",
                                    capacity=COMMAND_ROUTE_CAPACITY)],
        publishes=["loop.actuation"])
    m.add_process(
        "plant", command=plant(case), step_period_ns=STEP_NS,
        priority=PRIORITY["plant"],
        subscribes=[SubscriberRoute("loop.lead", capacity=1),
                    SubscriberRoute("loop.actuation", capacity=1)],
        publishes=["loop.truth"])
    return m.to_doc()


def loop_manifest(case: Case, controller: Controller, plant: Plant) -> dict:
    """The checked Manifest document of `case`: TableError when its
    consumption is not the declared, future-free one."""
    doc = loop_document(case, controller, plant)
    findings = consumption_findings(doc)
    if findings:
        raise TableError(f"{case.name}: {'; '.join(findings)}")
    return doc


def write(doc: dict, path: Path) -> Path:
    """The canonical bytes `Manifest.write` gives."""
    path.write_text(json.dumps(doc, sort_keys=True, separators=(",", ":"),
                               allow_nan=False) + "\n")
    return path


# --- the consumption table ----------------------------------------------------


def _schedule(doc: dict) -> dict[str, tuple[int, int]]:
    """Each Participant's (Period, priority). A Native participant's comes
    from its config and the Task the library registers."""
    out = {}
    for name, p in doc["participants"].items():
        if p["type"] == "native":
            out[name] = (p["config"]["period_ns"], PRIORITY["controller"])
        else:
            out[name] = (p["step_period_ns"], p["priority"])
    return out


def consumption_table(doc: dict) -> list[dict]:
    """Every consumption of the Manifest: who takes which Channel, after
    which Latency, in which order within a Slot, and how old the value it
    describes is when it is taken."""
    schedule = _schedule(doc)
    rows = []
    for name, p in sorted(doc["participants"].items(),
                          key=lambda item: schedule[item[0]][1]):
        period, priority = schedule[name]
        for route in p["subscribes"]:
            channel = doc["channels"][route["channel"]]
            publisher = CHANNELS[route["channel"]].publisher
            latency = channel["latency_ns"]
            same_slot = latency == 0 and schedule[publisher][1] < priority
            # The least time from publication to the activation that takes
            # the Message.
            # A later publisher in the Slot reaches the subscriber no sooner
            # than one Step on: the conservative bound.
            delay = latency if latency else (0 if same_slot else STEP_NS)
            rows.append({
                "participant": name, "period_ns": period, "offset_ns": 0,
                "priority": priority, "channel": route["channel"],
                "publisher": publisher,
                "publisher_period_ns": schedule[publisher][0],
                "publisher_priority": schedule[publisher][1],
                "latency_ns": latency,
                "sample_offset_ns": CHANNELS[route["channel"]].sample_offset_ns,
                "least_delay_ns": delay,
                "within_slot": "publisher first" if same_slot else None,
                "capacity": route["capacity"], "overflow": route["overflow"],
            })
    return rows


def consumption_findings(doc: dict) -> list[str]:
    """What the table does not allow: a Message taken before the time it
    describes, a zero-Latency edge without a stated within-Slot order, a
    Period that is not a whole number of Steps, or an unbounded route."""
    findings = []
    schedule = _schedule(doc)
    for name, (period, _) in schedule.items():
        if period % STEP_NS or DURATION_NS % period:
            findings.append(f"{name}: Period {period} ns")
    for row in consumption_table(doc):
        where = f"{row['participant']} <- {row['channel']}"
        if row["sample_offset_ns"] > row["least_delay_ns"]:
            findings.append(
                f"{where}: takes a value sampled "
                f"{row['sample_offset_ns'] - row['least_delay_ns']} ns after "
                "its activation (a future observation)")
        if row["latency_ns"] == 0 and \
                row["publisher_priority"] == row["priority"]:
            findings.append(f"{where}: Latency 0 at equal priority states no "
                            "within-Slot order")
        if row["overflow"] != "fail":
            findings.append(f"{where}: overflow {row['overflow']}")
    return findings


def form_difference(native: dict, fmu: dict) -> list[str]:
    """The paths at which the two forms differ; only the controller entry
    may."""
    allowed = {f"participants.controller.{k}"
               for k in equivalence.TARGET_KEYS}
    found = equivalence.differences(native, fmu)
    stray = [d for d in found if d not in allowed]
    if stray:
        raise TableError(f"the forms differ in {stray}")
    return found


def effect_difference(variant: str, baseline_doc: dict,
                      variant_doc: dict) -> list[str]:
    """The paths at which a variant differs from its baseline; only the
    declaration it isolates may."""
    effect = EFFECTS[variant]
    found = equivalence.differences(baseline_doc, variant_doc)
    stray = [d for d in found if not d.startswith(effect.paths)]
    if stray or not found:
        raise TableError(f"{variant} differs from {effect.compared_with} in "
                         f"{stray or 'nothing'}")
    return found


# --- comparison contracts and the independent rows ---------------------------

COMMAND_FIELDS = [f["name"] for f in SCHEMAS["adas.Command"]["fields"]]
OBSERVATIONS = {"start_ns": STEP_NS, "stop_ns": DURATION_NS,
                "step_ns": STEP_NS}


def _command_rules() -> dict:
    return {f["name"]: EXACT_FLOAT if f["type"] == "f32" else "exact"
            for f in SCHEMAS["adas.Command"]["fields"]}


def contract(reference_offset_ns: int, truth_rule: dict) -> dict:
    """Commands and truth of a Run against a reference. The Run stores each
    in its publication Slot, one Step before its Sample time."""
    def rule(channel: str, fields: dict) -> dict:
        return {"reference_channel": channel, "actual_offset_ns": STEP_NS,
                "reference_offset_ns": reference_offset_ns,
                "observations": OBSERVATIONS, "fields": fields}
    return {"sil_comparison": 1,
            "evaluation": {"from_ns": STEP_NS, "to_ns": DURATION_NS},
            "channels": {
                "adas.command": rule("adas.command", _command_rules()),
                "loop.truth": rule("loop.truth", dict.fromkeys(
                    TRUTH_FIELDS, truth_rule))}}


# The independent rows are stored at their Sample time.
INDEPENDENT_CONTRACT = contract(0, TRUTH_TOLERANCE)
CROSS_FORM_CONTRACT = contract(STEP_NS, EXACT_FLOAT)


def independent_mapping() -> dict:
    """The `sil-csv` mapping of independent.py's rows: one row per Sample
    time, the Command and the truth it describes."""
    command = {"sample_time_ns": {"column": "time_ms", "scale": MS},
               **{n: {"column": n} for n in COMMAND_FIELDS
                  if n != "sample_time_ns"}}
    return {
        "sil_csv_mapping": 1,
        "timestamp": {"column": "time_ms", "unit": "ms"},
        "schemas": {"adas.Command": SCHEMAS["adas.Command"],
                    "loop.Truth": SCHEMAS["loop.Truth"]},
        "channels": [
            {"channel": "adas.command", "schema": "adas.Command",
             "fields": command},
            {"channel": "loop.truth", "schema": "loop.Truth",
             "fields": {n: {"column": f"truth.{n}"} for n in TRUTH_FIELDS}}],
    }


def independent_declaration(case: Case, faults: bool = True) -> dict:
    """What independent.py executes: the case in its own terms. Only drop
    Interceptors are declared; it refuses any other."""
    drops = {}
    for sensor, interceptors in (case.interceptors if faults else {}).items():
        for i in interceptors:
            if i["kind"] != "drop":
                raise TableError(f"{case.name}: independent.py applies drop "
                                 f"only, not {i['kind']}")
            drops.setdefault(sensor, []).append([i["start_ns"], i["end_ns"]])
    return {
        "step_ns": STEP_NS, "duration_ns": DURATION_NS,
        "initial_lead_position_m": case.initial_gap_m,
        "initial_truth": initial_truth(case.initial_gap_m),
        "initial_accel_mps2": INITIAL_ACCEL_MPS2,
        "lead_accel": [list(w) for w in case.lead_accel],
        "hidden": [list(w) for w in case.hidden],
        "sensor_periods_ns": case.sensor_periods_ns,
        "sensor_latency_ns": case.sensor_latency_ns,
        "command_latency_ns": FEEDBACK_LATENCY_NS,
        "drops": drops, "objects": SENSOR_OBJECTS,
        "controller_start": equivalence.FMU_STARTS,
    }


# --- deliberate failures and failing Runs ------------------------------------


@dataclass(frozen=True)
class Divergence:
    observation_ns: int
    channel: str
    field: str
    actual: int | float
    expected: int | float


# A KPI that the faulted Run must fail: no braking at all, as on a clear
# road. The first Command that brakes is the first SENSOR_UNAVAILABLE one.
NO_BRAKING_KPI = {**KPI, "accel_min_mps2": 0.0}
DELIBERATE = {
    "kpi": {"case": "sensor_loss", "kpi": NO_BRAKING_KPI,
            "why": "the radar loss brakes, which a no-braking KPI refuses",
            "prediction": {"sample_time_ns": 1_040 * MS,
                           "field": "acceleration_mps2", "value": -0.5}},
    # The faulted Run against the independent execution without the fault.
    # At 1.0 s the dropped list leaves the one sampled at 0.98 s held.
    "comparison": {"case": "sensor_loss",
                   "why": "the radar loss against the unfaulted reference",
                   "prediction": Divergence(1_010 * MS, "adas.command",
                                            "radar_age_ns", 20 * MS, 0)},
}


@dataclass(frozen=True)
class Failure:
    """A Run that must end with `exit_code` and name its cause. `patch`
    changes the checked Manifest document the way a hand-edited one would,
    past the authoring check."""

    case: str
    why: str
    exit_code: int
    diagnostics: tuple[str, ...]
    interceptors: dict | None = None
    patch: Callable[[dict], None] | None = None
    both_forms: bool = False


def _future_truth(doc: dict) -> None:
    doc["participants"]["plant"]["priority"] = -5
    doc["channels"]["loop.truth"]["latency_ns"] = 0


def _missing_initial_truth(doc: dict) -> None:
    command = doc["participants"]["radar"]["command"]
    config = json.loads(command[-1])
    del config["initial_truth"]
    command[-1] = json.dumps(config, sort_keys=True, separators=(",", ":"))


FAILURES = {
    "route_overflow": Failure(
        "approach", "radar lists published in [0.5 s, 0.6 s) delayed by "
        "50 ms: three wait in a controller route of two at 0.54 s", 1,
        ("subscriber route capacity exceeded: Channel 'adas.radar'",
         "subscriber 'controller'", "configured capacity 2"),
        interceptors={"radar": [{"kind": "delay", "delay_ns": 50 * MS,
                                 "start_ns": 500 * MS, "end_ns": 600 * MS}]}),
    "malformed_list": Failure(
        "approach", "an Interceptor writes radar.count 9 into the list "
        "published at 0.2 s", 1,
        ("t=200000000 ns: radar.count 9 exceeds the capacity 8",),
        interceptors={"radar": [{"kind": "override", "field": "count",
                                 "value": 9, "start_ns": 200 * MS,
                                 "end_ns": 220 * MS}]},
        both_forms=True),
    "future_truth": Failure(
        "approach", "the plant first in the Slot and truth at Latency 0: a "
        "sensor would describe truth sampled after its activation", 1,
        ("t=0 ns:", "holds truth sampled at 10000000 ns, after the "
         "activation time"), patch=_future_truth),
    "manifest_error": Failure(
        "approach", "a sensor config without the initial condition", 2,
        ("participant 'radar'", "config keys"),
        patch=_missing_initial_truth),
}


def failure_document(failure: Failure, controller: Controller,
                     plant: Plant) -> dict:
    case = CASES[failure.case]
    if failure.interceptors is not None:
        case = replace(case, interceptors=failure.interceptors)
    doc = loop_manifest(case, controller, plant)
    if failure.patch:
        failure.patch(doc)
    return doc
