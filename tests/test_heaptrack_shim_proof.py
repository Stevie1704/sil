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


def test_retained_recording_and_clock_verdicts_follow_the_observed_messages():
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
        assert row["recording_equal"] == (row["mode"] == "preserve")
        heap = row["heap"]
        assert heap["known_site"]["calls"] == 80
        assert heap["known_site"]["sizes"] == [2048]
        assert heap["interpret_exit_code"] == 0
        assert heap["coverage"]["undecodable_records"] == 0
        assert not heap["coverage"]["truncated"]
        assert heap["timestamps"]["host_mapping"] is None


def test_every_retained_experiment_file_matches_the_original_inventory():
    inventory = load(EVIDENCE / "digests.json")
    for path in EVIDENCE.rglob("*"):
        if not path.is_file() or path.name in {"origin.json", "digests.json"}:
            continue
        expected = inventory[str(path.relative_to(EVIDENCE))]
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
