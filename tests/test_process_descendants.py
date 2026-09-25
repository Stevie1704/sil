"""Process-group lifetime at the Run boundary (issue #121)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sil.manifest import Manifest

from conftest import ROOT


DESCENDANT_SCHEMAS = {
    "test.Byte": {"fields": [{"name": "value", "type": "u8"}]}
}


@pytest.fixture
def descendant_case(tmp_path):
    regions = tmp_path / "regions"
    regions.mkdir()
    return regions, tmp_path / "observer.json"


def descendant_manifest(mode: str, observer: Path) -> Manifest:
    manifest = Manifest(duration_ns=10_000_000)
    manifest.add_schemas(DESCENDANT_SCHEMAS)
    # The fixture maps and writes this Arena during a successful Step, so the
    # cleanup assertion proves that an in-use Arena is released as well.
    manifest.add_channel("payload", schema="test.Byte", transport="shm")
    manifest.add_process(
        "descendant",
        command=[
            sys.executable,
            str(ROOT / "tests" / "participants" / "timeout.py"),
            mode,
            str(observer),
        ],
        step_period_ns=10_000_000,
        publishes=["payload"],
    )
    return manifest


def _wait_for_process_exit(pid: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    raise AssertionError(f"descendant process {pid} survived the Run")


def assert_cleanup(
    proc, observer: Path, regions: Path, expected_code: int, stderr: str = ""
) -> None:
    assert proc.returncode == expected_code, stderr or proc.stderr
    info = json.loads(observer.read_text())
    assert info["participant_marker"]
    assert info["descendant_marker"]
    _wait_for_process_exit(int(info["pid"]))
    assert not Path(info["cwd"]).exists()
    assert not any(
        path.name.startswith(("sil_arena_", "sil_clock_"))
        for path in regions.iterdir()
    )


def _scoped_env(regions: Path) -> dict[str, str]:
    return {**os.environ, "TMPDIR": str(regions)}


def _wait_for_observer(observer: Path) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            if "participant_marker" in json.loads(observer.read_text()):
                return
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        time.sleep(0.01)
    raise AssertionError("descendant fixture did not finish startup")


def _run(run_sil, mode: str, case):
    regions, observer = case
    manifest = descendant_manifest(mode, observer).write(
        observer.parent / "run.json"
    ).path
    return run_sil(manifest, env=_scoped_env(regions))


def test_successful_run_reaps_process_participant_descendants(run_sil, descendant_case):
    regions, observer = descendant_case
    proc = _run(run_sil, "descendant-success", descendant_case)
    assert_cleanup(proc, observer, regions, expected_code=0)


def test_failed_run_reaps_process_participant_descendants(run_sil, descendant_case):
    regions, observer = descendant_case
    proc = _run(run_sil, "descendant-failure", descendant_case)
    assert_cleanup(proc, observer, regions, expected_code=1)


def test_response_deadline_reaps_process_participant_descendants(sil_run, descendant_case):
    regions, observer = descendant_case
    manifest = descendant_manifest("descendant-timeout", observer).write(
        observer.parent / "run.json"
    ).path
    # The fixture is deliberately wedged; the response deadline is supplied
    # through the runner's boundary rather than the Manifest builder helper.
    proc = subprocess.run(
        [
            str(sil_run),
            str(manifest),
            "--participant-timeout-ms",
            "100",
            "--no-recording",
        ],
        capture_output=True,
        text=True,
        # --no-recording still writes a provenance side-car beside the
        # default output path; keep it out of the repository root.
        cwd=manifest.parent,
        env=_scoped_env(regions),
    )

    assert "timeout" in proc.stderr
    assert_cleanup(proc, observer, regions, expected_code=1)


def test_sigterm_ignoring_descendant_is_killed_and_reaped(run_sil, descendant_case):
    regions, observer = descendant_case
    proc = _run(run_sil, "descendant-ignore-term", descendant_case)
    assert_cleanup(proc, observer, regions, expected_code=0)


@pytest.mark.parametrize("termination_signal", [signal.SIGINT, signal.SIGHUP])
def test_terminal_signal_reaps_process_participant_descendants(
    sil_run, descendant_case, termination_signal
):
    regions, observer = descendant_case
    manifest = descendant_manifest("descendant-timeout", observer).write(
        observer.parent / "run.json"
    ).path
    runner = subprocess.Popen(
        [str(sil_run), str(manifest), "--no-recording"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        # --no-recording still writes a provenance side-car beside the
        # default output path; keep it out of the repository root.
        cwd=manifest.parent,
        env=_scoped_env(regions),
    )
    _wait_for_observer(observer)
    runner.send_signal(termination_signal)
    _, stderr = runner.communicate(timeout=5)

    assert "run interrupted" in stderr
    assert_cleanup(
        runner, observer, regions, expected_code=1, stderr=stderr
    )


def test_process_without_descendants_preserves_recording_bytes(run_sil, tmp_path):
    log = tmp_path / "protocol.json"
    manifest = Manifest(duration_ns=10_000_000)
    manifest.add_process(
        "clean",
        command=[
            sys.executable,
            str(ROOT / "tests" / "participants" / "protocol_probe.py"),
            str(log),
        ],
        step_period_ns=10_000_000,
    )
    manifest_path = manifest.write(tmp_path / "clean.json").path

    first = run_sil(manifest_path, out=tmp_path / "first.mcap")
    second = run_sil(manifest_path, out=tmp_path / "second.mcap")

    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout
    assert (tmp_path / "first.mcap").read_bytes() == (
        tmp_path / "second.mcap"
    ).read_bytes()
