"""One declared experiment, two execution forms (issue #226).

The ADAS reference controller runs as a Native participant of the C library
and as `AdasReference.fmu`, the FMI 3.0 export of the same sources. Both
Manifests come from one call of `manifest.reference_manifest` in
`examples/adas-reference/`: the same Schemas, Channels, Latencies,
Interceptors, Replay participant, input Recording, subscriber routes and
Duration. Only the controller entry differs:

| Declared once | Native form | FMU form |
| --- | --- | --- |
| controller Period 10 ms | config `period_ns` | `step_period_ns`, the FMU's fixed step |
| parameters | config keys | `--start` of the two Float64 parameters |
| Channel mapping | config `radar`, `camera`, `ego`, `command` | one `--bind` per Schema field |
| artifact | the library | the archive |

`form_difference` checks that this is all that differs, and records it.

Input consumption differs in one declared place. The native adapter
receives every Message of an activation in Publish order. The FMU's inputs
hold the last value written, so of several Messages of one Channel at one
activation the FMU takes the newest (`SUPERSEDED`).

Nothing here runs the controller or reads a Run's output: the predicted
divergences of the negative controls come from the authored maneuvers and
the profile rules in docs/adas-reference.md.
"""

from __future__ import annotations

import hashlib
import importlib.util
from dataclasses import dataclass, field
from pathlib import Path

from sil.manifest import Manifest, SubscriberRoute

PROOF_DIR = Path(__file__).resolve().parent
ROOT = PROOF_DIR.parents[1]
EXAMPLE_DIR = ROOT / "examples" / "adas-reference"


