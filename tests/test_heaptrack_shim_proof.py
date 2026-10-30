"""Exercise #330 proof validation and cleanup without stored captures."""
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def experiment_report():
    """Minimal synthetic rows for validator tests, independent of captured Runs."""
    return {"bounds": {"raw_bytes": 1024, "wall_seconds": 30}, "runs": [
        {"policy": policy, "mode": mode, "manifest": {"sha256": policy},
         "exit_code": 0, "stopped": None, "messages": 10,
         "clocks_match_virtual_time": {"parent": True, "child": True, "grandchild": True},
         "sleep_matches_policy": True, "recording_equal": False,
         "raw": {"bytes": 2048}, "heap": {
             "interpret_exit_code": 0, "timestamps": {"count": 10},
             "coverage": {"undecodable_records": 0, "truncated": False},
             "known_site": {"calls": 80, "sizes": [2048],
                            "expected_calls": 80, "expected_size": 2048}}}
        for policy in ("reject", "immediate")
        for mode in ("baseline", "preload", "preserve")]}


@pytest.fixture
def proof_module(monkeypatch):
    import runpy
    from types import SimpleNamespace
    monkeypatch.syspath_prepend(str(ROOT / "proofs/heaptrack-shim"))
    return SimpleNamespace(**runpy.run_path(str(ROOT / "proofs/heaptrack-shim/proof.py")))


def test_completed_matrix_is_valid(proof_module, experiment_report):
    observations = proof_module.validate_evidence(experiment_report)
    assert len(observations) == 6
    assert all(row["status"] == "completed" and row["run_complete"]
               for row in observations)


def test_bounded_capture_is_incomplete_negative_evidence(proof_module, experiment_report):
    # A valid bounded capture retains interpreted timestamps and completes
    # runner cleanup; it must never become a successful/equal Run.
    report = experiment_report
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


@pytest.mark.parametrize("invalid", ["cleanup", "unstable", "equal", "empty", "decode", "forced_kill"])
def test_bounded_capture_cannot_hide_missing_or_inconsistent_evidence(
        proof_module, experiment_report, invalid):
    report = experiment_report
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
    elif invalid == "forced_kill":
        row["exit_code"] = -9
    else:
        row["analysis_error"] = "undecodable capture"
    with pytest.raises(AssertionError):
        proof_module.validate_evidence(report)


def test_profiler_startup_failure_is_a_named_negative_observation(proof_module, experiment_report):
    report = experiment_report
    row = next(r for r in report["runs"] if r["mode"] == "preload")
    row.update(exit_code=1, recording_equal=False, stderr_nonempty=True, messages=0,
               failure={"message": "participant exited during initialization"})
    observation = next(o for o in proof_module.validate_evidence(report)
                       if o["policy"] == row["policy"] and o["mode"] == row["mode"])
    assert observation["status"] == "run_failed"
    assert observation["run_complete"] is False


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


def test_bpf_setup_probe_records_inaccessible_interfaces(proof_module, monkeypatch):
    import runpy
    probe = runpy.run_path(str(ROOT / "proofs/heaptrack-shim/availability.py"))
    original = Path.exists

    def inaccessible(path):
        if str(path) == "/sys/kernel/debug/tracing":
            raise PermissionError(13, "Permission denied", str(path))
        return original(path)

    monkeypatch.setattr(Path, "exists", inaccessible)
    facts = probe["facts"]()
    denied = facts["interfaces"]["/sys/kernel/debug/tracing"]
    assert denied["present"] is None
    assert "Permission denied" in denied["error"]


@pytest.mark.parametrize("timestamps, origin_domain", [
    ("c 0\nc fffffffffffffc18\n", "host"), ("c 7b\n", "unknown")])
def test_clock_origin_requires_a_model_for_every_timestamp(
        proof_module, monkeypatch, tmp_path, timestamps, origin_domain):
    import runpy
    import struct
    clock = runpy.run_path(str(ROOT / "proofs/heaptrack-shim/clock_probe.py"))
    library = tmp_path / "libstdc++.so.6"
    library.write_bytes(b"synthetic mapped library")
    (tmp_path / "maps.txt").write_text(f"1000-2000 r-xp 00000000 00:00 0 {library}\n")
    (tmp_path / "audit.bin").write_bytes(
        struct.pack("<8Q", 0, 1, 1, 0x1110, 1_000_000_000, 999_999_999, 1_000_000_001, 0) +
        struct.pack("<8Q", 1, 1, 1, 0x1110, 0, 1_000_000_001, 1_000_000_002, 0))
    (tmp_path / "heaptrack.raw").write_text(timestamps)
    monkeypatch.setitem(clock["_samples"].__globals__, "command", lambda args: {
        "stdout": "0000000000000100 0000000000000020 T _ZNSt6chrono3_V212steady_clock3nowEv\n"})
    result = clock["_analyze"](tmp_path)
    assert result["observed_clock_id"] == 1
    assert [sample["domain"] for sample in result["audit_samples"]] == ["host", "controlled_virtual"]
    assert result["origin_domain_inferred_from_all_timestamp_records"] == origin_domain
    assert result["origin_candidates_ns"] == ([1_000_000_000] if origin_domain == "host" else [])
    assert result["host_mapping"] is None
