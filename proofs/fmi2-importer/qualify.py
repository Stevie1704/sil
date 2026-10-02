"""Qualify the FMI 2.0 co-simulation profile of the Importer (issue #191).

Runs inside the pinned tool image with no network on Linux x86-64. Usage:
qualify.py <work-directory>. It writes `evidence/` there:

1. Inspection: `sil-fmi-inspect` selects the Reference FMUs `Dahlquist`,
   `VanDerPol`, `BouncingBall` and `Stair` and `OSMPDummySensor`, and refuses
   `Feedthrough` with the variables outside the profile named.
2. Reference FMUs: each case runs in SiL and under FMPy 0.3.26, an
   independent FMI 2.0 importer, with the same start values and the same
   communication points. Every sample of the observation grid is compared,
   the final one included. Each SiL Run is run twice and its Recordings must
   be byte-identical.
3. Failing controls: a wrong-input variant must diverge from its reference,
   and an FMU whose `fmi2DoStep` answers Error must fail the Run and name the
   call.
4. Cleanup: after a successful and a failed Run, neither the kernel's Run
   directory nor an extracted FMU is left behind.
5. Two instances of one Reference FMU in one Run agree with each other and
   with FMPy.
6. `OSMPDummySensor`, built as `proofs/osmp-sensor` builds it, runs its
   lifecycle with its start values (no SensorView: the input pointer is 0),
   and `valid` and `count` are compared with FMPy.

Every check raises, so a failed qualification cannot write a passing report.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "proofs" / "osmp-sensor"))

import prepare as osmp_prepare  # noqa: E402
from fmi2_fixture import fake_fmu  # noqa: E402
from fmpy import extract, read_model_description  # noqa: E402
from fmpy.fmi2 import FMU2Slave  # noqa: E402
from sil.fmi import FmuParticipant  # noqa: E402
from sil.fmi.inspection import inspect  # noqa: E402
from sil.manifest import Manifest  # noqa: E402
from sil.participant import ParticipantFailure  # noqa: E402
from sil.recording import read_records  # noqa: E402
from sil import schema  # noqa: E402
from sil.testing import RunResult  # noqa: E402

REFERENCE = ROOT / "tests" / "fixtures" / "reference-fmus" / "2.0"
BUILD = Path("/build")
SIL_RUN = BUILD / "sil-run"
FAKE_STEP_ERROR = BUILD / "Fmi2StepError.so"
NS_PER_S = 1_000_000_000

# The comparison tolerance of a Real sample: |sil - fmpy| <= ABS + REL*|fmpy|.
# Both importers hand the FMU the same doubles, so agreement is expected to be
# exact; the tolerance is stated so a report can say how close is close.
ABS_TOLERANCE = 1e-12
REL_TOLERANCE = 1e-12


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


@dataclass(frozen=True)
class Case:
    """One FMU, its outputs and start values, on one communication step."""

    name: str
    fmu: Path
    # Output variable -> Channel field type ('f64' or 'i32' or 'u8').
    outputs: dict[str, str]
    step_ns: int
    steps: int
    starts: dict[str, str] = field(default_factory=dict)

    @property
    def grid_ns(self) -> list[int]:
        """The observation grid: the end of every communication step."""
        return [(k + 1) * self.step_ns for k in range(self.steps)]


CASES = [
    Case("dahlquist", REFERENCE / "Dahlquist.fmu", {"x": "f64"},
         100_000_000, 100),
    # An authored parameter, applied before initialization.
    Case("dahlquist-k-0.5", REFERENCE / "Dahlquist.fmu", {"x": "f64"},
         100_000_000, 100, {"k": "0.5"}),
    Case("vanderpol", REFERENCE / "VanDerPol.fmu", {"x0": "f64", "x1": "f64"},
         10_000_000, 2000),
    # Events inside the step: the ball bounces between communication points.
    Case("bouncingball", REFERENCE / "BouncingBall.fmu",
         {"h": "f64", "v": "f64"}, 10_000_000, 300),
    # A tunable parameter, applied before initialization.
    Case("bouncingball-e-0.8", REFERENCE / "BouncingBall.fmu",
         {"h": "f64", "v": "f64"}, 10_000_000, 300, {"e": "0.8"}),
    # Stair answers fmi2DoStep with Discard once its counter reaches 10, at
    # 9 s, so the compared case ends before that; `real_discard` runs on.
    Case("stair", REFERENCE / "Stair.fmu", {"counter": "i32"},
         200_000_000, 40),
]

# Stair past the instant it discards at.
STAIR_TO_DISCARD = Case("stair-10s", REFERENCE / "Stair.fmu",
                        {"counter": "i32"}, 200_000_000, 50)

# A wrong input: each variant is compared with the nominal case's reference
# and must diverge from it.
WRONG_INPUTS = [
    ("dahlquist-k-0.5", "dahlquist"),
    ("bouncingball-e-0.8", "bouncingball"),
]


# SiL -------------------------------------------------------------------------

def manifest(case: Case, instances: int = 1, fmu: Path | None = None) -> Manifest:
    """Each instance of the case's FMU as its own process participant."""
    m = Manifest(duration_ns=case.step_ns * case.steps)
    m.add_schemas({"fmu.Out": {"fields": [
        {"name": name, "type": kind} for name, kind in case.outputs.items()
    ]}})
    for index in range(instances):
        channel = f"fmu.Out{index}"
        m.add_channel(channel, schema="fmu.Out")
        command = [sys.executable, "-m", "sil.fmi", str(fmu or case.fmu)]
        for name in case.outputs:
            command += ["--bind", f"{channel}:{name}={name}"]
        for name, value in case.starts.items():
            command += ["--start", f"{name}={value}"]
        m.add_process(f"fmu{index}", command=command,
                      step_period_ns=case.step_ns, publishes=[channel])
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

    The kernel's Run working directory, and every extraction in it, is made
    in the runner's working directory, so whatever is left there afterwards
    is what the Run failed to remove.
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
    return SilRun(process.returncode, process.stderr, mcap, leftovers, result)


