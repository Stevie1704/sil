"""The machine-readable failure report of a failed Run (issue #272)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from conftest import ROOT
from toys import toy_manifest


FAILURE_MODES = ROOT / "tests" / "participants" / "failure_modes.py"

# A 10 ms Step period, so the failing Step at 30 ms is Step index 3.
STEP_PERIOD_NS = 10_000_000
FAIL_AT_NS = 30_000_000


def failing_manifest(tmp_path: Path, mode: str) -> Path:
    manifest = toy_manifest(duration_ns=100_000_000)
    manifest.add_process(
        "controller",
        command=[sys.executable, str(FAILURE_MODES), mode, str(FAIL_AT_NS)],
        step_period_ns=STEP_PERIOD_NS,
    )
    return manifest.write(tmp_path / f"{mode}.json").path


def run(sil_run: Path, manifest: Path, *args: str):
    out = manifest.parent / "out.mcap"
    proc = subprocess.run(
        [str(sil_run), str(manifest), "-o", str(out), *args],
        capture_output=True,
        text=True,
        cwd=manifest.parent,
    )
    return proc, Path(str(out) + ".failure.json")


def test_nonzero_exit_is_reported_with_status_time_and_stderr(
    sil_run, tmp_path
):
    proc, report_path = run(sil_run, failing_manifest(tmp_path, "exit"))

    assert proc.returncode == 1, proc.stderr
    assert (
        "participant 'controller' exited unexpectedly with status 3 "
        "during Step 3 at virtual time 30000000 ns"
    ) in proc.stderr
    # The child's stderr is still passed through.
    assert "stderr line 29 at t=30000000" in proc.stderr

    report = json.loads(report_path.read_text())
    assert report["schema_version"] == 1
    assert report["exit_code"] == 1
    assert report["category"] == "exit"
    assert report["participant"] == "controller"
    assert report["exit_status"] == 3
    assert report["signal"] is None
    assert report["virtual_time_ns"] == FAIL_AT_NS
    assert report["step_index"] == 3
    assert report["stderr_tail"] == [
        f"stderr line {index} at t=30000000" for index in range(10, 30)
    ]


def test_signal_is_reported_with_signal_number_and_time(sil_run, tmp_path):
    proc, report_path = run(sil_run, failing_manifest(tmp_path, "signal"))

    assert proc.returncode == 1, proc.stderr
    assert (
        "participant 'controller' was terminated unexpectedly by signal 9 "
        "during Step 3 at virtual time 30000000 ns"
    ) in proc.stderr
    report = json.loads(report_path.read_text())
    assert report["category"] == "exit"
    assert report["participant"] == "controller"
    assert report["exit_status"] is None
    assert report["signal"] == 9
    assert report["virtual_time_ns"] == FAIL_AT_NS
    assert report["step_index"] == 3


def test_protocol_violation_is_reported_as_protocol(sil_run, tmp_path):
    proc, report_path = run(sil_run, failing_manifest(tmp_path, "protocol"))

    assert proc.returncode == 1, proc.stderr
    report = json.loads(report_path.read_text())
    assert report["category"] == "protocol"
    assert report["participant"] == "controller"
    assert report["virtual_time_ns"] == FAIL_AT_NS
    assert report["step_index"] == 3
    assert "expected step_done" in report["message"]


def test_response_deadline_is_reported_as_timeout(sil_run, tmp_path):
    manifest = toy_manifest(duration_ns=100_000_000)
    manifest.add_process(
        "hung",
        command=[
            sys.executable,
            str(ROOT / "tests" / "participants" / "timeout.py"),
            "step",
        ],
        step_period_ns=STEP_PERIOD_NS,
    )
    path = manifest.write(tmp_path / "timeout.json").path

    proc, report_path = run(
        sil_run, path, "--participant-timeout-ms", "200"
    )

    assert proc.returncode == 1, proc.stderr
    report = json.loads(report_path.read_text())
    assert report["category"] == "timeout"
    assert report["participant"] == "hung"
    # The first Step, at virtual time 0, never gets an answer.
    assert report["virtual_time_ns"] == 0
    assert report["step_index"] == 0
    assert report["message"] == (
        "participant 'hung': timeout waiting for step_done response during "
        "Step at virtual time 0 ns"
    )


def test_manifest_error_is_reported_with_exit_code_2(sil_run, tmp_path):
    manifest = tmp_path / "broken.json"
    manifest.write_text("{")

    proc, report_path = run(sil_run, manifest)

    assert proc.returncode == 2, proc.stderr
    report = json.loads(report_path.read_text())
    assert report["exit_code"] == 2
    assert report["category"] == "manifest"
    assert report["participant"] is None
    assert report["virtual_time_ns"] is None


def test_successful_run_writes_no_report_and_removes_a_stale_one(
    sil_run, tmp_path
):
    manifest = toy_manifest(duration_ns=100_000_000)
    manifest.add_process(
        "controller",
        # Fails only after the Run has ended, so every Step succeeds.
        command=[sys.executable, str(FAILURE_MODES), "exit", "1000000000"],
        step_period_ns=STEP_PERIOD_NS,
    )
    path = manifest.write(tmp_path / "ok.json").path
    stale = tmp_path / "out.mcap.failure.json"
    stale.write_text("{}")

    proc, report_path = run(sil_run, path)

    assert proc.returncode == 0, proc.stderr
    assert report_path == stale
    assert not report_path.exists()
