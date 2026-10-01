"""Run one experiment through the native and the FMU controller (issue #226).

With installed SiL (`sil-run`, `silschema`, `sil-csv`, `sil-compare`, the
`sil` wheel and `include/sil`), a C compiler and a Python with FMPy:

1. **Artifacts.** Build `AdasReference.fmu` with the pinned packaging and
   require the archive digest of proofs/adas-fmu/evidence (where the
   compiler matches); build the Native library against the installed
   headers; prepare the maneuver Recordings and the oracle.
2. **Forms.** Author the native and the FMU Manifest of every case from the
   same declaration (experiment.py) and require that only the controller's
   target and adaptation differ.
3. **Runs.** Run each Manifest twice and require identical Recording bytes.
   Check every Command from the first through the final Sample time: one
   per Slot 0 ms to 190 ms, Sample time = Slot + 10 ms, sequence from 1 (no
   output at initialization), and the Periods of the `cadence` inputs.
4. **Independent execution.** Run every case with FMPy (independent.py).
5. **Comparisons.** Compare each form with the oracle and with the FMPy
   execution, FMPy with the oracle, and the native form with the FMU form
   under `cross_form_contract`.
6. **Negative controls.** Each changes one binding, the sign, a parameter,
   the Sample-time offset or the input Latency, and must fail the oracle
   comparison at its predicted first divergence.
7. **Failures.** Each must exit with its code and name its cause, within the
   whole-Run guard, and leave no importer process behind.

    python prove.py OUT_DIR --fmpy-python /usr/local/bin/python

writes the reports into OUT_DIR, every Run into OUT_DIR/work, and exits 1
when a step fails.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import experiment
from experiment import CASES, CONTROLS, FAILURES, PERIOD_NS, Case

from sil.recording import read_records
from sil.schema import MessageType

ROOT = experiment.ROOT
EXAMPLE_DIR = experiment.EXAMPLE_DIR
package = experiment.load("adas_reference_package",
                          EXAMPLE_DIR / "fmu" / "package.py")
prepare = experiment.load("adas_reference_prepare", EXAMPLE_DIR / "prepare.py")

PIN = ROOT / "proofs" / "adas-fmu" / "evidence" / "AdasReference.identity.json"
INDEPENDENT = experiment.PROOF_DIR / "independent.py"
# The whole-Run guard: a Native participant runs inside sil-run, so only an
# external bound stops a native hang or crash loop. A Process participant
# also has its response deadline.
RUN_TIMEOUT_S = 300
PARTICIPANT_TIMEOUT_MS = 30_000
FORMS = ("native", "fmu")
COMMAND = MessageType("adas.Command", experiment.SCHEMAS["adas.Command"])


class Proof:
    def __init__(self, out: Path, fmpy_python: str, cc: str):
        self.out = out
        self.work = out / "work"
        self.inputs = self.work / "inputs"
        self.fmpy_python = fmpy_python
        self.cc = cc
        runner = shutil.which("sil-run")
        if runner is None:
            raise SystemExit("prove.py: no sil-run on PATH; install SiL first")
        self.prefix = Path(runner).resolve().parents[1]

    # --- artifacts ---------------------------------------------------------

    def artifacts(self) -> dict:
        self.work.mkdir(parents=True, exist_ok=True)
        identity = package.build(self.work / "fmu")
        self.fmu = self.work / "fmu" / f"{package.MODEL_IDENTIFIER}.fmu"
        wrong = package.build(self.work / "fmu-wrong-sign",
                              defines=("ADAS_REFERENCE_WRONG_SIGN",))
        self.wrong_sign_fmu = (self.work / "fmu-wrong-sign"
                               / f"{package.MODEL_IDENTIFIER}.fmu")
        pinned = json.loads(PIN.read_text())
        self.library = self._library()
        prepare.prepare(self.inputs)
        same_compiler = pinned["compiler"] == identity["compiler"] and \
            pinned["platform"] == identity["platform"]
        return {
            "fmu": {"sha256": identity["archive_sha256"],
                    "instantiation_token": identity["instantiation_token"],
                    "compiler": identity["compiler"],
                    "platform": identity["platform"]},
            "pinned_sha256": pinned["archive_sha256"],
            # The pin holds for the pinned compiler and platform only.
            "matches_pin": (identity["archive_sha256"]
                            == pinned["archive_sha256"]) if same_compiler
            else None,
            "wrong_sign_fmu": {"sha256": wrong["archive_sha256"]},
            "library": {"sha256": experiment.sha256(self.library)},
        }

    def _library(self) -> Path:
        include = self.work / "include"
        _run(["silschema", str(EXAMPLE_DIR / "schemas.json"),
              str(include / "adas_messages.h")])
        library = self.work / "adas_reference.so"
        _run([self.cc, "-std=c11", "-O2", "-ffp-contract=off", "-Wall",
              "-Wextra", "-shared", "-fPIC", f"-I{self.prefix / 'include'}",
              f"-I{EXAMPLE_DIR}", f"-I{include}", "-o", str(library),
              str(EXAMPLE_DIR / "adas_reference.c"),
              str(EXAMPLE_DIR / "sil_adapter.c"), "-lm"])
        return library

    # --- forms and runs ----------------------------------------------------

    def manifests(self, case: Case) -> dict[str, Path]:
        paths = {}
        for form, m in (
                ("native", experiment.native_manifest(case, self.inputs,
                                                      self.library)),
                ("fmu", experiment.fmu_manifest(case, self.inputs, self.fmu))):
            paths[form] = self.work / f"{case.name}.{form}.json"
            m.write(paths[form])
        return paths

    def forms(self) -> dict:
        report = {}
        for case in CASES.values():
            paths = self.manifests(case)
            docs = {form: json.loads(p.read_text()) for form, p in paths.items()}
            try:
                report[case.name] = experiment.form_difference(
                    case, docs["native"], docs["fmu"])
            except experiment.FormError as error:
                report[case.name] = {"error": str(error)}
        return report

    def runs(self) -> dict:
        report = {}
        for case in CASES.values():
            for form in FORMS:
                manifest = self.work / f"{case.name}.{form}.json"
                recordings = [self.work / f"{case.name}.{form}-{i}.mcap"
                              for i in (1, 2)]
                results = [sil_run(manifest, r) for r in recordings]
                identical = all(r["exit"] == 0 for r in results) and \
                    recordings[0].read_bytes() == recordings[1].read_bytes()
                findings = (observation_findings(case, recordings[0])
                            if results[0]["exit"] == 0 else ["Run failed"])
                report[f"{case.name}.{form}"] = {
                    "runs": results, "identical_bytes": identical,
                    "observation_findings": findings,
                    "passed": identical and not findings}
        return {"cases": report,
                "cadence_periods": cadence_period_findings(self.inputs)}

    # --- the independent execution -------------------------------------------

    def independent(self) -> dict:
        report = {}
        for case in CASES.values():
            declaration = self.work / f"{case.name}.case.json"
            declaration.write_text(json.dumps({
                "interceptors": case.interceptors,
                "input_latency_ns": case.input_latency_ns,
                "start": experiment.FMU_STARTS}, indent=2) + "\n")
            rows = self.work / f"{case.name}.fmpy.csv"
            superseded = self.work / f"{case.name}.superseded.json"
            proc = subprocess.run(
                [self.fmpy_python, str(INDEPENDENT), str(self.fmu),
                 str(self.inputs / f"{case.maneuver}.inputs.csv"),
                 str(declaration), str(rows), str(superseded)],
                capture_output=True, text=True, timeout=RUN_TIMEOUT_S)
            entry = {"exit": proc.returncode, "stderr": proc.stderr.strip()}
            if proc.returncode == 0:
                _run(["sil-csv",
                      str(self.inputs / f"{case.maneuver}.expected.mapping.json"),
                      str(rows), "-o", str(self.fmpy(case))])
                # The consumption difference must be exactly the declared one.
                entry["superseded"] = json.loads(superseded.read_text())
                entry["declared"] = experiment.SUPERSEDED.get(case.name, [])
            entry["passed"] = (proc.returncode == 0
                               and entry.get("superseded") == entry.get("declared"))
            report[case.name] = entry
        return report

    def fmpy(self, case: Case) -> Path:
        return self.work / f"{case.name}.fmpy.mcap"

    # --- comparisons ---------------------------------------------------------

    def comparisons(self) -> dict:
        report, contracts = {}, {}
        for case in CASES.values():
            oracle_contract = self.inputs / f"{case.maneuver}.contract.json"
            cross_contract = self.work / f"{case.maneuver}.cross-form.json"
            cross_contract.write_text(json.dumps(
                experiment.cross_form_contract(case.maneuver), indent=2) + "\n")
            # The FMPy rows, like the oracle's, are stored at their Sample
            # time: no offset on either side.
            independent_contract = self.work / f"{case.maneuver}.independent.json"
            independent_contract.write_text(json.dumps(
                with_actual_offset(oracle_contract, 0), indent=2) + "\n")
            contracts[case.maneuver] = {
                name: json.loads(path.read_text()) for name, path in (
                    ("oracle", oracle_contract),
                    ("independent", independent_contract),
                    ("cross_form", cross_contract))}
            oracle = self.inputs / f"{case.name}.expected.mcap"
            native = self.work / f"{case.name}.native-1.mcap"
            fmu = self.work / f"{case.name}.fmu-1.mcap"
            pairs = {
                "native-oracle": (oracle_contract, native, oracle),
                "fmu-oracle": (oracle_contract, fmu, oracle),
                "fmpy-oracle": (independent_contract, self.fmpy(case), oracle),
                "native-fmpy": (oracle_contract, native, self.fmpy(case)),
                "fmu-fmpy": (oracle_contract, fmu, self.fmpy(case)),
                "native-fmu": (cross_contract, native, fmu),
            }
            report[case.name] = {name: summary(sil_compare(*args))
                                 for name, args in pairs.items()}
        return {"cases": report, "contracts": contracts}

    # --- negative controls ---------------------------------------------------

    def controls(self) -> dict:
        report = {}
        for name, control in CONTROLS.items():
            case = CASES[control.case]
            archive = (self.wrong_sign_fmu if control.archive_define
                       else self.fmu)
            m = experiment.run_manifest(
                case, self.inputs,
                experiment.fmu_controller(
                    archive, **experiment.controller_changes(control)),
                input_latency_ns=control.input_latency_ns)
            path = self.work / f"control-{name}.json"
            m.write(path)
            recording = self.work / f"control-{name}.mcap"
            run = sil_run(path, recording)
            contract = self.inputs / f"{case.maneuver}.contract.json"
            if control.actual_offset_ns is not None:
                document = with_actual_offset(contract,
                                              control.actual_offset_ns)
                contract = self.work / f"control-{name}.contract.json"
                contract.write_text(json.dumps(document, indent=2) + "\n")
            result = sil_compare(
                contract, recording,
                self.inputs / f"{case.name}.expected.mcap")
            first = result["report"].get("first_divergence") or {}
            predicted = dataclasses.asdict(control.prediction)
            observed = {key: first.get(key) for key in predicted}
            detected = (run["exit"] == 0 and result["exit"] == 1
                        and first.get("kind") == "value"
                        and first.get("channel") == f"{case.maneuver}.command"
                        and observed == predicted)
            report[name] = {"case": case.name, "why": control.why,
                            "run": run, "compare_exit": result["exit"],
                            "predicted": predicted, "observed": observed,
                            "divergences": result["report"].get("divergences"),
                            "detected": detected}
        return report

    # --- failures ------------------------------------------------------------

    def failures(self) -> dict:
        report = {}
        for name, failure in FAILURES.items():
            case = dataclasses.replace(
                CASES[failure.case],
                interceptors=failure.interceptors or CASES[failure.case].interceptors)
            changes = {"step_period_ns": failure.step_period_ns,
                       **experiment.controller_changes(failure)}
            forms = {"fmu": (experiment.fmu_controller(self.fmu, **changes),
                             failure.diagnostics)}
            if failure.native_diagnostics:
                forms["native"] = (
                    experiment.manifest.native_controller(self.library),
                    failure.native_diagnostics)
            entry = {"case": case.name, "why": failure.why,
                     "expected_exit": failure.exit_code}
            for form, (controller, diagnostics) in forms.items():
                path = self.work / f"failure-{name}.{form}.json"
                experiment.run_manifest(case, self.inputs, controller).write(path)
                run = sil_run(path, self.work / f"failure-{name}.{form}.mcap")
                leftover = importer_processes(self.fmu)
                entry[form] = {
                    **run, "leftover_processes": leftover,
                    "diagnostics": list(diagnostics),
                    "passed": (run["exit"] == failure.exit_code
                               and all(d in run["stderr"] for d in diagnostics)
                               and not leftover)}
            entry["passed"] = all(entry[form]["passed"] for form in forms)
            report[name] = entry
        return report


# --- helpers -----------------------------------------------------------------


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True, capture_output=True, text=True)


def with_actual_offset(contract: Path, offset_ns: int) -> dict:
    """The contract document with every Channel's `actual_offset_ns` set."""
    document = json.loads(contract.read_text())
    for rule in document["channels"].values():
        rule["actual_offset_ns"] = offset_ns
    return document


