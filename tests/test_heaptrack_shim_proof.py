"""Check retained #330 evidence independently of unavailable host tools."""
import hashlib
import json
from pathlib import Path
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
        from sil.recording import read_records, read_schemas
        from sil.schema import MessageType
        recording = EVIDENCE / f'{row["policy"]}-{row["mode"]}/run.mcap'
        codec = MessageType("proof.Clock", read_schemas(recording)["proof.clock"])
        messages = [{"t_ns": t, **codec.unpack(data)} for _, t, data in read_records(recording)]
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


@pytest.mark.parametrize("directory", [EVIDENCE, ROOT / "proofs/heaptrack-shim/evidence-failure",
                                         ROOT / "proofs/heaptrack-shim/evidence-review"])
def test_retained_files_match_the_capture_inventory(directory):
    inventory = load(directory / "digests.json")
    retained = {str(p.relative_to(directory)) for p in directory.rglob("*")
                if p.is_file() and p.name not in {"origin.json", "digests.json"}}
    assert retained == inventory.keys()
    for path in directory.rglob("*"):
        if not path.is_file() or path.name in {"origin.json", "digests.json"}:
            continue
        expected = inventory[str(path.relative_to(directory))]
        assert path.stat().st_size == expected["bytes"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected["sha256"]


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
    row = next(r for r in report["runs"] if r["mode"] == "preload")
    row.update(exit_code=1, recording_equal=False, stderr_nonempty=True, messages=0,
               failure=load(ROOT / "proofs/heaptrack-shim/evidence-failure/reject-preload/run.mcap.failure.json"))
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



def test_retained_clock_model_matches_audit_bytes_and_every_timestamp(proof_module):
    import runpy
    import struct
    root = ROOT / "proofs/heaptrack-shim/evidence-review/heaptrack-shim-clock"
    report = load(root / "results.json")
    assert report["status"] == "established"
    arithmetic = runpy.run_path(str(ROOT / "proofs/heaptrack-shim/clock_probe.py"))["_milliseconds"]
    for mode, analysis in report["runs"].items():
        symbols = [line.split() for line in load(root / mode / "steady-clock-symbols.json")["matches"]]
        mappings = [line.split(maxsplit=5) for line in (root / mode / "maps.txt").read_text().splitlines()]
        rows = list(struct.iter_unpack("<8Q", (root / mode / "audit.bin").read_bytes()))
        assert len(rows) == len(analysis["audit_samples"])
        for raw, sample in zip(sorted(rows), analysis["audit_samples"]):
            assert list(raw) == [sample[key] for key in (
                "sequence", "tid", "clock_id", "caller_address", "returned_ns",
                "host_before_ns", "host_after_ns", "status")]
            assert sample["clock_id"] == 1 and sample["status"] == 0
            assert "steady_clock3now" in sample["symbol"]
            assert any(int(symbol[0], 16) <= sample["module_offset"] < int(symbol[0], 16) + int(symbol[1], 16)
                       and symbol[3] == sample["symbol"] for symbol in symbols)
            mapping = next(m for m in mappings if len(m) == 6 and m[5] == sample["module"]
                           and int(m[0].split("-")[0], 16) <= sample["caller_address"] < int(m[0].split("-")[1], 16))
            assert sample["module_offset"] == (sample["caller_address"] -
                int(mapping[0].split("-")[0], 16) + int(mapping[2], 16))
            if sample["domain"] == "host":
                assert sample["host_before_ns"] <= sample["returned_ns"] <= sample["host_after_ns"]
            else:
                assert sample["returned_ns"] in {0, 100_000_000, 200_000_000, 400_000_000, 900_000_000}
        projected = load(root / mode / "timestamp-records.json")
        assert sum(count for value, count in projected["counts_by_value_ms"]) == analysis["timestamp_count"]
        assert {value for value, count in projected["counts_by_value_ms"]} == set(analysis["timestamp_values_ms"])
        for origin in analysis["origin_candidates_ns"]:
            reproduced = {arithmetic(sample["returned_ns"] - origin) for sample in analysis["audit_samples"]}
            assert set(analysis["timestamp_values_ms"]) <= reproduced
        domains = {sample["domain"] for sample in analysis["audit_samples"]
                   if sample["returned_ns"] in analysis["origin_candidates_ns"]}
        assert len(domains) == 1
        assert analysis["origin_domain_inferred_from_all_timestamp_records"] == domains.pop()
        assert analysis["host_mapping"] is None
