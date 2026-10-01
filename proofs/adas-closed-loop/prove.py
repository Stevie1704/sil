"""Close a processed-sensor loop with interchangeable native and FMU
controllers (issue #227).

With installed SiL (`sil-run`, `silschema`, `sil-csv`, `sil-compare`, the
`sil` wheel and `include/sil`), a C compiler, a Python with FMPy, and the
ACC plant archive built by proofs/acc-fmi/build.py:

1. **Artifacts.** Build `AdasReference.fmu` and hold it to its #225 pin;
   build the Native library; require the plant archive's model and dynamics
   to be the ones the ACC evidence qualified, and its interface to be the
   one loop.py binds.
2. **Manifests.** Author the native and the FMU Manifest of every case from
   one declaration; require that only the controller entry differs and that
   the consumption table has no finding.
3. **Runs.** Run each Manifest twice and require identical Recording bytes,
   one Command and one truth Message in every Slot through the final one,
   the KPIs, and the expected modes at their predicted Sample times.
4. **Independent execution.** Run every case with FMPy (independent.py).
5. **Comparisons.** Each form against the independent execution, and the
   native form against the FMU form.
6. **Effects.** Sampling and hold, and sensor Latency, each within the
   declared envelope against its baseline.
7. **Deliberate failures.** A KPI and a comparison that the faulted Run
   must fail where predicted.
8. **Failures.** Each must exit with its code, name its cause, and leave
   no process of the Run behind.

    python prove.py OUT_DIR --plant /fmus/AccPlant.fmu \
        --fmpy-python /usr/local/bin/python

writes the reports into OUT_DIR, every Run into OUT_DIR/work, and exits 1
when a step fails.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict
from pathlib import Path

import kpi
import loop
from loop import ALL_CASES, CASES, DELIBERATE, EFFECTS, FAILURES, STEP_NS

from sil.recording import read_records
from sil.schema import MessageType

ROOT = loop.ROOT
EXAMPLE_DIR = loop.EXAMPLE_DIR
package = loop.load("adas_closed_loop_package",
                    EXAMPLE_DIR / "fmu" / "package.py")
PIN = ROOT / "proofs" / "adas-fmu" / "evidence" / "AdasReference.identity.json"
INDEPENDENT = loop.PROOF_DIR / "independent.py"
RUN_TIMEOUT_S = 600
PARTICIPANT_TIMEOUT_MS = 30_000
FORMS = ("native", "fmu")
COMMAND = MessageType("adas.Command", loop.SCHEMAS["adas.Command"])
TRUTH = MessageType("loop.Truth", loop.SCHEMAS["loop.Truth"])
# The plant variables the Manifest binds, by causality.
PLANT_INTERFACE = {
    "input": {"accel_mps2": "m/s2", "lead_accel_mps2": "m/s2",
              "initial_lead_position_m": "m"},
    "output": {"ego_position_m": "m", "ego_speed_mps": "m/s",
               "lead_position_m": "m", "lead_speed_mps": "m/s",
               "gap_m": "m", "relative_speed_mps": "m/s"},
}


class Proof:
    def __init__(self, out: Path, plant: Path, fmpy_python: str, cc: str):
        self.out = out
        self.work = out / "work"
        self.plant_fmu = plant
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
        pinned = json.loads(PIN.read_text())
        same_compiler = (pinned["compiler"] == identity["compiler"]
                         and pinned["platform"] == identity["platform"])
        self.library = self._library()
        return {
            "controller_fmu": {"sha256": identity["archive_sha256"],
                               "compiler": identity["compiler"],
                               "platform": identity["platform"],
                               "pinned_sha256": pinned["archive_sha256"],
                               "matches_pin": (identity["archive_sha256"]
                                               == pinned["archive_sha256"])
                               if same_compiler else None},
            "library": {"sha256": loop.equivalence.sha256(self.library)},
            "plant": plant_record(self.plant_fmu),
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

    def controller(self, form: str) -> loop.Controller:
        return (loop.native_controller(self.library) if form == "native"
                else loop.fmu_controller(self.fmu))

    def plant(self) -> loop.Plant:
        return loop.fmu_plant(self.plant_fmu)

    # --- Manifests and Runs -------------------------------------------------

    def manifests(self) -> dict:
        report = {}
        for case in ALL_CASES.values():
            docs = {}
            entry: dict = {"why": case.why}
            try:
                for form in FORMS:
                    docs[form] = loop.loop_manifest(case, self.controller(form),
                                                    self.plant())
                    loop.write(docs[form], self.manifest(case.name, form))
                entry["differences"] = loop.form_difference(docs["native"],
                                                            docs["fmu"])
                entry["table"] = loop.consumption_table(docs["native"])
            except loop.TableError as error:
                entry["error"] = str(error)
            report[case.name] = entry
        effects = {}
        for variant, (baseline, isolates, _) in EFFECTS.items():
            entry = {"baseline": baseline, "isolates": isolates}
            try:
                entry["differences"] = loop.effect_difference(
                    variant, *(json.loads(self.manifest(name, "native")
                                          .read_text())
                               for name in (baseline, variant)))
            except loop.TableError as error:
                entry["error"] = str(error)
            effects[variant] = entry
        report["effects"] = effects
        return report

    def manifest(self, name: str, form: str) -> Path:
        return self.work / f"{name}.{form}.json"

    def recording(self, name: str, form: str, run: int = 1) -> Path:
        return self.work / f"{name}.{form}-{run}.mcap"

    def runs(self) -> dict:
        report = {}
        for case in ALL_CASES.values():
            for form in FORMS:
                results = [sil_run(self.manifest(case.name, form),
                                   self.recording(case.name, form, i))
                           for i in (1, 2)]
                identical = all(r["exit"] == 0 for r in results) and \
                    self.recording(case.name, form, 1).read_bytes() == \
                    self.recording(case.name, form, 2).read_bytes()
                entry = {"runs": results, "identical_bytes": identical}
                if results[0]["exit"] == 0:
                    entry.update(behavior(case, *self.decoded(case.name,
                                                              form)))
                entry["passed"] = identical and not entry.get(
                    "findings", ["Run failed"])
                report[f"{case.name}.{form}"] = entry
        return report

    def decoded(self, name: str, form: str) -> tuple[list, list]:
        return decode(self.recording(name, form))

    # --- the independent execution -------------------------------------------

    def independent(self) -> dict:
        report = {}
        mapping = self.work / "independent.mapping.json"
        mapping.write_text(json.dumps(loop.independent_mapping(), indent=2)
                           + "\n")
        runs = [(case, case.name, True) for case in ALL_CASES.values()]
        # The deliberate comparison: the faulted case without its fault.
        runs.append((CASES["sensor_loss"], "sensor_loss.unfaulted", False))
        for case, name, faults in runs:
            declaration = self.work / f"{name}.case.json"
            declaration.write_text(json.dumps(
                loop.independent_declaration(case, faults), indent=2) + "\n")
            rows = self.work / f"{name}.fmpy.csv"
            initial = self.work / f"{name}.fmpy-initial.json"
            proc = subprocess.run(
                [self.fmpy_python, str(INDEPENDENT), str(self.plant_fmu),
                 str(self.fmu), str(declaration), str(rows), str(initial)],
                capture_output=True, text=True, timeout=RUN_TIMEOUT_S)
            entry = {"exit": proc.returncode, "stderr": proc.stderr.strip()}
            if proc.returncode == 0:
                _run(["sil-csv", str(mapping), str(rows), "-o",
                      str(self.fmpy(name))])
                entry["initial_truth"] = json.loads(initial.read_text())
            entry["passed"] = proc.returncode == 0
            report[name] = entry
        return report

    def fmpy(self, name: str) -> Path:
        return self.work / f"{name}.fmpy.mcap"

    # --- comparisons and effects ---------------------------------------------

    def comparisons(self) -> dict:
        contracts = {"independent": self.work / "independent.contract.json",
                     "cross_form": self.work / "cross-form.contract.json"}
        contracts["independent"].write_text(
            json.dumps(loop.INDEPENDENT_CONTRACT, indent=2) + "\n")
        contracts["cross_form"].write_text(
            json.dumps(loop.CROSS_FORM_CONTRACT, indent=2) + "\n")
        report = {}
        for case in ALL_CASES.values():
            native = self.recording(case.name, "native")
            fmu = self.recording(case.name, "fmu")
            pairs = {
                "native-independent": (contracts["independent"], native,
                                       self.fmpy(case.name)),
                "fmu-independent": (contracts["independent"], fmu,
                                    self.fmpy(case.name)),
                "native-fmu": (contracts["cross_form"], native, fmu),
            }
            report[case.name] = {name: summary(sil_compare(*args))
                                 for name, args in pairs.items()}
        return {"cases": report,
                "contracts": {"independent": loop.INDEPENDENT_CONTRACT,
                              "cross_form": loop.CROSS_FORM_CONTRACT}}

    def effects(self) -> dict:
        report = {}
        for variant, (baseline, isolates, _) in EFFECTS.items():
            for form in FORMS:
                result = kpi.effect(self.decoded(baseline, form),
                                    self.decoded(variant, form), loop.HAZARD,
                                    STEP_NS, loop.EFFECT_ENVELOPE)
                report[f"{variant}.{form}"] = {
                    "baseline": baseline, "isolates": isolates, **result,
                    "passed": not result["findings"]}
        return {"envelope": loop.EFFECT_ENVELOPE, "effects": report}

    # --- deliberate failures --------------------------------------------------

    def deliberate(self) -> dict:
        report = {}
        spec = DELIBERATE["kpi"]
        for form in FORMS:
            commands, truth = self.decoded(spec["case"], form)
            observed = kpi.first_violation(commands, truth, spec["kpi"],
                                           STEP_NS)
            seen = {k: observed[k] for k in spec["prediction"]} \
                if observed else None
            report[f"kpi.{form}"] = {
                "why": spec["why"], "kpi": spec["kpi"],
                "predicted": spec["prediction"], "observed": observed,
                "detected": seen == spec["prediction"]}
        spec = DELIBERATE["comparison"]
        predicted = asdict(spec["prediction"])
        for form in FORMS:
            result = sil_compare(self.work / "independent.contract.json",
                                 self.recording(spec["case"], form),
                                 self.fmpy("sensor_loss.unfaulted"))
            first = result["report"].get("first_divergence") or {}
            observed = {key: first.get(key) for key in predicted}
            report[f"comparison.{form}"] = {
                "why": spec["why"], "compare_exit": result["exit"],
                "predicted": predicted, "observed": observed,
                "detected": result["exit"] == 1
                and first.get("kind") == "value" and observed == predicted}
        return report

    # --- failures --------------------------------------------------------------

    def failures(self) -> dict:
        report = {}
        for name, failure in FAILURES.items():
            entry: dict = {"case": failure.case, "why": failure.why,
                           "expected_exit": failure.exit_code}
            forms = FORMS if failure.both_forms else ("fmu",)
            for form in forms:
                doc = loop.failure_document(failure, self.controller(form),
                                            self.plant())
                path = loop.write(doc, self.work
                                  / f"failure-{name}.{form}.json")
                run = sil_run(path, self.work / f"failure-{name}.{form}.mcap")
                leftover = run_processes()
                entry[form] = {
                    **run, "leftover_processes": leftover,
                    "diagnostics": list(failure.diagnostics),
                    "passed": (run["exit"] == failure.exit_code
                               and all(d in run["stderr"]
                                       for d in failure.diagnostics)
                               and not leftover)}
            entry["passed"] = all(entry[form]["passed"] for form in forms)
            report[name] = entry
        return report


# --- helpers -------------------------------------------------------------------


def plant_record(archive: Path) -> dict:
    """The plant archive against the identity the ACC evidence qualified,
    and its interface against the one loop.py binds."""
    with zipfile.ZipFile(archive) as z:
        identity = json.loads(z.read("resources/identity.json"))
        description = ET.fromstring(z.read("modelDescription.xml"))
    qualified = json.loads(loop.QUALIFIED_PLANT.read_text())[
        "fmus"]["AccPlant"]["exporter_identity"]
    # The source revision and the archive digest name the build; the rest
    # names the model.
    model_keys = sorted(set(qualified) - {"source_revision", "archive_sha256"})
    variables = {v.get("name"): v for v in description.find("ModelVariables")}
    interface = []
    for causality, names in PLANT_INTERFACE.items():
        for name, unit in names.items():
            v = variables.get(name)
            if v is None or v.tag != "Float64" or \
                    v.get("causality") != causality or v.get("unit") != unit:
                interface.append(f"{name}: expected Float64 {causality} in "
                                 f"{unit}, found "
                                 f"{None if v is None else dict(v.attrib)}")
    identity_matches = all(identity.get(k) == qualified[k] for k in model_keys)
    return {"identity": identity,
            "qualified": {k: qualified[k] for k in model_keys},
            "identity_matches": identity_matches,
            "interface_findings": interface,
            "passed": identity_matches and not interface}


def decode(recording: Path) -> tuple[list, list]:
    commands, truth = [], []
    for channel, t, payload in read_records(recording):
        if channel == "adas.command":
            commands.append((t, COMMAND.unpack(payload)))
        elif channel == "loop.truth":
            truth.append((t, TRUTH.unpack(payload)))
    return commands, truth


def behavior(case: loop.Case, commands: list, truth: list) -> dict:
    """The KPIs, coverage and modes of one Run."""
    runs = kpi.mode_runs(commands, STEP_NS)
    violation = kpi.first_violation(commands, truth, loop.KPI, STEP_NS)
    findings = (kpi.coverage(commands, truth, STEP_NS, loop.DURATION_NS)
                + kpi.mode_findings(runs, case.modes))
    if violation:
        findings.append(f"KPI {violation}")
    return {"modes": runs, "expected_modes": case.modes,
            **kpi.summary(commands, truth), "first_violation": violation,
            "commands": len(commands), "truth": len(truth),
            "final_command": commands[-1][1] if commands else None,
            "findings": findings}


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True, capture_output=True, text=True)


def sil_run(manifest: Path, recording: Path,
            timeout_s: float = RUN_TIMEOUT_S) -> dict:
    """One Run under the whole-Run guard; at the guard every process of the
    Run is stopped, also the participants in process groups of their own."""
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
    for target in [pid, *descendants(pid)]:
        try:
            os.kill(target, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_processes() -> list[str]:
    """Processes of a Run still alive: an importer, an edge Participant."""
    proc = subprocess.run(["ps", "-eo", "args"], capture_output=True,
                          text=True, check=True)
    return [line for line in proc.stdout.splitlines()
            if ("sil.fmi" in line or str(loop.EDGE) in line)
            and "ps -eo" not in line]


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
            "first_divergence": report.get("first_divergence"),
            "passed": result["exit"] == 0}


def prove(out: Path, plant: Path, fmpy_python: str, cc: str) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    proof = Proof(out, plant, fmpy_python, cc)
    sections = {"artifacts": proof.artifacts(), "manifests": proof.manifests()}
    sections["runs"] = proof.runs()
    sections["independent"] = proof.independent()
    sections["comparisons"] = proof.comparisons()
    sections["effects"] = proof.effects()
    sections["deliberate"] = proof.deliberate()
    sections["failures"] = proof.failures()
    for name, section in sections.items():
        (out / f"{name}.json").write_text(
            json.dumps(section, indent=2, default=str) + "\n")
    artifacts = sections["artifacts"]
    passed = {
        "controller_pin": artifacts["controller_fmu"]["matches_pin"]
        is not False,
        "plant": artifacts["plant"]["passed"],
        "manifests": all("error" not in m for name, m in
                         sections["manifests"].items() if name != "effects")
        and all("error" not in e
                for e in sections["manifests"]["effects"].values()),
        "runs": all(r["passed"] for r in sections["runs"].values()),
        "independent": all(r["passed"]
                           for r in sections["independent"].values()),
        "comparisons": all(c["passed"] for case in
                           sections["comparisons"]["cases"].values()
                           for c in case.values()),
        "effects": all(e["passed"]
                       for e in sections["effects"]["effects"].values()),
        "deliberate": all(d["detected"]
                          for d in sections["deliberate"].values()),
        "failures": all(f["passed"] for f in sections["failures"].values()),
    }
    result = {"cases": sorted(ALL_CASES), "steps": passed,
              "passed": all(passed.values()),
              "controller_fmu_sha256": artifacts["controller_fmu"]["sha256"],
              "plant_sha256": loop.equivalence.sha256(plant)}
    (out / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out_dir", type=Path, help="where the reports go")
    parser.add_argument("--plant", type=Path, required=True,
                        help="AccPlant.fmu from proofs/acc-fmi/build.py")
    parser.add_argument("--fmpy-python", default="python3",
                        help="a Python with FMPy, for independent.py")
    args = parser.parse_args()
    result = prove(args.out_dir, args.plant, args.fmpy_python,
                   os.environ.get("CC", "cc"))
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["passed"] else 1)
