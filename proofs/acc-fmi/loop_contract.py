"""Authored closed-loop configuration, Manifest and archive contract checks."""
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from sil.manifest import Manifest, SubscriberRoute
from proof_support import require

HERE = Path(__file__).resolve().parent
STEP_NS = 10_000_000
STEPS = 500
MIN_GAP_M = 5.0
FIELDS = {
    "sensing": ["gap_m", "relative_speed_mps", "ego_speed_mps"],
    "command": ["accel_mps2"],
    "state": ["ego_position_m", "lead_position_m", "lead_speed_mps"],
}
# The two engines call identical Float64 artifacts in the same runtime. Equal
# budgets are intentional: 1e-10 SI absolute plus 1e-12 relative allows rounding
# while remaining well below the measured one-Step differences. Explicit entries
# make each field's budget reviewable without implying different physical accuracy.
TOLERANCES = {
    "gap_m": (1e-10, 1e-12),
    "relative_speed_mps": (1e-10, 1e-12),
    "ego_speed_mps": (1e-10, 1e-12),
    "accel_mps2": (1e-10, 1e-12),
    "ego_position_m": (1e-10, 1e-12),
    "lead_position_m": (1e-10, 1e-12),
    "lead_speed_mps": (1e-10, 1e-12),
}
UNITS = {name: ("m/s2" if name == "accel_mps2" else
                "m/s" if "speed" in name else "m") for name in TOLERANCES}
UNITS.update({"lead_accel_mps2": "m/s2", "initial_lead_position_m": "m"})
PLANT_INPUTS = ["accel_mps2", "lead_accel_mps2", "initial_lead_position_m"]
INITIAL_OUTPUTS = {"sensing": [60.0, 0.0, 25.0], "state": [0.0, 60.0, 25.0],
                   "command": [1.5]}


@dataclass(frozen=True)
class Schedule:
    channels: tuple[tuple[str, str], ...]
    sensing_offset_ns: int
    first_command_step: int = 0

    def channels_at(self, step):
        names = {name for _, name in self.channels}
        if step < self.first_command_step:
            names.remove("command")
        return names

    def communication_ns(self, channel, publication_ns):
        offset = {"sensing": self.sensing_offset_ns, "state": STEP_NS, "command": 0}[channel]
        return publication_ns + offset


FMU_SCHEDULE = Schedule(tuple((name, name) for name in FIELDS), STEP_NS)
PYTHON_SCHEDULE = Schedule((("acc.Sensing", "sensing"), ("acc.Command", "command")), 0, 1)
VARIANTS = ("nominal", "shift-command", "shift-sensing", "initial-command")


def configuration():
    return dict(step_ns=STEP_NS, steps=STEPS, tolerances=TOLERANCES,
                minimum_gap_m=MIN_GAP_M, fields=FIELDS, units=UNITS,
                initialization=INITIAL_OUTPUTS, nominal_route_capacity=2,
                delayed_route_capacity=3, nominal_latency_ns=STEP_NS)


def validate_archives(directory=Path("/fmus"), *, require_sensitivity_inputs=False):
    plant_inputs = PLANT_INPUTS if require_sensitivity_inputs else FIELDS["command"]
    for model, inputs, outputs, starts in (
        ("AccController", FIELDS["sensing"], FIELDS["command"], [60, 0, 25]),
        ("AccPlant", plant_inputs, FIELDS["sensing"] + FIELDS["state"],
         [0, 0, 60] if require_sensitivity_inputs else [0]),
    ):
        with zipfile.ZipFile(directory / f"{model}.fmu") as archive:
            root = ET.fromstring(archive.read("modelDescription.xml"))
        require(root.find("CoSimulation").get("canHandleVariableCommunicationStepSize") == "false",
                f"{model}: expected qualified fixed communication interval profile")
        # These FMUs have no internal sample Clock or declared DefaultExperiment
        # rate. The driver supplies a positive constant interval; no XML rate is
        # invented. Both driver receipts and every Manifest are checked below.
        variables = {v.attrib["name"]: v for v in root.find("ModelVariables")}
        require(all(v.tag != "Clock" for v in variables.values()), f"{model}: unexpected Clock rate")
        for causality, names in (("input", inputs), ("output", outputs)):
            actual = {n for n, v in variables.items() if v.get("causality") == causality}
            if model == "AccPlant" and not require_sensitivity_inputs and causality == "input":
                require(set(names) <= actual,
                        f"{model}: missing base input names: {actual}")
            else:
                require(actual == set(names),
                        f"{model}: invalid {causality} names: {actual}")
            for name in names:
                v = variables[name]
                require(v.tag == "Float64" and v.get("unit") == UNITS[name],
                        f"{model}/{name}: invalid scalar type/unit")
        for name, start in zip(inputs, starts):
            require(float(variables[name].get("start")) == start,
                    f"{model}/{name}: invalid start")


def validate_manifest(document):
    require(type(STEP_NS) is int and STEP_NS > 0 and type(STEPS) is int and STEPS > 0,
            "invalid communication grid")
    require(document["duration_ns"] == STEP_NS * STEPS, "Manifest Duration differs from exchange grid")
    for name, participant in document["participants"].items():
        require(participant["step_period_ns"] == STEP_NS,
                f"{name}: Step period differs from exchange grid {STEP_NS}")
        for route in participant.get("subscribes", []):
            require(type(route["capacity"]) is int and route["capacity"] > 0 and route["overflow"] == "fail",
                    f"{name}/{route['channel']}: invalid finite route capacity/policy")


def manifest(variant="nominal", *, capacity=None, minimum_gap_m=MIN_GAP_M):
    require(variant in VARIANTS, f"unknown coupling variant {variant}")
    route_capacity = capacity if capacity is not None else (3 if variant.startswith("shift-") else 2)
    m = Manifest(duration_ns=STEP_NS * STEPS)
    for channel, fields in FIELDS.items():
        m.add_schemas({channel: {"fields": [{"name": n, "type": "f64"} for n in fields]}})
        latency = 2 * STEP_NS if variant == f"shift-{channel}" else STEP_NS
        m.add_channel(channel, schema=channel, latency_ns=latency)
    for model, name, incoming, outgoing, priority in (
        ("AccPlant", "plant", ["command"], ["sensing", "state"], 0),
        ("AccController", "controller", ["sensing"], ["command"], 1),
    ):
        binds = [part for ch in incoming + outgoing for field in FIELDS[ch]
                 for part in ("--bind", f"{ch}:{field}={field}")]
        starts = ["--start", "accel_mps2=0.5"] if name == "plant" and variant == "initial-command" else []
        m.add_process(name, command=["python3", "-m", "sil.fmi", f"/fmus/{model}.fmu", *binds, *starts],
                      step_period_ns=STEP_NS, publishes=outgoing, priority=priority,
                      subscribes=[SubscriberRoute(ch, capacity=route_capacity) for ch in incoming])
    m.add_process("kpi", command=["python3", str(HERE / "loop_kpi.py"), str(minimum_gap_m),
                                  str(STEP_NS), str(STEPS)], step_period_ns=STEP_NS, priority=2,
                  subscribes=[SubscriberRoute("sensing", capacity=route_capacity)])
    validate_manifest(m.to_doc())
    return m
