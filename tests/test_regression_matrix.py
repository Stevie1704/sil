"""An offline regression matrix over sealed bundles (issue #202).

The bundles are prepared from the source tree and sealed once per module.
`sil bundle matrix` then runs them from the staged installation, as a CI job does:
each case in its own evidence directory, with bounded concurrency, a
whole-case wall-clock guard, and JSON, JUnit and readable summaries.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest
from conftest import ROOT, load_module

from sil import matrix

prepare = load_module("matrix_prepare", ROOT / "examples" / "bundle" / "prepare.py")


@pytest.fixture(scope="module")
def runtime(installed_python: Path, staged_prefix: Path):
    return prepare.Runtime(installed_python / "bin", staged_prefix / "bin")


def _env(runtime) -> dict[str, str]:
    return {"PATH": f"{runtime.python_bin}:{runtime.sil_bin}", "PYTHONNOUSERSITE": "1"}


def seal(runtime, root: Path) -> str:
    """Seal `root` and return the lock digest `seal` printed."""
    proc = subprocess.run([str(runtime.python_bin / "sil"), "bundle", "seal", str(root)],
                          env=_env(runtime), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.split("lock sha256 ")[1].strip()


def _library(root: Path, runtime, binary: Path, timeout_ms: int | None) -> Path:
    declaration = prepare.library(root, runtime, binary)
    document = json.loads(declaration.read_text())
    (run,) = document["runs"]
    if timeout_ms is None:
        del run["participant_timeout_ms"]
    else:
        run["participant_timeout_ms"] = timeout_ms
    declaration.write_text(json.dumps(document))
    return root


@pytest.fixture(scope="module")
def bundles(tmp_path_factory, runtime, build_dir, sil_run) -> dict[str, tuple[Path, str]]:
    """Every bundle the matrices use, sealed once: name -> (root, lock)."""
    base = tmp_path_factory.mktemp("bundles")
    fmu = ROOT / "tests" / "fixtures" / "reference-fmus" / "3.0" / "Feedthrough.fmu"
    roots = {
        "library": prepare.library(base / "library", runtime,
                                   build_dir / "speed_filter.so").parent,
        "fmu-replay": prepare.fmu_replay(base / "fmu-replay", runtime,
                                         build_dir / "EgoMotion.so").parent,
        "coupling": prepare.coupling(base / "coupling", runtime, fmu, sil_run).parent,
        "failed-kpi": prepare.library(base / "failed-kpi", runtime,
                                      build_dir / "speed_filter_defect.so").parent,
        "invalid": prepare.library(base / "invalid", runtime,
                                   build_dir / "speed_filter.so").parent,
        # The configured response deadline turns the stall into a Run failure.
        "stalled": _library(base / "stalled", runtime,
                            build_dir / "speed_filter_hang.so", 500),
        # No deadline: only the whole-case guard ends it.
        "wedged": _library(base / "wedged", runtime,
                           build_dir / "speed_filter_hang.so", None),
    }
    sealed = {name: (root, seal(runtime, root)) for name, root in roots.items()}
    csv = roots["invalid"] / "signals.csv"
    csv.write_text(csv.read_text().replace("8", "9", 1))
    return sealed


def case(bundles, name: str, bundle: str | None = None, **fields) -> dict:
    root, lock = bundles[bundle or name]
    return {"name": name, "bundle": str(root), "expect_lock": lock,
            "timeout_s": 120, **fields}


def write_cases(path: Path, cases: list[dict]) -> Path:
    path.write_text(json.dumps({"sil_matrix": 1, "cases": cases}))
    return path


def sil_matrix(runtime, *args, **popen) -> subprocess.CompletedProcess:
    return subprocess.run([str(runtime.python_bin / "sil"), "bundle", "matrix", *map(str, args)],
                          env=_env(runtime), capture_output=True, text=True, **popen)


def summary(out: Path) -> dict:
    return json.loads((out / "summary.json").read_text())


def by_name(result: dict) -> dict[str, dict]:
    return {c["name"]: c for c in result["cases"]}


def survivors(root: Path) -> list[str]:
    """Processes whose command line still names a file in the bundle."""
    listing = subprocess.run(["ps", "-axo", "pid=,command="],
                             capture_output=True, text=True).stdout
    return [line for line in listing.splitlines() if str(root) in line]


def _wait_until_gone(root: Path) -> list[str]:
    deadline = time.monotonic() + 5
    while (found := survivors(root)) and time.monotonic() < deadline:
        time.sleep(0.05)
    return found


def test_a_mixed_matrix_reports_every_case_and_fails_on_required_cases(
        bundles, runtime, tmp_path):
    cases = write_cases(tmp_path / "cases.json", [
        case(bundles, "library"),
        case(bundles, "fmu-replay"),
        case(bundles, "coupling"),
        case(bundles, "failed-kpi"),
        case(bundles, "invalid"),
        case(bundles, "stalled"),
        case(bundles, "wedged", timeout_s=3),
        case(bundles, "optional-kpi", "failed-kpi", required=False),
    ])
    out = tmp_path / "out"

    proc = sil_matrix(runtime, cases, "-o", out, "--jobs", "3")

    assert proc.returncode == 1, proc.stdout + proc.stderr
    result = summary(out)
    assert result["verdict"] == "fail"
    cases = by_name(result)
    assert [c["name"] for c in result["cases"]] == [
        "library", "fmu-replay", "coupling", "failed-kpi", "invalid", "stalled",
        "wedged", "optional-kpi"]
    assert {n: c["status"] for n, c in cases.items()} == {
        "library": "pass", "fmu-replay": "pass", "coupling": "pass",
        "failed-kpi": "behavioral-failure", "invalid": "manifest-error",
        "stalled": "behavioral-failure", "wedged": "timeout",
        "optional-kpi": "behavioral-failure",
    }
    assert cases["failed-kpi"]["bundle_exit_code"] == 1
    assert cases["failed-kpi"]["runs"] == [
        {"name": "library", "status": "behavioral-failure", "exit_code": 1,
         "recording_sha256": cases["failed-kpi"]["runs"][0]["recording_sha256"]}]
    assert cases["invalid"]["bundle_exit_code"] == 2
    assert "altered artifact signals.csv" in cases["invalid"]["reason"]
    assert cases["stalled"]["runs"][0]["exit_code"] == 1
    # The terminated sil bundle keeps the runner code of the interrupted Run.
    assert cases["wedged"]["bundle_exit_code"] == 1
    assert cases["wedged"]["runs"][0]["exit_code"] == 1
    assert "3 s" in cases["wedged"]["reason"]
    assert "run interrupted" in (
        out / "cases" / "wedged" / "runs" / "library" / "run-1.log").read_text()
    assert cases["optional-kpi"]["required"] is False
    for name, entry in cases.items():
        assert entry["evidence"] == f"cases/{name}"
        assert (out / entry["log"]).is_file()
        assert (out / entry["evidence"] / "summary.json").is_file()
    assert (out / "cases" / "coupling" / "runs" / "substituted" / "run-1.mcap").is_file()
    assert survivors(bundles["wedged"][0]) == []
    assert str(tmp_path) not in json.dumps(result)

    suite = ElementTree.parse(out / "junit.xml").getroot().find("testsuite")
    assert suite.get("tests") == "8"
    assert suite.get("failures") == "3"
    assert suite.get("errors") == "2"
    outcomes = {tc.get("name"): [child.tag for child in tc] for tc in suite}
    assert outcomes["library"] == ["system-out"]
    assert outcomes["wedged"] == ["error", "system-out"]
    assert "wedged" in proc.stdout and "timeout" in proc.stdout


def test_order_and_parallelism_do_not_change_a_case_recording(
        bundles, runtime, tmp_path):
    names = ["library", "fmu-replay", "coupling"]
    serial = write_cases(tmp_path / "serial.json", [case(bundles, n) for n in names])
    parallel = write_cases(tmp_path / "parallel.json",
                           [case(bundles, n) for n in reversed(names)])

    assert sil_matrix(runtime, serial, "-o", tmp_path / "a", "--jobs", "1").returncode == 0
    assert sil_matrix(runtime, parallel, "-o", tmp_path / "b", "--jobs", "3").returncode == 0

    first, second = by_name(summary(tmp_path / "a")), by_name(summary(tmp_path / "b"))
    for name in names:
        assert first[name]["runs"] == second[name]["runs"]
        assert all(run["recording_sha256"] for run in first[name]["runs"])
        assert first[name]["identity_sha256"] == second[name]["identity_sha256"]
        observations = first[name]["observations"]
        assert observations["duration_s"] > 0
        assert observations["max_rss_kib"] > 0
        identity = json.dumps(first[name]["identity"])
        assert "duration" not in identity and "rss" not in identity


def test_fail_fast_starts_no_case_after_a_required_failure(bundles, runtime, tmp_path):
    cases = write_cases(tmp_path / "cases.json", [
        case(bundles, "failed-kpi"), case(bundles, "library"),
        case(bundles, "fmu-replay")])

    proc = sil_matrix(runtime, cases, "-o", tmp_path / "out", "--jobs", "1",
                      "--fail-fast")

    assert proc.returncode == 1
    cases = by_name(summary(tmp_path / "out"))
    assert cases["failed-kpi"]["status"] == "behavioral-failure"
    for name in ("library", "fmu-replay"):
        assert cases[name]["status"] == "skipped"
        assert cases[name]["reason"] == "fail-fast"
        assert not (tmp_path / "out" / "cases" / name).exists()
    suite = ElementTree.parse(tmp_path / "out" / "junit.xml").getroot().find("testsuite")
    assert suite.get("skipped") == "2"


def test_an_optional_failure_leaves_the_matrix_passing(bundles, runtime, tmp_path):
    cases = write_cases(tmp_path / "cases.json", [
        case(bundles, "library"), case(bundles, "failed-kpi", required=False)])

    proc = sil_matrix(runtime, cases, "-o", tmp_path / "out")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert summary(tmp_path / "out")["verdict"] == "pass"


def test_an_interrupted_matrix_terminates_its_run_process_trees(
        bundles, runtime, tmp_path):
    cases = write_cases(tmp_path / "cases.json", [
        case(bundles, "wedged"), case(bundles, "library")])
    out = tmp_path / "out"
    matrix_proc = subprocess.Popen(
        [str(runtime.python_bin / "sil"), "bundle", "matrix", str(cases), "-o", str(out),
         "--jobs", "1"],
        env=_env(runtime), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 30
    while not survivors(bundles["wedged"][0]) and time.monotonic() < deadline:
        time.sleep(0.05)
    # Let the wedged Run reach its stalled Step.
    time.sleep(1)

    matrix_proc.send_signal(signal.SIGINT)
    matrix_proc.communicate(timeout=30)

    assert matrix_proc.returncode == 130
    result = summary(out)
    assert result["verdict"] == "interrupted"
    cases = by_name(result)
    assert cases["wedged"]["status"] == "skipped"
    assert cases["wedged"]["reason"].startswith("interrupted")
    assert cases["wedged"]["runs"][0]["exit_code"] == 1
    assert cases["library"]["evidence"] is None
    assert cases["library"]["status"] == "skipped"
    assert _wait_until_gone(bundles["wedged"][0]) == []


@pytest.mark.parametrize("problem, cases, expected", [
    ("duplicate", [{"name": "a"}, {"name": "A"}], "names case 'A' twice"),
    ("no guard", [{"name": "a", "timeout_s": None}], "timeout_s"),
    ("no lock", [{"name": "a", "expect_lock": None}], "expect_lock"),
    ("bad name", [{"name": "../a"}], "must be letters"),
    ("infinite guard", [{"name": "a", "timeout_s": 1e999}], "timeout_s"),
])
def test_a_malformed_case_list_is_refused(bundles, runtime, tmp_path,
                                          problem, cases, expected):
    entries = []
    for fields in cases:
        entry = {**case(bundles, "library"), **fields}
        entries.append({k: v for k, v in entry.items() if v is not None})
    listing = write_cases(tmp_path / "cases.json", entries)

    proc = sil_matrix(runtime, listing, "-o", tmp_path / "out")

    assert proc.returncode == 2
    assert expected in proc.stderr
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("where", ["used", "inside"])
def test_the_output_directory_must_be_new_and_outside_every_bundle(
        bundles, runtime, tmp_path, where):
    listing = write_cases(tmp_path / "cases.json", [case(bundles, "library")])
    if where == "used":
        out = tmp_path / "out"
        out.mkdir()
        (out / "old.json").write_text("{}")
    else:
        out = bundles["library"][0] / "out"

    proc = sil_matrix(runtime, listing, "-o", out)

    assert proc.returncode == 2
    assert "output directory" in proc.stderr
    assert not (bundles["library"][0] / "out").exists()


# The classification seam: a bundle summary and exit code in, a status out.

def _bundle_summary(**run) -> dict:
    entry = {"name": "r", "exit_code": 0, "recording_sha256": "a",
             "determinism": None, "comparisons": {}, **run}
    return {"verdict": "fail", "bundle_unchanged": True, "runs": [entry]}


@pytest.mark.parametrize("run, status", [
    ({"exit_code": 2}, "manifest-error"),
    ({"exit_code": None}, "manifest-error"),
    ({"exit_code": 1}, "behavioral-failure"),
    ({"determinism": {"verdict": "fail", "second_exit_code": 0,
                      "recording_sha256": ["a", "b"]}}, "determinism-violation"),
    ({"determinism": {"verdict": "fail", "second_exit_code": 1,
                      "recording_sha256": ["a", None]}}, "behavioral-failure"),
    ({"comparisons": {"ref": {"verdict": "fail"}}}, "behavioral-failure"),
])
def test_a_run_result_maps_to_one_status(run, status):
    assert matrix.classify(1, _bundle_summary(**run))[0] == status


def test_the_most_severe_run_decides_the_case():
    result = _bundle_summary(exit_code=1)
    result["runs"].append({**result["runs"][0], "name": "s", "exit_code": 0,
                           "determinism": {"verdict": "fail", "second_exit_code": 0,
                                           "recording_sha256": ["a", "b"]}})
    status, reason = matrix.classify(1, result)
    assert status == "determinism-violation"
    assert "run s" in reason


def test_a_bundle_without_a_summary_is_a_manifest_error():
    assert matrix.classify(1, None)[0] == "manifest-error"


def test_a_changed_bundle_fails_a_passing_case():
    result = _bundle_summary()
    result["bundle_unchanged"] = False
    assert matrix.classify(1, result)[0] == "behavioral-failure"


def test_a_case_that_cannot_start_is_reported_with_the_others(
        tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError(8, "Exec format error")

    monkeypatch.setattr(matrix.subprocess, "Popen", refuse)
    monkeypatch.setattr(matrix.signal, "signal", lambda *args: None)
    listing = write_cases(tmp_path / "cases.json", [
        {"name": n, "bundle": str(tmp_path / n), "expect_lock": "0" * 64,
         "timeout_s": 5} for n in ("a", "b")])
    out = tmp_path / "out"

    assert matrix.main([str(listing), "-o", str(out)]) == 1

    for entry in summary(out)["cases"]:
        assert entry["status"] == "manifest-error"
        assert "cannot start sil bundle" in entry["reason"]
    assert (out / "junit.xml").is_file()


# A case process tree whose Process participant leads its own group, as
# sil-run's participants do, and ignores SIGTERM.
_TREE = """
import os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, "-c",
    "import os, signal, time; os.setpgid(0, 0); "
    "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"])
