"""The agent investigation proof of issue #283, offline and deterministic.

`proofs/agent-investigation/prepare.py` writes the four cases from the
source tree, and the staged installation seals and runs them as one failed
regression matrix. These tests check what the proof relies on: the case
statuses and failure evidence, the driver's follow-up experiment and its
refusals and budgets, and `verify.py`, which must accept true claims and
reject invented ones. The last tests check the retained real-agent trial:
its reports against its retained evidence, and its experiments repeated here
from their specs, which must give the same values.

No test calls a model. A canned report is a fixture for the checks, not
evidence of diagnostic ability.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import ROOT, load_module
from test_staged_install import installed_environment

PROOF = ROOT / "proofs" / "agent-investigation"
TRIAL = PROOF / "evidence" / "trial-1"
MS = 1_000_000
DELAY = "delay-regression"

prepare = load_module("investigation_prepare", PROOF / "prepare.py")
verifier = load_module("investigation_verify", PROOF / "verify.py")
bundles = load_module("bundle_prepare", ROOT / "examples" / "bundle" / "prepare.py")


@pytest.fixture(scope="module")
def runtime(installed_python: Path, staged_prefix: Path):
    return bundles.Runtime(installed_python / "bin", staged_prefix / "bin")


@pytest.fixture(scope="module")
def target(build_dir: Path) -> Path:
    path = build_dir / "adas_reference_arrival_time.so"
    assert path.is_file(), f"controller build missing at {path}"
    return path


def prepared(out: Path, runtime, target: Path) -> Path:
    prepare.prepare(out, runtime, target)
    return out


@pytest.fixture(scope="module")
def proof(tmp_path_factory, runtime, target) -> Path:
    """One prepared proof. Tests add experiments with distinct names only."""
    return prepared(tmp_path_factory.mktemp("investigation"), runtime, target)


def driver(runtime, agent: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(runtime.python_bin / "python3"), "tools/investigate.py", *map(str, args)],
        cwd=agent, env=installed_environment(runtime.python_bin.parent),
        capture_output=True, text=True)


def spec(agent: Path, name: str, **fields) -> Path:
    doc = {"sil_investigation_experiment": 1, "name": name, "parent": DELAY,
           "hypothesis": "ages count from arrival",
           "distinguishes": "a route fault predicts a pass without the delay",
           "channels": {}, "references": [], **fields}
    path = agent / "specs" / f"{name}.json"
    path.write_text(json.dumps(doc))
    return path.relative_to(agent)


LATE = {channel: {"latency_ns": 10 * MS} for channel in prepare.INPUT_CHANNELS}
LATE[f"{prepare.MANEUVER}.radar"]["interceptors"] = []


def sha256(path: Path) -> str:
    return verifier.sha256(path)


def command_at(recording: Path, t_ns: int, field: str):
    return verifier.recorded_value(recording, "cadence.command", t_ns, 0, field, None)


class TestCases:
    def test_the_matrix_reports_each_case_status(self, proof):
        summary = json.loads((proof / "agent" / "ci" / "summary.json").read_text())

        assert {c["name"]: c["status"] for c in summary["cases"]} == {
            DELAY: "behavioral-failure", "nominal-control": "pass",
            "monitor-timeout": "behavioral-failure",
            "late-private": "behavioral-failure"}

    def test_the_behavioral_failure_is_a_completed_run_that_fails_its_reference(
            self, proof):
        runs = proof / "agent" / "ci" / "cases" / DELAY / "runs" / DELAY
        report = json.loads((runs / "cadence.delay.comparison.json").read_text())

        assert not (runs / "run-1.mcap.failure.json").exists()
        assert report["verdict"] == "fail"
        # The first divergence is a mode, not the age that causes it.
        assert (report["first_divergence"]["field"],
                report["first_divergence"]["actual_publication_ns"]) == ("mode", 110 * MS)
        assert command_at(runs / "run-1.mcap", 110 * MS, "radar_age_ns") == 0

    def test_the_operational_failure_has_a_timeout_report_and_no_comparison(
            self, proof):
        case = proof / "agent" / "ci" / "cases" / "monitor-timeout"
        runs = case / "runs" / "monitor-timeout"
        failure = json.loads((runs / "run-1.mcap.failure.json").read_text())

        assert (failure["category"], failure["participant"],
                failure["virtual_time_ns"]) == ("timeout", "monitor", 100 * MS)
        (run,) = json.loads((case / "summary.json").read_text())["runs"]
        assert run["exit_code"] == 1 and run["comparisons"] == {}
        assert not list(runs.glob("*.comparison.json"))

    def test_the_private_case_shares_only_its_summary(self, proof):
        agent = proof / "agent"
        case = agent / "ci" / "cases" / "late-private"

        assert [p.name for p in case.iterdir()] == ["summary.json"]
        assert not (agent / "bundles" / "late-private").exists()
        (run,) = json.loads((case / "summary.json").read_text())["runs"]
        assert run["comparisons"]["cadence.late"]["verdict"] == "fail"

    def test_the_workspace_holds_no_evaluator_reference(self, proof):
        names = {p.name for p in (proof / "agent").rglob("*")}
        assert "reference.json" not in names and "cases.json" not in names


class TestDriver:
    def test_a_follow_up_is_a_new_sealed_bundle_and_keeps_its_parent(
            self, proof, runtime):
        agent = proof / "agent"
        proc = driver(runtime, agent, "experiment",
                      spec(agent, "late", channels=LATE,
                           references=["cadence.late.expected.mcap"]))

        assert proc.returncode == 1, proc.stdout + proc.stderr
        record = json.loads((agent / "experiments" / "late" / "record.json").read_text())
        assert record["parent"] == {
            "case": DELAY, "bundle": f"bundles/{DELAY}",
            "lock_sha256": _case(agent, DELAY)["expect_lock"],
            "manifest_sha256": sha256(agent / "bundles" / DELAY / f"{DELAY}.json")}
        assert record["intervention"]["cadence.radar"]["interceptors"]["follow_up"] is None
        assert record["contract"]["sha256"] == sha256(
            agent / "bundles" / DELAY / "cadence.contract.json")
        assert record["first_divergences"]["cadence.late"]["field"] == "radar_age_ns"
        assert record["result"][0]["determinism"]["verdict"] == "pass"
        assert [c["argv"][:3] for c in record["commands"]] == [
            ["sil", "manifest", "validate"], ["sil", "bundle", "seal"],
            ["sil", "bundle", "run"]]
        result = verifier.verify(agent, digests=proof / "evaluator" / "digests.json")
        assert [c for c in result["checks"] if c["status"] != "pass"
                and c["check"] == "original-unchanged"] == []

    def test_a_tool_call_counts_the_bytes_it_delivers(self, proof, runtime):
        agent = proof / "agent"
        proc = driver(runtime, agent, "tool", "--", "cat", "investigation.json")

        assert proc.returncode == 0
        entry = [json.loads(line) for line in
                 (agent / "ledger.jsonl").read_text().splitlines()][-1]
        assert entry["delivered_bytes"] == len(proc.stdout.encode()) == \
            (agent / "investigation.json").stat().st_size

    @pytest.mark.parametrize("argv", [
        ["sil", "bundle", "run", f"bundles/{DELAY}", "-o", "x"],
        ["sil", "check", "x.json"],
        ["sil-run", "x.json"],
        ["cat", "/etc/hosts"],
        ["cat", "../evaluator/cases.json"],
        ["rm", "investigation.json"],
    ])
    def test_a_command_outside_the_allowed_actions_is_refused(
            self, proof, runtime, argv):
        proc = driver(runtime, proof / "agent", "tool", "--", *argv)
        assert proc.returncode == 64, proc.stderr

    @pytest.mark.parametrize("fields", [
        {"parent": "late-private"},
        {"channels": {"cadence.command": {"latency_ns": 0}}},
        {"channels": {"cadence.radar": {"priority": 1}}},
        {"references": ["cadence.contract.json"]},
        {"contract": "loose.json"},
        {"hypothesis": ""},
    ])
    def test_a_spec_outside_the_allowed_parameters_is_refused(
            self, proof, runtime, fields):
        agent = proof / "agent"
        proc = driver(runtime, agent, "experiment", spec(agent, "refused", **fields))
        assert proc.returncode == 64, proc.stdout + proc.stderr
        assert not (agent / "experiments" / "refused").exists()

    def test_an_exhausted_budget_refuses_and_allows_an_incomplete_report(
            self, tmp_path, runtime, target):
        agent = prepared(tmp_path / "proof", runtime, target) / "agent"
        settings = json.loads((agent / "investigation.json").read_text())
        settings["budgets"]["runs"] = 1
        (agent / "investigation.json").write_text(json.dumps(settings))

        proc = driver(runtime, agent, "experiment", spec(agent, "over"))
        assert proc.returncode == 65, proc.stderr
        assert "budget exhausted: runs" in proc.stderr
        _write_reports(agent, {DELAY: _incomplete(DELAY)})
        result = verifier.verify(agent)
        assert _status(result, "outcome-facts", DELAY) == "pass"


def _case(agent: Path, name: str) -> dict:
    cases = json.loads((agent / "investigation.json").read_text())["cases"]
    return next(c for c in cases if c["name"] == name)


def _status(result: dict, check: str, subject: str) -> str:
    return next(c["status"] for c in result["checks"]
                if c["check"] == check and c["subject"] == subject)


def _report(case: str, outcome: str, **fields) -> dict:
    return {"sil_investigation_report": 1, "case": case, "outcome": outcome,
            "summary": "", "observations": [], "hypotheses": [], "experiments": [],
            "conclusions": [], "unresolved_alternatives": [], "missing_evidence": [],
            "regression_case": None, **fields}


def _incomplete(case: str) -> dict:
    return _report(case, "incomplete", missing_evidence=["a follow-up Run"])


def _write_reports(agent: Path, reports: dict) -> None:
    for case, report in reports.items():
        (agent / "reports" / f"{case}.json").write_text(json.dumps(report))


def canned_reports(agent: Path) -> dict:
    """True claims about the prepared cases and the `late` experiment."""
    runs = f"ci/cases/{DELAY}/runs/{DELAY}/run-1.mcap"
    late = "experiments/late/evidence/runs/late/run-1.mcap"
    timeout = "ci/cases/monitor-timeout/runs/monitor-timeout/run-1.mcap.failure.json"
    return {
        DELAY: _report(DELAY, "diagnosed", observations=[
            {"id": "O1", "artifact": runs, "sha256": sha256(agent / runs),
             "channel": "cadence.command", "t_ns": 110 * MS, "ordinal": 0,
             "field": "radar_age_ns", "value": 0},
            {"id": "O2", "artifact": late, "sha256": sha256(agent / late),
             "channel": "cadence.command", "t_ns": 10 * MS, "ordinal": 0,
             "field": "radar_age_ns", "value": 0}],
            hypotheses=[{"id": "H1", "statement": "age from arrival", "predicts": ""}],
            experiments=[{"name": "late", "tests": ["H1"], "result": "fail",
                          "observations": ["O2"]}],
            conclusions=[{"statement": "", "supported_by": ["O1", "late"]}],
            regression_case="late"),
        "nominal-control": _report("nominal-control", "no-failure", observations=[
            {"id": "O1", "artifact": "ci/summary.json",
             "sha256": sha256(agent / "ci" / "summary.json"),
             "pointer": "/cases/1/status", "value": "pass"}]),
        "monitor-timeout": _report("monitor-timeout", "execution-failure", observations=[
            {"id": "O1", "artifact": timeout, "sha256": sha256(agent / timeout),
             "pointer": "/category", "value": "timeout"}]),
        "late-private": _report("late-private", "inconclusive",
                                missing_evidence=["the Recording"]),
    }


@pytest.fixture(scope="module")
def reported(proof, runtime) -> Path:
    """The proof with the `late` experiment and the canned reports."""
    agent = proof / "agent"
    if not (agent / "experiments" / "late").exists():
        driver(runtime, agent, "experiment",
               spec(agent, "late", channels=LATE,
                    references=["cadence.late.expected.mcap"]))
    _write_reports(agent, canned_reports(agent))
    return agent


class TestVerify:
    def test_true_claims_pass_every_check(self, reported, proof):
        result = verifier.verify(reported, digests=proof / "evaluator" / "digests.json",
                                 reference=PROOF / "reference.json")

        assert result["passed"], [c for c in result["checks"] if c["status"] != "pass"]
        assert result["counts"]["unavailable"] == 0
        assert _status(result, "comparison-verdict", "late/cadence.late") == "pass"
        assert _status(result, "divergence-values", "late/cadence.late") == "pass"
        assert _status(result, "intervention-scope", "late") == "pass"

    def test_the_command_line_writes_the_result(self, reported, runtime, tmp_path):
        out = tmp_path / "verification.json"
        proc = subprocess.run(
            [str(runtime.python_bin / "python3"), str(PROOF / "verify.py"),
             str(reported), "-o", str(out)],
            env=installed_environment(runtime.python_bin.parent),
            capture_output=True, text=True)

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert json.loads(out.read_text())["passed"] is True

    @pytest.mark.parametrize("change, check, subject", [
        (lambda r: r[DELAY]["observations"][0].update(value=50 * MS),
         "observation-value", f"{DELAY}/O1"),
        (lambda r: r[DELAY]["observations"][0].update(sha256="0" * 64),
         "observation-digest", f"{DELAY}/O1"),
        (lambda r: r[DELAY]["observations"][0].update(t_ns=115 * MS),
         "observation-value", f"{DELAY}/O1"),
        (lambda r: r["monitor-timeout"].update(outcome="diagnosed"),
         "outcome-facts", "monitor-timeout"),
        (lambda r: r["nominal-control"].update(outcome="execution-failure"),
         "outcome-facts", "nominal-control"),
        (lambda r: r["late-private"].update(missing_evidence=[]),
         "outcome-facts", "late-private"),
        (lambda r: r[DELAY].update(regression_case=None),
         "outcome-facts", DELAY),
        (lambda r: r[DELAY]["conclusions"][0].update(supported_by=["O9"]),
         "report-references", DELAY),
        (lambda r: r["late-private"].update(outcome="diagnosed"),
         "expected-outcome", "late-private"),
    ])
    def test_a_false_claim_fails_its_check(self, reported, change, check, subject):
        reports = canned_reports(reported)
        change(reports)
        _write_reports(reported, reports)
        try:
            result = verifier.verify(reported, reference=PROOF / "reference.json")
        finally:
            _write_reports(reported, canned_reports(reported))

        assert not result["passed"]
        assert _status(result, check, subject) == "fail"

    def test_a_changed_original_fails(self, reported, proof, tmp_path):
        digests = json.loads((proof / "evaluator" / "digests.json").read_text())
        digests[f"bundles/{DELAY}/cadence.contract.json"] = "0" * 64
        (tmp_path / "digests.json").write_text(json.dumps(digests))

        result = verifier.verify(reported, digests=tmp_path / "digests.json")

        assert _status(result, "original-unchanged",
                       f"bundles/{DELAY}/cadence.contract.json") == "fail"

    def test_an_experiment_outside_its_parameters_fails(self, reported):
        manifest = reported / "experiments" / "late" / "bundle" / "late.json"
        original = manifest.read_text()
        doc = json.loads(original)
        doc["duration_ns"] += 10 * MS
        manifest.write_text(json.dumps(doc))
        try:
            result = verifier.verify(reported)
        finally:
            manifest.write_text(original)

        assert _status(result, "intervention-scope", "late") == "fail"


@pytest.mark.skipif(not TRIAL.is_dir(), reason="no retained trial")
class TestRetainedTrial:
    def test_the_retained_reports_hold_against_the_retained_evidence(self):
        result = verifier.verify(TRIAL, reference=PROOF / "reference.json")
        failed = [(c["check"], c["subject"]) for c in result["checks"]
                  if c["status"] == "fail"]
        # The agent quoted a line of source text, which the report format
        # cannot cite; assessment.json records it. Every other claim holds.
        assert failed == [("observation-value", "monitor-timeout/O7")]
        assert result["outcomes"] == {
            name: case["outcome"] for name, case in json.loads(
                (PROOF / "reference.json").read_text())["cases"].items()}

    def test_the_trial_experiments_repeat_with_the_same_values(
            self, tmp_path, runtime, target):
        """The trial ran on another host. Its Manifests name other paths, so
        the digests differ; every quoted Recording value must not."""
        agent = prepared(tmp_path / "proof", runtime, target) / "agent"
        experiments = [e["name"] for e in _ledger(TRIAL) if e["kind"] == "experiment"]
        for name in experiments:
            source = TRIAL / "experiments" / name / "spec.json"
            shutil.copyfile(source, agent / "specs" / f"{name}.json")
            proc = driver(runtime, agent, "experiment", f"specs/{name}.json")
            assert proc.returncode in (0, 1), proc.stdout + proc.stderr
            retained = json.loads((TRIAL / "experiments" / name / "record.json").read_text())
            repeated = json.loads((agent / "experiments" / name / "record.json").read_text())
            assert repeated["first_divergences"] == retained["first_divergences"], name
            assert [r["verdict"] for r in repeated["result"]] == \
                [r["verdict"] for r in retained["result"]], name
        quoted = 0
        for path in sorted((TRIAL / "reports").glob("*.json")):
            for o in json.loads(path.read_text())["observations"]:
                if "channel" not in o:
                    continue
                value = verifier.recorded_value(agent / o["artifact"], o["channel"],
                                                o["t_ns"], o.get("ordinal", 0),
                                                o["field"], o.get("index"))
                assert value == o["value"], (path.name, o["id"])
                quoted += 1
        assert quoted > 0


def _ledger(root: Path) -> list[dict]:
    return [json.loads(line) for line in (root / "ledger.jsonl").read_text().splitlines()]
