"""A Run never writes an output over a Recording that it replays (issue #387).

The runner truncates its outputs in place. An output path that is the same
file as a Replay input would destroy the stimulus that the Manifest hash
covers, so the runner refuses the Run before it opens any output.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from test_replay_bounded import replay_manifest, ticks_at, write_recording

MS = 1_000_000


@pytest.fixture
def replayed(tmp_path: Path) -> Path:
    return write_recording(tmp_path / "in.mcap", ticks_at(0, 10 * MS))


@pytest.fixture
def manifest(tmp_path: Path, replayed: Path) -> Path:
    return replay_manifest(replayed).write(tmp_path / "m.json").path


def run(sil_run: Path, manifest: Path, *args: str, cwd: Path):
    return subprocess.run([str(sil_run), str(manifest), *args],
                          capture_output=True, text=True, cwd=cwd)


def assert_refused(proc, replayed: Path, before: bytes, output: str):
    assert proc.returncode == 2, proc.stderr
    assert replayed.read_bytes() == before
    assert "participant 'rep'" in proc.stderr
    assert str(replayed) in proc.stderr
    assert output in proc.stderr


@pytest.mark.parametrize("spelling", ["same", "relative", "symlink", "hardlink"])
def test_recording_output_on_a_replay_input_is_refused(
    sil_run, tmp_path, replayed, manifest, spelling
):
    before = replayed.read_bytes()
    cwd = tmp_path / "work"
    cwd.mkdir()
    out = {"same": str(replayed), "relative": "../in.mcap",
           "symlink": "link.mcap", "hardlink": "link.mcap"}[spelling]
    if spelling == "symlink":
        (cwd / "link.mcap").symlink_to(replayed)
    elif spelling == "hardlink":
        (cwd / "link.mcap").hardlink_to(replayed)

    proc = run(sil_run, manifest, "-o", out, cwd=cwd)

    assert_refused(proc, replayed, before, f"Recording '{out}'")
    # The refused Run opens no output, so no side-car is left beside it.
    assert list(cwd.glob("*.json")) == []
    assert not Path(str(replayed) + ".provenance.json").exists()
    assert not Path(str(replayed) + ".failure.json").exists()


def test_provenance_output_on_a_replay_input_is_refused(
    sil_run, tmp_path, replayed, manifest
):
    before = replayed.read_bytes()
    out = tmp_path / "out.mcap"

    proc = run(sil_run, manifest, "-o", str(out), "--provenance", "in.mcap",
               cwd=tmp_path)

    assert_refused(proc, replayed, before, "provenance record 'in.mcap'")
    assert not out.exists()
    assert not Path(str(out) + ".failure.json").exists()


def test_failure_report_on_a_replay_input_is_refused(
    sil_run, tmp_path, replayed, manifest
):
    before = replayed.read_bytes()
    out = tmp_path / "out.mcap"
    report = Path(str(out) + ".failure.json")
    report.symlink_to(replayed)

    proc = run(sil_run, manifest, "-o", str(out), cwd=tmp_path)

    assert_refused(proc, replayed, before, f"failure report '{report}'")
    assert not out.exists()
    assert report.is_symlink()
