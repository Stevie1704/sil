"""Check retained #330 evidence independently of unavailable host tools."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "proofs/heaptrack-shim/evidence"


def load(path):
    return json.loads(path.read_text())


def test_retained_native_evidence_is_complete_and_uses_the_pinned_tools():
    report = load(EVIDENCE / "results.json")
    assert report["complete"] and report["qualified_machine"]
    assert report["host"]["machine"] == "x86_64"
    assert report["host"]["libc"][0] == "glibc"
    assert report["host"]["uid"] != 0
    packages = next(row["stdout"] for row in report["tools"]
                    if row["command"][0] == "dpkg-query")
    assert "heaptrack\t1.5.0+dfsg1-2ubuntu3" in packages
    assert {(row["policy"], row["mode"]) for row in report["runs"]} == {
        (policy, mode) for policy in ("reject", "immediate")
        for mode in ("baseline", "preload", "preserve")}
    assert len(report["runs"]) == 6
    for policy in ("reject", "immediate"):
        rows = [r for r in report["runs"] if r["policy"] == policy]
        assert len({r["manifest"]["sha256"] for r in rows}) == 1


def test_retained_verdicts_follow_recording_bytes_and_observed_messages():
    report = load(EVIDENCE / "results.json")
    for row in report["runs"]:
        assert row["exit_code"] == 0 and row["stopped"] is None
        messages = load(EVIDENCE / f'{row["policy"]}-{row["mode"]}/messages.json')
        assert len(messages) == row["messages"] == 10
        assert [m["t_ns"] for m in messages] == list(range(0, 1_000_000_000, 100_000_000))
        for field, verdict in row["clocks_match_virtual_time"].items():
            assert verdict == all(m[field] == m["t_ns"] for m in messages)
        expected = (-1, 38) if row["policy"] == "reject" else (0, 0)
        assert all((m["sleep_status"], m["sleep_errno"]) == expected for m in messages)
        assert row["sleep_matches_policy"]
        if row["mode"] == "baseline":
            assert all(row["clocks_match_virtual_time"].values())
            continue
        baseline = next(r for r in report["runs"]
                        if r["policy"] == row["policy"] and r["mode"] == "baseline")
        assert row["recording_equal"] == (row["recording"] == baseline["recording"])
        captured_bytes = (EVIDENCE / f'{row["policy"]}-{row["mode"]}/run.mcap').read_bytes()
        baseline_bytes = (EVIDENCE / f'{row["policy"]}-baseline/run.mcap').read_bytes()
        assert row["recording_equal"] == (captured_bytes == baseline_bytes)
        heap = row["heap"]
        assert heap["known_site"]["calls"] == 80
        assert heap["known_site"]["sizes"] == [2048]
        assert heap["interpret_exit_code"] == 0
        assert heap["coverage"]["undecodable_records"] == 0
        assert not heap["coverage"]["truncated"]
        assert heap["timestamps"]["host_mapping"] is None


@pytest.mark.parametrize("directory", [EVIDENCE, ROOT / "proofs/heaptrack-shim/evidence-failure"])
def test_retained_files_match_the_capture_inventory(directory):
    inventory = load(directory / "digests.json")
    for path in directory.rglob("*"):
        if not path.is_file() or path.name in {"origin.json", "digests.json"}:
            continue
        expected = inventory[str(path.relative_to(directory))]
        assert path.stat().st_size == expected["bytes"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected["sha256"]


@pytest.mark.parametrize("content", ["changed evidence", ""])
def test_retention_rejects_a_changed_or_truncated_artifact(tmp_path, content):
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "results.json").write_text(content)
    (artifact / "digests.json").write_text(json.dumps({"results.json": {
        "bytes": 2, "sha256": hashlib.sha256(b"{}").hexdigest()}}))
    done = subprocess.run([sys.executable, str(ROOT / "proofs/heaptrack-shim/retain.py"),
        str(artifact), str(tmp_path / "retained")], capture_output=True, text=True, timeout=10)
    assert done.returncode != 0
    assert not (tmp_path / "retained/results.json").exists()


@pytest.fixture
def proof_module(monkeypatch):
    import runpy
    from types import SimpleNamespace
    monkeypatch.syspath_prepend(str(ROOT / "proofs/heaptrack-shim"))
    return SimpleNamespace(**runpy.run_path(str(ROOT / "proofs/heaptrack-shim/proof.py")))


def test_bounded_capture_is_incomplete_negative_evidence(proof_module):
    # A valid bounded capture retains interpreted timestamps and completes
    # runner cleanup; it must never become a successful/equal Run.
    report = load(EVIDENCE / "results.json")
    report["bounds"] = {"raw_bytes": 1024, "wall_seconds": 30}
    row = next(r for r in report["runs"] if r["mode"] == "preserve")
    row.update(stopped="raw_size_limit", exit_code=1, cleanup_completed=True,
               raw_stable=True, recording_equal=False)
    observations = proof_module.validate_evidence(report)
    observation = next(o for o in observations
                       if o["mode"] == row["mode"] and o["policy"] == row["policy"])
    assert observation["status"] == "bounded"
    assert observation["run_complete"] is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_capture_limit_allows_runner_to_reap_its_participant(
        proof_module, build_dir, tmp_path):
    import os
    import signal
    from sil.manifest import Manifest

    writer = tmp_path / "writer.py"
    raw = tmp_path / "heaptrack.raw"
    pid_file = tmp_path / "participant.pid"
    writer.write_text('''
import os, pathlib, sys, time
pathlib.Path(sys.argv[2]).write_text(str(os.getpid()))
with open(sys.argv[1], "wb", buffering=0) as output:
    while True:
        output.write(b"x" * 8192)
        time.sleep(0.01)
''')
    manifest = Manifest(duration_ns=1)
    manifest.add_process("writer", command=[sys.executable, str(writer), str(raw),
        str(pid_file)], step_period_ns=1)
    path = manifest.write(tmp_path / "manifest.json").path
    try:
        outcome = proof_module.execute([str(build_dir / "sil-run"), str(path),
            "-o", str(tmp_path / "run.mcap")], tmp_path, os.environ.copy(), raw,
            raw_limit_bytes=4096)
        assert outcome["stopped"] == "raw_size_limit"
        assert outcome["exit_code"] == 1
        assert outcome["cleanup_completed"]
        with pytest.raises(ProcessLookupError):
            os.killpg(int(pid_file.read_text()), 0)
    finally:
        if pid_file.exists():
            try:
                os.killpg(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("invalid", ["cleanup", "unstable", "equal", "empty", "decode"])
def test_bounded_capture_cannot_hide_missing_or_inconsistent_evidence(proof_module, invalid):
    report = load(EVIDENCE / "results.json")
    report["bounds"]["raw_bytes"] = 1024
    row = next(r for r in report["runs"] if r["mode"] == "preserve")
    row.update(stopped="raw_size_limit", exit_code=1, cleanup_completed=True,
               raw_stable=True, recording_equal=False)
    if invalid == "cleanup":
        row["cleanup_completed"] = False
    elif invalid == "unstable":
        row["raw_stable"] = False
    elif invalid == "equal":
        row["recording_equal"] = True
    elif invalid == "empty":
        row["heap"]["timestamps"]["count"] = 0
    else:
        row["analysis_error"] = "undecodable capture"
    with pytest.raises(AssertionError):
        proof_module.validate_evidence(report)


def test_profiler_startup_failure_is_a_named_negative_observation(proof_module):
    report = load(EVIDENCE / "results.json")
    failed = load(ROOT / "proofs/heaptrack-shim/evidence-failure/results.json")
    row = next(r for r in report["runs"] if r["mode"] == "preload")
    original = next(r for r in failed["runs"] if r["mode"] == "preload")
    row.update(exit_code=1, recording_equal=False, stderr_nonempty=True,
               failure=load(ROOT / "proofs/heaptrack-shim/evidence-failure/reject-preload/run.mcap.failure.json"))
    row["messages"] = original["messages"]
    observation = next(o for o in proof_module.validate_evidence(report)
                       if o["policy"] == row["policy"] and o["mode"] == row["mode"])
    assert observation["status"] == "run_failed"
    assert observation["run_complete"] is False


def test_original_failed_run_is_not_accepted_as_clean_bounded_evidence(proof_module):
    failed = load(ROOT / "proofs/heaptrack-shim/evidence-failure/results.json")
    assert failed.get("complete") is not True
    # Earlier failed rows must have a named failure before replaying validation.
    for row in failed["runs"]:
        if row["exit_code"] == 1:
            directory = ROOT / "proofs/heaptrack-shim/evidence-failure" / f'{row["policy"]}-{row["mode"]}'
            row["failure"] = load(directory / "run.mcap.failure.json")
            row["stderr_nonempty"] = bool(row["failure"]["stderr_tail"])
    with pytest.raises(AssertionError):
        proof_module.validate_evidence(failed)


def test_timestamp_statistics_do_not_invent_a_measured_clock(proof_module, tmp_path):
    raw = tmp_path / "heaptrack.raw"
    raw.write_bytes(b"v 10500 3\nc 4\nc 4\nc 68\n")
    result = proof_module.analyze(raw, Path("/bin/cat"), tmp_path)
    assert result["timestamps"]["count"] == 3
    assert result["timestamps"]["unique"] == 2
    assert result["timestamps"]["clock"] is None
    assert result["timestamps"]["clock_assessment"] == "not_measured_in_this_capture"
    assert result["timestamps"]["host_mapping"] is None


@pytest.mark.parametrize("nanoseconds, expected", [
    (-999_999, 0), (-1_000_001, (1 << 64) - 1), (1_000_001, 1)])
def test_clock_calibration_uses_cpp_signed_duration_arithmetic(proof_module, nanoseconds, expected):
    import runpy
    clock = runpy.run_path(str(ROOT / "proofs/heaptrack-shim/clock_probe.py"))
    assert clock["_milliseconds"](nanoseconds) == expected
