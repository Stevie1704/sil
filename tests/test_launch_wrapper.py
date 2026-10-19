"""`sil-run-diagnostics --launch-wrapper NAME PATH` (#317).

A heap profile of one Process participant needs an adapter in that one
process (Heaptrack's preload library), but the Manifest must stay as it is.
The diagnostics runner can start the named Process participant through a
wrapper executable: it executes `PATH COMMAND...` instead of `COMMAND...`,
with the same working directory, pipes and environment. The wrapper must
exec the command, so the participant keeps the PID that the runner forked.
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
from test_run_diagnostics import MS, mixed_period_manifest

PID_MARKER = ROOT / "tests" / "participants" / "pid_marker.py"


def wrapper(tmp_path: Path) -> tuple[Path, Path]:
    """A wrapper that logs what it got, then execs its arguments."""
    log = tmp_path / "wrapper-log"
    log.mkdir()
    script = tmp_path / "wrap.sh"
    script.write_text(
        "#!/bin/sh\n"
        f"log='{log}'\n"
        'printf "%s" "$$" > "$log/pid"\n'
        'pwd > "$log/cwd"\n'
        'for a in "$@"; do printf "%s\\n" "$a"; done > "$log/argv"\n'
        'printf "%s" "${SIL_WRAPPER_PROBE-unset}" > "$log/env"\n'
        'exec "$@"\n')
    script.chmod(0o755)
    return script, log


def two_marker_manifest(tmp_path: Path) -> tuple[Path, Path]:
    markers = tmp_path / "markers"
    markers.mkdir()
    m = Manifest(duration_ns=20 * MS)
    for name in ("wrapped", "sibling"):
        m.add_process(name, command=[sys.executable, str(PID_MARKER),
                                     str(markers / name), "ok"],
                      step_period_ns=10 * MS)
    return m.write(tmp_path / "markers.json").path, markers


def run(runner, manifest, cwd, *extra, env=None):
    return subprocess.run([str(runner), str(manifest), *extra], cwd=cwd,
                          capture_output=True, text=True,
                          env={**os.environ, **(env or {})})


def test_the_named_participant_starts_through_the_wrapper_with_its_pid(
        sil_run_diagnostics, tmp_path):
    manifest, markers = two_marker_manifest(tmp_path)
    script, log = wrapper(tmp_path)

    proc = run(sil_run_diagnostics, manifest, tmp_path, "--no-recording",
               "--launch-wrapper", "wrapped", str(script),
               env={"SIL_WRAPPER_PROBE": "inherited"})

    assert proc.returncode == 0, proc.stderr
    # exec keeps the PID: the participant is the process the runner forked.
    assert (log / "pid").read_text() == (markers / "wrapped").read_text()
    argv = (log / "argv").read_text().splitlines()
    assert argv == [sys.executable, str(PID_MARKER),
                    str(markers / "wrapped"), "ok"]
    # The participant's own working directory and the runner's environment.
    cwd = Path((log / "cwd").read_text().strip())
    assert cwd.name == "participant-" + b"wrapped".hex()
    assert cwd.parent.name.startswith(".sil-run-")
    assert (log / "env").read_text() == "inherited"


def test_other_participants_do_not_start_through_the_wrapper(
        sil_run_diagnostics, tmp_path):
    manifest, markers = two_marker_manifest(tmp_path)
    script, log = wrapper(tmp_path)

    proc = run(sil_run_diagnostics, manifest, tmp_path, "--no-recording",
               "--launch-wrapper", "wrapped", str(script))

    assert proc.returncode == 0, proc.stderr
    assert (markers / "sibling").is_file()
    # The wrapper ran once, for the named participant only.
    assert (log / "pid").read_text() != (markers / "sibling").read_text()
    assert (log / "argv").read_text().count(str(PID_MARKER)) == 1


def test_the_wrapped_run_records_the_same_bytes_and_manifest_hash(
        sil_run, sil_run_diagnostics, tmp_path):
    manifest = mixed_period_manifest(tmp_path)
    script, _ = wrapper(tmp_path)

    ordinary = run(sil_run, manifest, tmp_path, "-o",
                   str(tmp_path / "ordinary.mcap"))
    wrapped = run(sil_run_diagnostics, manifest, tmp_path, "-o",
                  str(tmp_path / "wrapped.mcap"), "--launch-wrapper",
                  "pyecho", str(script))

    assert ordinary.returncode == 0, ordinary.stderr
    assert wrapped.returncode == 0, wrapped.stderr
    assert (tmp_path / "wrapped.mcap").read_bytes() == \
        (tmp_path / "ordinary.mcap").read_bytes()
    assert ordinary.stdout == wrapped.stdout  # manifest_hash <hex>
    report = json.loads(
        (tmp_path / "wrapped.mcap.diagnostics.json").read_text())
    assert report["run"]["status"] == "succeeded"


def test_a_wrapper_for_an_unknown_participant_is_a_usage_error(
        sil_run_diagnostics, tmp_path):
    manifest, markers = two_marker_manifest(tmp_path)
    script, log = wrapper(tmp_path)

    proc = run(sil_run_diagnostics, manifest, tmp_path, "--no-recording",
               "--launch-wrapper", "nobody", str(script))

    assert proc.returncode == 2
    assert "--launch-wrapper: no Process participant 'nobody'" in proc.stderr
    assert not list(markers.iterdir())


def test_a_wrapper_for_a_native_participant_is_a_usage_error(
        sil_run_diagnostics, tmp_path):
    manifest = mixed_period_manifest(tmp_path)
    script, log = wrapper(tmp_path)

    proc = run(sil_run_diagnostics, manifest, tmp_path, "--no-recording",
               "--launch-wrapper", "multi", str(script))

    assert proc.returncode == 2
    assert "--launch-wrapper: no Process participant 'multi'" in proc.stderr
    assert not (log / "pid").exists()


def test_a_missing_wrapper_is_a_usage_error_before_the_run(
        sil_run_diagnostics, tmp_path):
    manifest, markers = two_marker_manifest(tmp_path)

    proc = run(sil_run_diagnostics, manifest, tmp_path, "--no-recording",
               "--launch-wrapper", "wrapped", str(tmp_path / "missing.sh"))

    assert proc.returncode == 2
    assert "is not an executable file" in proc.stderr
    assert not list(markers.iterdir())


def test_the_ordinary_runner_has_no_launch_wrapper(sil_run, tmp_path):
    manifest, markers = two_marker_manifest(tmp_path)
    script, log = wrapper(tmp_path)

    proc = run(sil_run, manifest, tmp_path, "--no-recording",
               "--launch-wrapper", "wrapped", str(script))

    assert proc.returncode == 2
    assert "usage: sil-run" in proc.stderr
    assert not list(markers.iterdir())
    assert not (log / "pid").exists()