def sil_trajectory(case: Case, run: SilRun, index: int = 0) -> list[dict]:
    """The SiL samples, each dated by its Sample time.

    A Message published in the Slot at `t` carries what the FMU reached at
    the end of the Step `[t, t + step]`.
    """
    return [
        {"t_ns": t + case.step_ns, **fields}
        for t, fields in run.result.messages(f"fmu.Out{index}")
    ]


# FMPy ------------------------------------------------------------------------

def fmpy_trajectory(case: Case, work: Path,
                    rows: list | None = None) -> list[dict]:
    """The same lifecycle under FMPy, on the same communication points.

    `rows`, when given, collects each sample as it is read, so a caller sees
    how far FMPy got when a call fails.
    """
    description = read_model_description(str(case.fmu))
    unzip = extract(str(case.fmu), unzipdir=str(work / f"fmpy-{case.name}"))
    variables = {v.name: v for v in description.modelVariables}
    fmu = FMU2Slave(
        guid=description.guid, unzipDirectory=unzip,
        modelIdentifier=description.coSimulation.modelIdentifier,
        instanceName="reference",
    )
    fmu.instantiate()
    for name, text in case.starts.items():
        variable = variables[name]
        setter = {"Real": (fmu.setReal, float), "Integer": (fmu.setInteger, int),
                  "Boolean": (fmu.setBoolean, lambda v: v == "true")}
        set_values, parse = setter[variable.type]
        set_values([variable.valueReference], [parse(text)])
    fmu.setupExperiment(startTime=0.0)
    fmu.enterInitializationMode()
    fmu.exitInitializationMode()
    rows = [] if rows is None else rows
    for k in range(case.steps):
        fmu.doStep(currentCommunicationPoint=k * case.step_ns / NS_PER_S,
                   communicationStepSize=case.step_ns / NS_PER_S)
        row = {"t_ns": (k + 1) * case.step_ns}
        for name in case.outputs:
            variable = variables[name]
            getter = {"Real": fmu.getReal, "Integer": fmu.getInteger,
                      "Boolean": fmu.getBoolean}[variable.type]
            (row[name],) = getter([variable.valueReference])
        rows.append(row)
    fmu.terminate()
    fmu.freeInstance()
    shutil.rmtree(unzip)
    return rows


# Comparison ------------------------------------------------------------------

def agrees(sil, reference) -> bool:
    if isinstance(reference, float):
        return abs(sil - reference) <= ABS_TOLERANCE + REL_TOLERANCE * abs(reference)
    return int(sil) == int(reference)