open(sys.argv[1], "w").write(str(child.pid))
if sys.argv[2] == "stay":
    time.sleep(60)
"""


def _tree(tmp_path, mode: str) -> tuple[subprocess.Popen, int]:
    marker = tmp_path / "participant.pid"
    proc = subprocess.Popen([sys.executable, "-c", _TREE, str(marker), mode],
                            start_new_session=True)
    deadline = time.monotonic() + 10
    while not (marker.exists() and marker.read_text()) and time.monotonic() < deadline:
        time.sleep(0.02)
    return proc, int(marker.read_text())


def _gone(pid: int) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


def test_termination_reaches_participant_groups_the_runner_left(
        tmp_path, monkeypatch):
    monkeypatch.setattr(matrix, "TERMINATION_GRACE_S", 0.5)
    proc, participant = _tree(tmp_path, "stay")

    code, _, ended = matrix._wait(proc, time.monotonic(), threading.Event())

    assert ended == "timeout"
    assert code is not None
    assert _gone(participant)


def test_a_finished_case_leaves_no_process_behind(tmp_path):
    proc, participant = _tree(tmp_path, "exit")

    code, _, ended = matrix._wait(proc, time.monotonic() + 30, threading.Event())

    assert (code, ended) == (0, "exited")
    assert _gone(participant)
