"""`sil-run-diagnostics --hold-deadline NAME` (#367).

A debugger stop in one Process participant can last longer than any finite
Response deadline. The diagnostics runner can remove the deadline of that
one named participant; every other Process participant keeps
`--participant-timeout-ms`, so a real hang elsewhere still fails the Run.
The ordinary runner has no such option.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from sil.manifest import Manifest

from conftest import ROOT

TIMEOUT_PARTICIPANT = ROOT / "tests" / "participants" / "timeout.py"
MS = 1_000_000
# The "hold" participant's first Step takes 1.5 s, past this deadline.
DEADLINE_MS = "1000"


def manifest(tmp_path: Path, held_mode: str, sibling_mode: str) -> Path:
    m = Manifest(duration_ns=20 * MS)
    for name, mode in (("held", held_mode), ("sibling", sibling_mode)):
        m.add_process(name, command=[sys.executable, str(TIMEOUT_PARTICIPANT),
                                     mode], step_period_ns=10 * MS)
    return m.write(tmp_path / "hold.json").path


def run(runner, path, cwd, *extra):
    return subprocess.run([str(runner), str(path), "--participant-timeout-ms",
                           DEADLINE_MS, *extra], cwd=cwd, capture_output=True,
                          text=True, env=dict(os.environ), timeout=60)


def report(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "out.mcap.diagnostics.json").read_text())


def test_the_held_participant_completes_past_the_deadline(
        sil_run_diagnostics, tmp_path):
    path = manifest(tmp_path, "hold", "ok")

    proc = run(sil_run_diagnostics, path, tmp_path, "-o",
               str(tmp_path / "out.mcap"), "--hold-deadline", "held")

    assert proc.returncode == 0, proc.stderr
    configuration = report(tmp_path)["configuration"]
    assert configuration["held_deadline_participant"] == "held"
    assert configuration["participant_timeout_ms"] == 1000


def test_without_the_hold_the_same_step_misses_its_deadline(
        sil_run_diagnostics, tmp_path):
    path = manifest(tmp_path, "hold", "ok")

    proc = run(sil_run_diagnostics, path, tmp_path, "-o",
               str(tmp_path / "out.mcap"))

    assert proc.returncode == 1
    run_report = report(tmp_path)
    assert run_report["run"]["failure"]["category"] == "timeout"
    assert run_report["run"]["failure"]["participant"] == "held"
    assert run_report["configuration"]["held_deadline_participant"] is None


def test_a_hang_in_a_participant_that_is_not_held_still_fails_the_run(
        sil_run_diagnostics, tmp_path):
    path = manifest(tmp_path, "hold", "step")

    proc = run(sil_run_diagnostics, path, tmp_path, "-o",
               str(tmp_path / "out.mcap"), "--hold-deadline", "held")

    assert proc.returncode == 1
    failure = report(tmp_path)["run"]["failure"]
    assert failure["category"] == "timeout"
    assert failure["participant"] == "sibling"
    assert failure["virtual_time_ns"] == 0


def test_the_held_run_records_the_same_bytes_as_an_ordinary_run(
        sil_run, sil_run_diagnostics, tmp_path):
    path = manifest(tmp_path, "hold", "ok")

    ordinary = subprocess.run([str(sil_run), str(path), "-o",
                               str(tmp_path / "ordinary.mcap")], cwd=tmp_path,
                              capture_output=True, text=True, timeout=60)
    held = run(sil_run_diagnostics, path, tmp_path, "-o",
               str(tmp_path / "out.mcap"), "--hold-deadline", "held")

    assert ordinary.returncode == 0, ordinary.stderr
    assert held.returncode == 0, held.stderr
    assert (tmp_path / "out.mcap").read_bytes() == \
        (tmp_path / "ordinary.mcap").read_bytes()
    assert ordinary.stdout == held.stdout  # manifest_hash <hex>


def test_the_ordinary_runner_has_no_held_deadline(sil_run, tmp_path):
    path = manifest(tmp_path, "ok", "ok")

    proc = run(sil_run, path, tmp_path, "--no-recording",
               "--hold-deadline", "held")

    assert proc.returncode == 2
    assert "usage: sil-run " in proc.stderr


def test_a_held_deadline_must_name_a_process_participant(
        sil_run_diagnostics, tmp_path):
    path = manifest(tmp_path, "ok", "ok")

    proc = run(sil_run_diagnostics, path, tmp_path, "--no-recording",
               "--hold-deadline", "missing")

    assert proc.returncode == 2
    assert "--hold-deadline: no Process participant 'missing'" in proc.stderr


def test_a_held_deadline_is_given_once(sil_run_diagnostics, tmp_path):
    path = manifest(tmp_path, "ok", "ok")

    proc = run(sil_run_diagnostics, path, tmp_path, "--no-recording",
               "--hold-deadline", "held", "--hold-deadline", "sibling")

    assert proc.returncode == 2
    assert "usage: sil-run-diagnostics " in proc.stderr