def compare(case: Case, sil: list[dict], reference: list[dict]) -> dict:
    """Every sample on the case's observation grid, and the first divergence."""
    require([row["t_ns"] for row in reference] == case.grid_ns,
            f"{case.name}: the FMPy trajectory is not on the observation grid")
    if [row["t_ns"] for row in sil] != case.grid_ns:
        return {"agrees": False, "samples": len(sil),
                "first_divergence": "the SiL samples are not on the grid"}
    worst = 0.0
    for sil_row, reference_row in zip(sil, reference):
        for name in case.outputs:
            a, b = sil_row[name], reference_row[name]
            if isinstance(b, float):
                worst = max(worst, abs(a - b))
            if not agrees(a, b):
                return {"agrees": False, "samples": len(sil),
                        "first_divergence": {
                            "t_ns": sil_row["t_ns"], "variable": name,
                            "sil": a, "fmpy": b}}
    return {"agrees": True, "samples": len(sil), "max_abs_difference": worst,
            "final_sample": sil[-1]}


# Checks ----------------------------------------------------------------------

def inspection(osmp_sensor: Path) -> dict:
    selected = {path.stem: path for path in sorted(REFERENCE.glob("*.fmu"))}
    selected["OSMPDummySensor"] = osmp_sensor
    reports = {name: inspect(path) for name, path in selected.items()}
    for name, report in reports.items():
        require(report["facts"]["fmi_version"] == "2.0",
                f"{name}: inspection reports fmiVersion "
                f"{report['facts']['fmi_version']!r}")
        expected = "unusable" if name == "Feedthrough" else "compatible"
        require(report["verdict"] == expected,
                f"{name}: verdict {report['verdict']!r}, expected {expected!r}: "
                f"{report['unusable']}")
    (reason,) = reports["Feedthrough"]["unusable"]
    for name in ("String_input", "String_output", "Enumeration_input",
                 "Enumeration_output"):
        require(repr(name) in reason,
                f"Feedthrough's refusal does not name {name}: {reason}")
    return {name: {"verdict": r["verdict"], "unusable": r["unusable"],
                   "platform": r["platform"], "report": r}
            for name, r in reports.items()}


def reference_cases(work: Path) -> dict:
    results = {}
    references = {}
    for case in CASES:
        reference = fmpy_trajectory(case, work)
        references[case.name] = reference
        first = run_sil(manifest(case), work / case.name / "1")
        second = run_sil(manifest(case), work / case.name / "2")
        for run in (first, second):
            require(run.returncode == 0, f"{case.name}: SiL Run failed: {run.stderr}")
            require(not run.leftovers,
                    f"{case.name}: the Run left {run.leftovers}")
        identical = first.mcap.read_bytes() == second.mcap.read_bytes()
        require(identical, f"{case.name}: two Runs recorded different bytes")
        comparison = compare(case, sil_trajectory(case, first), reference)
        require(comparison["agrees"],
                f"{case.name}: SiL diverges from FMPy: "
                f"{comparison.get('first_divergence')}")
        results[case.name] = {
            "fmu": case.fmu.name, "fmu_sha256": sha256(case.fmu),
            "step_ns": case.step_ns, "steps": case.steps,
            "observation_grid": {"first_ns": case.grid_ns[0],
                                 "last_ns": case.grid_ns[-1],
                                 "spacing_ns": case.step_ns},
            "starts": case.starts, "outputs": case.outputs,
            "comparison": comparison,
            "recording_sha256": sha256(first.mcap),
            "recordings_identical": identical,
        }
    by_name = {case.name: case for case in CASES}
    controls = {}
    for variant, nominal in WRONG_INPUTS:
        comparison = compare(
            by_name[nominal],
            sil_trajectory(by_name[variant],
                           run_sil(manifest(by_name[variant]),
                                   work / f"control-{variant}")),
            references[nominal],
        )
        require(not comparison["agrees"],
                f"control {variant} agrees with {nominal}'s reference")
        controls[variant] = {"compared_with": nominal, **comparison}
    return {"cases": results, "wrong_input_controls": controls}


def status_failure(work: Path) -> dict:
    """A failing fmi2DoStep fails the Run, names the call, and leaves nothing."""
    archive = fake_fmu(work / "Fmi2StepError.fmu", FAKE_STEP_ERROR, "linux64")
    case = Case("fake", archive, {"y": "f64"}, 10_000_000, 5)
    run = run_sil(manifest(case), work / "status-failure")
    require(run.returncode == 1, f"the failing Run exited {run.returncode}")
    named = "fmi2DoStep returned Error" in run.stderr
    require(named, f"the failing Run does not name the call: {run.stderr}")
    require(not run.leftovers, f"the failed Run left {run.leftovers}")
    diagnostic = [line for line in run.stderr.splitlines()
                  if line.startswith("sil-run:")]
    return {"exit_code": run.returncode, "diagnostic": diagnostic,
            "names_the_call": named, "leftovers": run.leftovers}