def load(name: str, path: Path):
    """An example's module, by its file and under a name of its own: other
    examples also have a `manifest.py` and a `prepare.py`."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manifest = load("adas_reference_manifest", EXAMPLE_DIR / "manifest.py")

MS = manifest.MS
PERIOD_NS = manifest.PERIOD_NS
DURATION_NS = manifest.DURATION_NS
SCHEMAS = manifest.SCHEMAS
ROLES = (*manifest.INPUTS, "command")
CLEAR, HAZARD, UNAVAILABLE = 0, 1, 2

# Each input Channel's publication Period in the `cadence` maneuver.
CADENCE_PERIODS_NS = {"radar": 20 * MS, "camera": 40 * MS, "ego": 10 * MS}

# The FMU's two Float64 parameters, as the native config states them.
FMU_STARTS = [f"{name}={value!r}" for name, value in manifest.PARAMETERS.items()]

# Where the FMU form takes only the newest of several observations of one
# sensor at one activation: per case, the activation, the sensor, the Sample
# times of the superseded observations and of the one taken. In `delay`, the
# radar list sampled at 80 ms is delayed to 130 ms, and the lists sampled at
# 100 and 120 ms wait behind it in the route. Natively each of the three is
# accepted in turn, since each sequence exceeds the held one, and the list
# sampled at 120 ms is held after the activation. The FMU holds the same list
# and counts nothing, so both forms must still give the same Commands. A
# superseded observation that the native form would ignore and count would
# make the forms differ; the comparisons would show it.
SUPERSEDED = {
    "cadence.delay": [{"t_ns": 130 * MS, "sensor": "radar",
                       "superseded_ns": [80 * MS, 100 * MS],
                       "taken_ns": 120 * MS}],
}

# The rule of each Command field when one form is compared with the other.
# Both forms compile adas_reference.c with -ffp-contract=off and compute in
# binary64, and each output acceleration is rounded once to binary32 when the
# Command is written. The same inputs therefore give the same binary32 value:
# the declared rounding policy allows no error, so the tolerance is 0.
FLOAT_RULE = {"atol": 0, "rtol": 0}


def field_rules() -> dict:
    return {f["name"]: FLOAT_RULE if f["type"] == "f32" else "exact"
            for f in SCHEMAS["adas.Command"]["fields"]}


@dataclass(frozen=True)
class Case:
    """One experiment: a maneuver and the faults declared over it."""

    name: str
    maneuver: str
    interceptors: dict = field(default_factory=dict)
    input_latency_ns: int = manifest.INPUT_LATENCY_NS


def _cases() -> dict[str, Case]:
    cases = {m: Case(m, m) for m in manifest.MANEUVERS}
    for name, spec in manifest.EXPERIMENTS.items():
        case = Case(f"{manifest.EXPERIMENT_MANEUVER}.{name}",
                    manifest.EXPERIMENT_MANEUVER, **spec)
        cases[case.name] = case
    return cases


CASES = _cases()
# The cases whose first activation holds no observation of some sensor.
INITIALLY_UNAVAILABLE = ("freshness", "cadence.late")


# --- the two forms ---------------------------------------------------------


# Where the archive path stands in the importer command:
# python3 -m sil.fmi ARCHIVE ...
ARCHIVE_ARGUMENT = 3


def fmu_bindings(maneuver: str) -> list[str]:
    """Every Schema field of the maneuver's four Channels, bound to the FMU
    variable `<role>.<field>` (proofs/adas-fmu/recorded-input.mapping.json)."""
    return [f"{maneuver}.{role}:{f['name']}={role}.{f['name']}"
            for role in ROLES
            for f in SCHEMAS[_schema(role)]["fields"]]


def _schema(role: str) -> str:
    return manifest.INPUTS.get(role, "adas.Command")


def fmu_controller(fmu: Path, binds: dict[str, list[str]] | None = None,
                   starts: list[str] = FMU_STARTS,
                   step_period_ns: int = PERIOD_NS) -> manifest.Controller:
    """The controller as the Process participant of SiL's FMI importer.
    `binds` replaces the bindings of a named maneuver."""
    binds = binds or {}

    def add(m: Manifest, maneuver: str,
            routes: list[SubscriberRoute]) -> None:
        command = ["python3", "-m", "sil.fmi", str(Path(fmu).resolve())]
        for bind in binds.get(maneuver, fmu_bindings(maneuver)):
            command += ["--bind", bind]
        for start in starts:
            command += ["--start", start]
        m.add_process(maneuver, command=command,
                      step_period_ns=step_period_ns, subscribes=routes,
                      publishes=[f"{maneuver}.command"])
    return add


def run_manifest(case: Case, inputs: Path, controller=None,
                 input_latency_ns: int | None = None) -> Manifest:
    """The Manifest of `case`; `controller` selects the execution form."""
    return manifest.reference_manifest(
        inputs, None, maneuvers=(case.maneuver,),
        interceptors=case.interceptors,
        input_latency_ns=(case.input_latency_ns if input_latency_ns is None
                          else input_latency_ns),
        controller=controller)


def native_manifest(case: Case, inputs: Path, library: Path) -> Manifest:
    return run_manifest(case, inputs, manifest.native_controller(library))


def fmu_manifest(case: Case, inputs: Path, fmu: Path, **controller) -> Manifest:
    return run_manifest(case, inputs, fmu_controller(fmu, **controller))


# --- what differs between the forms ----------------------------------------


class FormError(AssertionError):
    """The two Manifests differ in more than the execution form."""


# The keys of the controller entry that may differ between the forms.
TARGET_KEYS = {"type", "library", "config", "command", "step_period_ns",
               "priority"}


def differences(a, b, path: str = "") -> list[str]:
    """The dotted paths at which two JSON documents differ."""
    if isinstance(a, dict) and isinstance(b, dict):
        return [d for key in sorted(a.keys() | b.keys())
                for d in differences(a.get(key), b.get(key),
                                     f"{path}.{key}" if path else key)]
    return [] if a == b else [path]


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _option(command: list[str], flag: str) -> list[str]:
    return [command[i + 1] for i, a in enumerate(command[:-1]) if a == flag]


def form_difference(case: Case, native: dict, fmu: dict) -> dict:
    """What the two Manifest documents of `case` declare, and the proof that
    they differ only in the controller's target and adaptation.

    Raises FormError when anything else differs, or when the adaptation does
    not state the same Period, parameters and Channels in both forms."""
    controller = f"participants.{case.maneuver}"
    stray = [d for d in differences(native, fmu)
             if not (d.startswith(controller + ".")
                     and d.removeprefix(controller + ".") in TARGET_KEYS)]
    if stray:
        raise FormError(f"{case.name}: the forms differ in {stray}")
    native_entry = native["participants"][case.maneuver]
    fmu_entry = fmu["participants"][case.maneuver]
    config, command = native_entry["config"], fmu_entry["command"]
    archive = command[ARCHIVE_ARGUMENT]
    starts = dict(s.split("=", 1) for s in _option(command, "--start"))
    binds = _option(command, "--bind")
    adaptation = {
        "period_ns": {"native": config["period_ns"],
                      "fmu": fmu_entry["step_period_ns"]},
        "parameters": {
            name: {"native": config[name], "fmu": starts.get(name)}
            for name in manifest.PARAMETERS},
        # The Channels whose fields are bound to the FMU's `<role>.` variables.
        "channels": {
            role: {"native": config[role],
                   "fmu": sorted({b.split(":")[0] for b in binds
                                  if b.split("=")[1].startswith(f"{role}.")})}
            for role in ROLES},
    }
    _require_same_adaptation(case, adaptation)
    return {
        "case": case.name,
        "maneuver": case.maneuver,
        "differences": differences(native, fmu),
        "shared": {
            "duration_ns": native["duration_ns"],
            "channels": native["channels"],
            "replay": native["participants"][f"{case.maneuver}_replay"],
            "subscribes": native_entry["subscribes"],
            "publishes": native_entry["publishes"],
        },
        "native": {"library": Path(native_entry["library"]).name,
                   "library_sha256": sha256(native_entry["library"]),
                   "config": config},
        "fmu": {"archive": Path(archive).name,
                "archive_sha256": sha256(archive),
                "step_period_ns": fmu_entry["step_period_ns"],
                "priority": fmu_entry["priority"],
                "bind": binds, "start": _option(command, "--start")},
        "adaptation": adaptation,
    }


def _require_same_adaptation(case: Case, adaptation: dict) -> None:
    period = adaptation["period_ns"]
    if period["native"] != period["fmu"]:
        raise FormError(f"{case.name}: Period {period}")
    for name, values in adaptation["parameters"].items():
        if values["fmu"] is None or float(values["fmu"]) != values["native"]:
            raise FormError(f"{case.name}: parameter {name} {values}")
    for role, values in adaptation["channels"].items():
        if values["fmu"] != [values["native"]]:
            raise FormError(f"{case.name}: Channel {role} {values}")


# --- comparison contracts --------------------------------------------------


def cross_form_contract(maneuver: str) -> dict:
    """Native Commands against FMU Commands. Both Recordings store the
    publication Slot t of the Command for Sample time t + 10 ms, so both
    offsets are one Period, and the observations are the Sample times."""
    channel = f"{maneuver}.command"
    return {
        "sil_comparison": 1,
        "evaluation": {"from_ns": PERIOD_NS, "to_ns": DURATION_NS},
        "channels": {channel: {
            "reference_channel": channel,
            "actual_offset_ns": PERIOD_NS,
            "reference_offset_ns": PERIOD_NS,
            "observations": {"start_ns": PERIOD_NS, "stop_ns": DURATION_NS,
                             "step_ns": PERIOD_NS},
            "fields": field_rules(),
        }},
    }


# --- negative controls -----------------------------------------------------


@dataclass(frozen=True)
class Divergence:
    """The first divergence a control must produce against the oracle."""

    observation_ns: int
    field: str
    actual: int | float
    expected: int | float


@dataclass(frozen=True)
class Control:
    """One change to the nominal FMU form, and where it must diverge.

    Exactly one of the change fields, `binds` to `input_latency_ns`, is set.
    `input_latency_ns` is the experiment's one declared input Latency, which
    `reference_manifest` gives every input Channel."""

    case: str
    why: str
    prediction: Divergence
    binds: dict | None = None
    starts: list[str] | None = None
    archive_define: str | None = None
    actual_offset_ns: int | None = None
    input_latency_ns: int | None = None

    CHANGES = ("binds", "starts", "archive_define", "actual_offset_ns",
               "input_latency_ns")

    def __post_init__(self):
        changed = [k for k in self.CHANGES if getattr(self, k) is not None]
        if len(changed) != 1:
            raise ValueError(f"a control changes one thing, not {changed}")


def controller_changes(spec) -> dict:
    """The `fmu_controller` arguments a Control or a Failure replaces."""
    return {key: getattr(spec, key) for key in ("binds", "starts")
            if getattr(spec, key) is not None}


def _rebound(maneuver: str, binding: str, variable: str) -> dict:
    """The bindings of `maneuver`, with one Channel field bound to another
    variable."""
    return {maneuver: [f"{bind.split('=')[0]}={variable}"
                       if bind.split("=")[0] == f"{maneuver}.{binding}"
                       else bind for bind in fmu_bindings(maneuver)]}


CONTROLS = {
    "binding": Control(
        "hazard",
        "the radar x_m field bound to the variable radar.y_m: the FMU's "
        "radar.x_m keeps its start value 0, which is not eligible (x > 0); "
        "no confirmed object, so CLEAR where the oracle has HAZARD",
        Divergence(10 * MS, "mode", CLEAR, HAZARD),
        binds=_rebound("hazard", "radar:x_m", "radar.y_m")),
    "sign": Control(
        "hazard",
        "an archive built with ADAS_REFERENCE_WRONG_SIGN: the object closing "
        "at 25 m/s has closing speed 0 and x = 30 m >= 8 m, so CLEAR",
        Divergence(10 * MS, "mode", CLEAR, HAZARD),
        archive_define="ADAS_REFERENCE_WRONG_SIGN"),
    "parameter": Control(
        "hazard",
        "max_change_mps2 1 instead of 0.5: the first HAZARD activation "
        "commands -1 m/s2, where the oracle has -0.5",
        Divergence(10 * MS, "acceleration_mps2", -1.0, -0.5),
        starts=["hazard_acceleration_mps2=-3.0", "max_change_mps2=1.0"]),
    "sample_time_offset": Control(
        "hazard",
        "the contract reads the publication Slot as the Sample time "
        "(actual_offset_ns 0): the observation at 10 ms is the Command "
        "published at 10 ms, whose Sample time is 20 ms",
        Divergence(10 * MS, "sample_time_ns", 20 * MS, 10 * MS),
        actual_offset_ns=0),
    "input_latency": Control(
        "cadence",
        "every input Channel with one Period of Latency: the first "
        "activation receives nothing, so SENSOR_UNAVAILABLE where the "
        "oracle has CLEAR",
        Divergence(10 * MS, "mode", UNAVAILABLE, CLEAR),
        input_latency_ns=PERIOD_NS),
}


# --- failures --------------------------------------------------------------


@dataclass(frozen=True)
class Failure:
    """A Run of the FMU form (and of the native form, where it applies)
    that must fail with `exit_code` and name its cause."""

    case: str
    why: str
    exit_code: int
    diagnostics: tuple[str, ...]
    binds: dict | None = None
    starts: list[str] | None = None
    step_period_ns: int = PERIOD_NS
    interceptors: dict | None = None
    native_diagnostics: tuple[str, ...] = ()


def _array_swap(maneuver: str) -> dict:
    """radar.x_m, an [8] Float32 array, bound to the ego speed field, and
    the scalar ego.speed_mps to the radar x_m field."""
    binds = []
    for bind in fmu_bindings(maneuver):
        target, variable = bind.split("=")
        variable = {"radar.x_m": "ego.speed_mps",
                    "ego.speed_mps": "radar.x_m"}.get(variable, variable)
        binds.append(f"{target}={variable}")
    return {maneuver: binds}


FAILURES = {
    "incompatible_array": Failure(
        "hazard", "an [8] array variable bound to a scalar field, and back: "
        "refused before the FMU is loaded", 2,
        ("Float32 variable 'radar.x_m' of dimensions [8] of 8 values",
         "'f32' scalar"),
        binds=_array_swap("hazard")),
    "unsupported_step": Failure(
        "hazard", "a 20 ms step: the FMU declares one fixed 10 ms step and "
        "no variable communication step size", 1,
        ("not the fixed step 10000000 ns", "fmi3DoStep returned Error"),
        step_period_ns=20 * MS),
    "parameter_out_of_range": Failure(
        "hazard", "max_change_mps2 20 is outside (0, 10]: initialization "
        "fails", 1,
        ("max_change_mps2 20 is outside (0, 10]",
         "fmi3ExitInitializationMode returned Error"),
        starts=["hazard_acceleration_mps2=-3.0", "max_change_mps2=20"]),
    "malformed_input": Failure(
        "hazard", "an Interceptor writes radar.count 9 into the lists "
        "published in [20, 40) ms: both forms fail at 20 ms", 1,
        ("t=20000000 ns: radar.count 9 exceeds the capacity 8",
         "fmi3DoStep returned Error"),
        interceptors={"radar": [{"kind": "override", "field": "count",
                                 "value": 9, "start_ns": 20 * MS,
                                 "end_ns": 40 * MS}]},
        native_diagnostics=(
            "t=20000000 ns: radar.count 9 exceeds the capacity 8",)),
}