def sil_run(manifest: Path, recording: Path,
            timeout_s: float = RUN_TIMEOUT_S) -> dict:
    """One Run under the whole-Run guard; its exit, time and stderr.

    At the guard, every process of the Run is stopped: sil-run and each
    participant, which sil-run starts in a process group of its own."""
    started = time.monotonic()
    proc = subprocess.Popen(
        ["sil-run", str(manifest), "-o", str(recording),
         "--participant-timeout-ms", str(PARTICIPANT_TIMEOUT_MS)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        _, stderr = proc.communicate(timeout=timeout_s)
        status = proc.returncode
    except subprocess.TimeoutExpired:
        stop_process_tree(proc.pid)
        _, stderr = proc.communicate()
        status = "timeout"
    return {"exit": status, "seconds": round(time.monotonic() - started, 3),
            "stderr": stderr.strip()}


def descendants(pid: int) -> list[int]:
    """Every process below `pid`, read from the process table."""
    table = subprocess.run(["ps", "-eo", "pid=,ppid="], capture_output=True,
                           text=True, check=True).stdout.split()
    children: dict[int, list[int]] = {}
    for child, parent in zip(table[::2], table[1::2]):
        children.setdefault(int(parent), []).append(int(child))
    found, pending = [], [pid]
    while pending:
        below = children.get(pending.pop(), [])
        found += below
        pending += below
    return found


def stop_process_tree(pid: int) -> None:
    """Kill `pid` and every process below it. The tree is read before any
    kill: a killed parent's children move to init and leave the tree."""
    for target in [pid, *descendants(pid)]:
        try:
            os.kill(target, signal.SIGKILL)
        except ProcessLookupError:
            pass


def sil_compare(contract: Path, actual: Path, reference: Path) -> dict:
    try:
        proc = subprocess.run(
            ["sil-compare", str(contract), str(actual), str(reference),
             "--json"], capture_output=True, text=True, timeout=RUN_TIMEOUT_S)
    except subprocess.TimeoutExpired as error:
        return {"exit": "timeout", "report": {"error": str(error)}}
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        report = {"error": proc.stderr.strip()}
    return {"exit": proc.returncode, "report": report}


def summary(result: dict) -> dict:
    report = result["report"]
    return {"exit": result["exit"], "verdict": report.get("verdict"),
            "channels": report.get("channels"),
            "divergences": report.get("divergences"),
            "first_divergence": report.get("first_divergence"),
            "contract_sha256": (report.get("contract") or {}).get("sha256"),
            "passed": result["exit"] == 0}


def commands(recording: Path, maneuver: str) -> list[tuple[int, dict]]:
    return [(t, COMMAND.unpack(payload))
            for channel, t, payload in read_records(recording)
            if channel == f"{maneuver}.command"]


def observation_findings(case: Case, recording: Path) -> list[str]:
    """Every Command, first through final: Slot t, Sample time t + 10 ms."""
    findings = []
    if experiment.DURATION_NS % PERIOD_NS:
        findings.append("the Duration is not a whole number of Periods")
    got = commands(recording, case.maneuver)
    slots = list(range(0, experiment.DURATION_NS, PERIOD_NS))
    if [t for t, _ in got] != slots:
        findings.append(f"Commands in Slots {[t for t, _ in got]}, "
                        f"expected {slots}")
    for k, (t, fields) in enumerate(got):
        if fields["sample_time_ns"] != t + PERIOD_NS:
            findings.append(f"Slot {t}: Sample time {fields['sample_time_ns']}")
        if fields["sequence"] != k + 1:
            findings.append(f"Slot {t}: sequence {fields['sequence']}")
    if case.name in experiment.INITIALLY_UNAVAILABLE and got and \
            got[0][1]["mode"] != experiment.UNAVAILABLE:
        findings.append("the first activation is not SENSOR_UNAVAILABLE")
    return findings


def cadence_period_findings(inputs: Path) -> list[str]:
    """The `cadence` inputs publish at the declared Periods, over the
    whole Duration."""
    times: dict[str, list[int]] = {}
    for channel, t, _ in read_records(inputs / "cadence.inputs.mcap"):
        times.setdefault(channel.removeprefix("cadence."), []).append(t)
    return [f"{role} published at {times.get(role)}"
            for role, period in experiment.CADENCE_PERIODS_NS.items()
            if experiment.DURATION_NS % period
            or times.get(role) != list(range(0, experiment.DURATION_NS, period))]


def importer_processes(fmu: Path) -> list[str]:
    """Processes still running the importer on `fmu`."""
    proc = subprocess.run(["ps", "-eo", "args"], capture_output=True,
                          text=True, check=True)
    return [line for line in proc.stdout.splitlines()
            if "sil.fmi" in line and str(fmu.resolve()) in line]


def prove(out: Path, fmpy_python: str, cc: str) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    proof = Proof(out, fmpy_python, cc)
    sections = {
        "artifacts": proof.artifacts(),
        "forms": proof.forms(),
        "runs": proof.runs(),
        "independent": proof.independent(),
        "comparisons": proof.comparisons(),
        "controls": proof.controls(),
        "failures": proof.failures(),
    }
    for name, section in sections.items():
        (out / f"{name}.json").write_text(json.dumps(section, indent=2) + "\n")
    passed = {
        "pin": sections["artifacts"]["matches_pin"] is not False,
        "forms": all("error" not in f for f in sections["forms"].values()),
        "runs": all(r["passed"] for r in sections["runs"]["cases"].values())
        and not sections["runs"]["cadence_periods"],
        "independent": all(r["passed"]
                           for r in sections["independent"].values()),
        "comparisons": all(c["passed"] for case in
                           sections["comparisons"]["cases"].values()
                           for c in case.values()),
        "controls": all(c["detected"] for c in sections["controls"].values()),
        "failures": all(f["passed"] for f in sections["failures"].values()),
    }
    result = {"cases": sorted(CASES), "steps": passed,
              "passed": all(passed.values()),
              "matches_pin": sections["artifacts"]["matches_pin"],
              "fmu_sha256": sections["artifacts"]["fmu"]["sha256"]}
    (out / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out_dir", type=Path, help="where the reports go")
    parser.add_argument("--fmpy-python", default="python3",
                        help="a Python with FMPy, for independent.py")
    args = parser.parse_args()
    result = prove(args.out_dir, args.fmpy_python, os.environ.get("CC", "cc"))
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["passed"] else 1)