def real_discard(work: Path) -> dict:
    """An independent FMU's own failing status, in both importers.

    SiL must fail the Run and name the call; FMPy must fail at the same
    communication step.
    """
    case = STAIR_TO_DISCARD
    run = run_sil(manifest(case), work / "stair-discard")
    require(run.returncode == 1, f"Stair to 10 s exited {run.returncode}")
    named = "fmi2DoStep returned Discard" in run.stderr
    require(named, f"Stair's failure does not name the call: {run.stderr}")
    require(not run.leftovers, f"the failed Run left {run.leftovers}")
    fmpy_rows: list = []
    try:
        fmpy_trajectory(case, work, fmpy_rows)
    except Exception as error:  # FMPy raises its own exception per status
        fmpy_error = f"{type(error).__name__}: {error}"
    else:
        raise RuntimeError("FMPy steps Stair to 10 s without a failing status")
    require("discard" in fmpy_error, f"FMPy fails Stair otherwise: {fmpy_error}")
    # Each importer completes the same Steps before the failing one.
    recorded = 0
    if run.mcap.exists():
        recorded = sum(1 for _ in read_records(run.mcap))
    require(recorded == len(fmpy_rows),
            f"SiL recorded {recorded} Steps of Stair, FMPy completed "
            f"{len(fmpy_rows)}")
    return {"fmu": case.fmu.name, "step_ns": case.step_ns,
            "sil_exit_code": run.returncode,
            "sil_diagnostic": [line for line in run.stderr.splitlines()
                               if line.startswith("sil-run:")],
            "completed_steps": recorded, "fmpy_error": fmpy_error}


def importer_cleanup(work: Path) -> dict:
    """The Importer removes its own extraction, after success and failure.

    The kernel removes its Run directory in any case, so this drives the
    participant directly and looks at what the Importer itself left.
    """
    init = {"op": "init", "name": "cleanup", "schemas": {
        "fmu.Out": {"fields": [{"name": "y", "type": "f64"}]}},
        "channels": {"fmu.Out": {"schema": "fmu.Out", "direction": "out"}}}
    outcomes = {}
    for name, archive, binds in (
        ("success", REFERENCE / "Dahlquist.fmu", ["fmu.Out:y=x"]),
        ("failure", work / "Fmi2StepError.fmu", ["fmu.Out:y=y"]),
    ):
        directory = Path(tempfile.mkdtemp(dir=work, prefix=f"cleanup-{name}-"))
        participant = FmuParticipant(archive, binds=binds)
        previous = Path.cwd()
        try:
            os.chdir(directory)
            participant.on_init(init)
            extracted = [p.name for p in directory.iterdir()]
            failed = None
            try:
                participant.on_step(0, 10_000_000, [])
            except ParticipantFailure as error:
                failed = str(error)
        finally:
            participant.close()
            os.chdir(previous)
        left = [p.name for p in directory.iterdir()]
        require(extracted, f"{name}: no extraction was made")
        require(not left, f"{name}: the Importer left {left}")
        require((failed is not None) == (name == "failure"),
                f"{name}: step failure {failed!r}")
        outcomes[name] = {"extracted_during_run": len(extracted),
                          "left_after_close": left, "step_failure": failed}
    return outcomes


def two_instances(work: Path) -> dict:
    case = next(c for c in CASES if c.name == "vanderpol")
    run = run_sil(manifest(case, instances=2), work / "two-instances")
    require(run.returncode == 0, f"two instances: {run.stderr}")
    reference = fmpy_trajectory(case, work)
    first = sil_trajectory(case, run, 0)
    second = sil_trajectory(case, run, 1)
    require(first == second, "two instances in one Run disagree")
    comparison = compare(case, first, reference)
    require(comparison["agrees"], f"two instances: {comparison}")
    return {"fmu": case.fmu.name, "instances": 2, "identical": True,
            "comparison": comparison}


