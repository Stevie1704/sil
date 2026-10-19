"""`sil-run-diagnostics --retain-run-directory PATH` (#315).

A CPU profile must capture the modules that a Run loads from its temporary
Run working directory (an extracted FMU, for example) before they are gone.
The diagnostics runner can move that directory to PATH at its normal removal
point, instead of deleting it. The ordinary runner has no such option, and
without it the diagnostics runner removes the directory as before.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from sil.manifest import Manifest

from conftest import ROOT

PROBE = ROOT / "tests" / "participants" / "working_directory_probe.py"


def probe_manifest(tmp_path) -> tuple:
    observer = tmp_path / "observer"
    observer.mkdir()
    manifest = Manifest(duration_ns=10_000_000)
    manifest.add_process("probe", command=[sys.executable, str(PROBE),
                                           str(observer)],
                         step_period_ns=10_000_000)
    path = tmp_path / "manifest.json"
    manifest.write(path)
    return path, observer


def run(runner, manifest, cwd, *extra):
    return subprocess.run([str(runner), str(manifest), "--no-recording",
                           *extra], cwd=cwd, capture_output=True, text=True)


def test_diagnostics_runner_moves_the_run_directory_to_the_retention_path(
        sil_run_diagnostics, tmp_path):
    manifest, observer = probe_manifest(tmp_path)
    retained = tmp_path / "retained"

    proc = run(sil_run_diagnostics, manifest, tmp_path,
               "--retain-run-directory", str(retained))

    assert proc.returncode == 0, proc.stderr
    participant_cwd = (observer / "cwd").read_text()
    run_directory = participant_cwd.rsplit("/", 1)[0]
    assert not list(tmp_path.glob(".sil-run-*"))
    relative = participant_cwd.removeprefix(run_directory + "/")
    owned = retained / relative / "participant-owned.txt"
    assert owned.read_text() == "remove me with the working directory"


def test_diagnostics_runner_without_the_option_removes_the_run_directory(
        sil_run_diagnostics, tmp_path):
    manifest, observer = probe_manifest(tmp_path)

    proc = run(sil_run_diagnostics, manifest, tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert not Path((observer / "cwd").read_text()).exists()
    assert not list(tmp_path.glob(".sil-run-*"))


def test_existing_retention_path_is_a_usage_error_before_the_run(
        sil_run_diagnostics, tmp_path):
    manifest, observer = probe_manifest(tmp_path)
    retained = tmp_path / "retained"
    retained.mkdir()

    proc = run(sil_run_diagnostics, manifest, tmp_path,
               "--retain-run-directory", str(retained))

    assert proc.returncode == 2
    assert "already exists" in proc.stderr
    assert not (observer / "cwd").exists()


def test_ordinary_runner_has_no_retention_option(sil_run, tmp_path):
    manifest, observer = probe_manifest(tmp_path)

    proc = run(sil_run, manifest, tmp_path, "--retain-run-directory",
               str(tmp_path / "retained"))

    assert proc.returncode == 2
    assert not (tmp_path / "retained").exists()
    assert not (observer / "cwd").exists()
