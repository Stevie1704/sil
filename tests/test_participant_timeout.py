"""Run-boundary tests for process-participant response deadlines (issue #114)."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT
from test_run_boundary import read_mcap
from toys import toy_manifest


TIMEOUT_PARTICIPANT = ROOT / "tests" / "participants" / "timeout.py"


def timeout_manifest(
    tmp_path: Path,
    mode: str,
    observer: Path | None = None,
    *,
    duration_ns: int = 10,
):
    manifest = toy_manifest(duration_ns=duration_ns)
    command = [sys.executable, str(TIMEOUT_PARTICIPANT), mode]
    if observer is not None:
        command.append(str(observer))
    manifest.add_process("hung", command=command, step_period_ns=1)
    return manifest.write(tmp_path / f"{mode}.json").path


def _run(sil_run: Path, manifest: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(sil_run), str(manifest), *args],
        capture_output=True,
        text=True,
        # With --no-recording the runner still writes a provenance side-car
        # beside its default output path. Run beside the manifest so it
        # cannot land in whatever directory pytest was started from.
        cwd=manifest.parent,
    )


def test_timeout_argument_is_not_part_of_manifest_or_recording(
    sil_run, tmp_path
):
    manifest = timeout_manifest(tmp_path, "ok")
    without = _run(sil_run, manifest, "-o", str(tmp_path / "without.mcap"))
    with_deadline = _run(
        sil_run,
        manifest,
        "--participant-timeout-ms",
        str(2**63 - 1),
        "-o",
        str(tmp_path / "with.mcap"),
    )

    assert without.returncode == with_deadline.returncode == 0
    assert without.stdout == with_deadline.stdout
    assert (tmp_path / "without.mcap").read_bytes() == (
        tmp_path / "with.mcap"
    ).read_bytes()


@pytest.mark.parametrize(
    "arguments",
    [
        ["--participant-timeout-ms"],
        ["--participant-timeout-ms", "0"],
        ["--participant-timeout-ms", "-1"],
        ["--participant-timeout-ms", "1.5"],
        ["--participant-timeout-ms", "1", "--participant-timeout-ms", "2"],
        ["--participant-timeout-ms", str(2**63)],
    ],
)
def test_invalid_timeout_is_rejected_before_spawn(
    sil_run, tmp_path, arguments
):
    marker = tmp_path / "spawned"
    manifest = timeout_manifest(tmp_path, "ok", marker)
    proc = _run(sil_run, manifest, *arguments, "--no-recording")

    assert proc.returncode == 2
    assert not marker.exists()


def test_timeout_during_initialization_is_a_run_failure(
    sil_run, tmp_path
):
    proc = _run(
        sil_run,
        timeout_manifest(tmp_path, "init"),
        "--participant-timeout-ms",
        "100",
        "--no-recording",
    )

    assert proc.returncode == 1
    assert "hung" in proc.stderr
    assert "initialization" in proc.stderr
    assert "timeout" in proc.stderr


def test_timeout_during_step_reports_virtual_time(sil_run, tmp_path):
    proc = _run(
        sil_run,
        timeout_manifest(tmp_path, "step"),
        "--participant-timeout-ms",
        "100",
        "--no-recording",
    )

    assert proc.returncode == 1
    assert "hung" in proc.stderr
    assert "step" in proc.stderr
    assert "virtual time 0 ns" in proc.stderr


def test_each_step_gets_a_fresh_deadline(sil_run, tmp_path):
    proc = _run(
        sil_run,
        timeout_manifest(tmp_path, "slow", duration_ns=3),
        "--participant-timeout-ms",
        "100",
        "--no-recording",
    )

    assert proc.returncode == 0, proc.stderr


def test_partial_response_does_not_extend_step_deadline(sil_run, tmp_path):
    proc = _run(
        sil_run,
        timeout_manifest(tmp_path, "partial"),
        "--participant-timeout-ms",
        "100",
        "--no-recording",
    )

    assert proc.returncode == 1
    assert "timeout" in proc.stderr
    assert "virtual time 0 ns" in proc.stderr


def test_timeout_reaps_sigterm_ignoring_child_and_removes_working_directory(
    sil_run, tmp_path
):
    observer = tmp_path / "observer.txt"
    proc = _run(
        sil_run,
        timeout_manifest(tmp_path, "ignore-term", observer),
        "--participant-timeout-ms",
        "100",
        "--no-recording",
    )

    assert proc.returncode == 1
    pid_text, working_directory = observer.read_text().splitlines()
    pid = int(pid_text)
    assert not Path(working_directory).exists()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_timeout_leaves_only_a_partial_recording_on_failure(sil_run, tmp_path):
    output = tmp_path / "partial.mcap"
    manifest = timeout_manifest(tmp_path, "step")
    proc = _run(
        sil_run,
        manifest,
        "--participant-timeout-ms",
        "100",
        "-o",
        str(output),
    )

    assert proc.returncode == 1
    assert output.exists()
    metadata, messages = read_mcap(output)
    assert metadata["manifest_hash"] == hashlib.sha256(
        manifest.read_bytes()
    ).hexdigest()
    assert messages == []