def osmp_sensor(work: Path) -> tuple[Path, dict]:
    """Build OSMPDummySensor as proofs/osmp-sensor builds it."""
    epoch = osmp_prepare.command(
        "git", "-C", osmp_prepare.OSMP_CHECKOUT, "log", "-1", "--format=%ct"
    ).strip()
    archives, _ = osmp_prepare.build(work / "osmp-build", epoch)
    archive = archives["OSMPDummySensor"]
    pinned = json.loads(
        (ROOT / "proofs" / "osmp-sensor" / "evidence" / "fmu-inspection.json")
        .read_text())["OSMPDummySensor"]["binary"]["sha256"]
    with zipfile.ZipFile(archive) as opened:
        built = hashlib.sha256(
            opened.read("binaries/linux64/OSMPDummySensor.so")).hexdigest()
    return archive, {"archive_sha256": sha256(archive), "binary_sha256": built,
                     # Reported, not required: the #230 build is the pin.
                     "binary_matches_230_pin": built == pinned}


def osmp_lifecycle(archive: Path, work: Path) -> dict:
    """Start values only: no SensorView, so the input pointer is 0."""
    case = Case("osmp", archive, {"valid": "u8", "count": "i32"},
                20_000_000, 50)
    run = run_sil(manifest(case), work / "osmp")
    reference_error = None
    try:
        reference = fmpy_trajectory(case, work)
    except Exception as error:  # FMPy raises its own exception per status
        reference, reference_error = None, f"{type(error).__name__}: {error}"
    outcome = {"step_ns": case.step_ns, "steps": case.steps,
               "sil_exit_code": run.returncode,
               "sil_diagnostic": [line for line in run.stderr.splitlines()
                                  if line.startswith("sil-run:")],
               "fmpy_error": reference_error, "leftovers": run.leftovers}
    require(not run.leftovers, f"OSMP: the Run left {run.leftovers}")
    if run.returncode != 0 or reference is None:
        # The FMU does not accept the lifecycle without a SensorView. Both
        # importers must say so, and SiL must name the call.
        require(run.returncode != 0 and reference is None,
                f"OSMP: SiL exit {run.returncode}, FMPy {reference_error!r}")
        require("fmi2" in run.stderr, "OSMP: the failure names no fmi2 call")
        outcome["accepted"] = False
        return outcome
    # FMPy hands back a Boolean as a Python bool; the Channel carries a u8.
    reference = [{**row, "valid": int(row["valid"])} for row in reference]
    comparison = compare(case, sil_trajectory(case, run), reference)
    require(comparison["agrees"], f"OSMP: SiL diverges from FMPy: {comparison}")
    outcome.update(accepted=True, comparison=comparison,
                   valid=sorted({row["valid"] for row in reference}),
                   count=sorted({row["count"] for row in reference}))
    return outcome


def environment() -> dict:
    return {
        "machine": platform.machine(),
        "libc": " ".join(platform.libc_ver()),
        "python": platform.python_version(),
        "fmpy": importlib.metadata.version("fmpy"),
        "source_revision": (ROOT / "source-revision.txt").read_text().strip(),
        "tolerance": {"absolute": ABS_TOLERANCE, "relative": REL_TOLERANCE},
    }


def main(work: Path) -> None:
    evidence = work / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    scratch = work / "scratch"
    scratch.mkdir(exist_ok=True)
    require((platform.system(), platform.machine()) == ("Linux", "x86_64"),
            "the FMI 2.0 profile is qualified on Linux x86-64")
    sensor, build = osmp_sensor(scratch)
    inspected = inspection(sensor)
    write_json(evidence / "fmu-inspection.json",
               {name: entry.pop("report") for name, entry in inspected.items()})
    report = {
        "issue": 191,
        "environment": environment(),
        "inspection": inspected,
        "reference_fmus": reference_cases(scratch),
        "status_failure": status_failure(scratch),
        "reference_fmu_discard": real_discard(scratch),
        "importer_cleanup": importer_cleanup(scratch),
        "two_instances": two_instances(scratch),
        "osmp_sensor": {"build": build,
                        "lifecycle": osmp_lifecycle(sensor, scratch)},
        "known_limits": [
            "Two OSI FMUs in one process abort in the Protobuf pool "
            "(proofs/osmp-sensor/README.md#one-process); two instances of "
            "OSMPDummySensor are therefore not run in one process. This is a "
            "limit of the FMU build, not of the Importer.",
        ],
    }
    write_json(evidence / "report.json", report)
    shutil.rmtree(scratch)
    print(json.dumps({"passed": True}, indent=2))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
