"""The documented managed-platform example (#329) runs and says what it must.

Expected digests come from `hashlib` over the files on disk. The expected
state of each digest comes from the table in docs/running.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT

EXAMPLE = ROOT / "examples" / "managed-platform" / "platform_run.py"


def digest(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run_example(build_dir: Path, workdir: Path, execution_id: str = "exec-0001"):
    path = os.pathsep.join([str(build_dir), "/usr/bin:/bin"])
    return subprocess.run(
        [sys.executable, str(EXAMPLE), "--execution-id", execution_id,
         "--workdir", str(workdir)],
        capture_output=True, text=True, env={**os.environ, "PATH": path})


@pytest.fixture
def executed(build_dir, tmp_path):
    workdir = tmp_path / "run"
    proc = run_example(build_dir, workdir)
    assert proc.returncode == 0, proc.stderr
    record = json.loads((workdir / "execution-record.json").read_text())
    side_car = json.loads(
        (workdir / "outputs" / "out.mcap.provenance.json").read_text())
    return workdir, record, side_car


def test_the_record_is_associated_with_the_external_execution(executed):
    _, record, _ = executed
    assert record["execution_id"] == "exec-0001"
    assert record["run_exit_code"] == 0
    assert record["provenance_mode"] == "external"


def test_the_manifest_digest_is_of_the_exact_bytes_that_sil_read(executed):
    workdir, record, side_car = executed
    expected = digest(workdir / "inputs" / "manifest.json")
    assert record["manifest"]["sha256"] == expected
    assert side_car["manifest_hash"] == expected


def test_each_resolved_input_has_the_identity_of_the_platform(executed):
    workdir, record, side_car = executed
    by_role = {row["role"]: row for row in record["inputs"]}
    assert set(by_role) == {"runner", "executable", "command_file"}
    assert by_role["command_file"]["path"] == os.path.realpath(
        workdir / "inputs" / "participant.py")
    assert by_role["executable"]["path"] == os.path.realpath(sys.executable)
    for row in record["inputs"]:
        assert row["platform_sha256"] == digest(row["path"])
    # The side-car states the path and no digest of its own.
    assert side_car["artifacts"]["runner"]["digest_state"] == "omitted"
    assert side_car["artifacts"]["runner"]["sha256"] is None
    assert side_car["recording"] == {"digest_state": "omitted", "sha256": None}


def test_the_output_digest_is_of_the_finalized_recording(executed):
    workdir, record, _ = executed
    recording = workdir / "outputs" / "out.mcap"
    assert record["output"]["platform_sha256"] == digest(recording)
    assert record["output"]["bytes"] == recording.stat().st_size


def test_the_inputs_are_read_only(executed):
    workdir, _, _ = executed
    for name in ("manifest.json", "participant.py"):
        assert (workdir / "inputs" / name).stat().st_mode & 0o222 == 0
    assert (workdir / "inputs").stat().st_mode & 0o222 == 0


def test_an_existing_workdir_is_refused(build_dir, tmp_path):
    workdir = tmp_path / "run"
    workdir.mkdir()

    proc = run_example(build_dir, workdir)

    assert proc.returncode == 2
    assert not (workdir / "execution-record.json").exists()


def test_a_second_execution_gives_the_same_output_digest(build_dir, tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    assert run_example(build_dir, first).returncode == 0
    assert run_example(build_dir, second, "exec-0002").returncode == 0

    def output_digest(workdir: Path) -> str:
        return json.loads((workdir / "execution-record.json").read_text())[
            "output"]["platform_sha256"]

    assert output_digest(first) == output_digest(second)
