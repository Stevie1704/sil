"""Replay saved #362 evidence without perf, Linux or workload binaries."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from decode import bind_capture
from evidence import PidNamespace, read_json, sha256
from report import STATES

# Affine composition modulo 2^64 independently gives work(15M) XOR work(30M).
EXPECTED_MESSAGES = [{"channel": channel, "virtual_ns": i * 1_000_000,
                      "index": i, "value": 3776395645756024896}
                     for i in range(3) for channel in ("peer", "slow")]


def verify_inventory(root):
    for name, expected in read_json(root / "integrity.json").items():
        artifact = root / name
        if not artifact.resolve().is_relative_to(root.resolve()):
            raise ValueError("integrity path escapes evidence directory")
        assert sha256(artifact) == expected, name


def verify_partition(window):
    if window["end_ns"] is None:
        assert window["intervals"] is None and window["states_ns"] is None
        return
    cursor = window["begin_ns"]
    for begin, end, state in window["intervals"]:
        assert begin == cursor and begin < end <= window["end_ns"], window
        assert state in STATES
        cursor = end
    assert cursor == window["end_ns"], window


def verify_normal_capture(analysis):
    assert analysis["completed_steps"] == 6
    assert not analysis["reasons"], analysis["reasons"]
    for phase, evidence in analysis["phase_evidence"].items():
        assert evidence["windows"] == 6, phase
        # Direct state presence qualifies interpretation, with no minimum ns
        # threshold or repeatability assertion for the measured runnable delay.
        assert evidence["observed_windows"] == 6, f"{phase}: expected state not observed"
    assert len({w["lifetime"] for w in analysis["windows"] if w["kind"] == "step"}) == 2
    assert sum(bool(life["exec_ns"]) for life in analysis["lifetimes"]) >= 8
    assert any(w["kind"] == "worker_window_association" for w in analysis["windows"])


def verify_capture_fault(row, analysis):
    if row["mode"] == "unclosed":
        assert analysis["activation_footer"] is None
        assert any(w["outcome"] == "unclosed" for w in analysis["windows"])
    if row["fault"] == "truncated":
        assert row["injected_truncation"] and "truncated" in analysis["reasons"]
    if row["fault"] in ("limit", "interrupt"):
        expected = {"limit": ("size_guard", "collector_size_limit"),
                    "interrupt": ("interrupted", "collector_interrupted")}[row["fault"]]
        assert analysis["collector_stop_reason"] == row["collector_stop_reason"] == expected[0]
        assert expected[1] in analysis["reasons"]


def validate_capture(directory, row, namespace):
    document, analysis = bind_capture(directory, row, namespace)
    assert document == read_json(directory / "input.json")
    assert analysis == read_json(directory / "analysis.json")
    if row["fault"] is None and row["mode"] in ("success", "failure"):
        assert analysis["clock_verified"] and analysis["namespace_verified"]
    if row["mode"] == "success" and row["fault"] is None:
        verify_normal_capture(analysis)
    verify_capture_fault(row, analysis)
    for window in analysis["windows"]:
        verify_partition(window)
    return {"name": row["name"], "analysis": analysis}


def verify_outcome(row, rows):
    assert row["run"]["stopped"] is None, row["name"]
    if row["mode"] == "unclosed":
        assert row["run"]["exit_code"] == -9
    elif row["mode"] == "edge":
        assert row["run"]["exit_code"] in (0, 1)
        assert row["messages"] == EXPECTED_MESSAGES[:len(row["messages"])]
    else:
        baseline = next(r for r in rows if not r["observed"] and r["mode"] == row["mode"])
        assert row["run"]["exit_code"] == baseline["run"]["exit_code"] == (1 if row["mode"] == "failure" else 0)
        assert row["recording_sha256"] == baseline["recording_sha256"]
        assert row["messages"] == (EXPECTED_MESSAGES[:3] if row["mode"] == "failure" else EXPECTED_MESSAGES)


def validate_row(root, row, rows, namespace):
    directory = root / row["name"]
    if not directory.resolve().is_relative_to(root.resolve()):
        raise ValueError("case path escapes evidence directory")
    assert read_json(directory / "result.json") == row
    if row["availability"] == "unavailable":
        assert not row["collector"]["ready"] and "run" not in row
        return {"name": row["name"], "availability": "unavailable"}
    verify_outcome(row, rows)
    assert sha256(directory / "run.mcap") == row["recording_sha256"]
    return validate_capture(directory, row, namespace) if row["observed"] else None


def validate(root: Path):
    verify_inventory(root)
    identity = read_json(root / "identity.json")
    namespace = PidNamespace.from_pair(identity["pid_namespace"])
    rows = read_json(root / "results.json")
    captures = [result for row in rows if (result := validate_row(root, row, rows, namespace)) is not None]
    return {"decision": "defer", "environment": identity["environment"],
            "captures": captures, "validated_runs": sum("run" in row for row in rows)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    result = validate(args.evidence)
    print(json.dumps({"decision": result["decision"], "environment": result["environment"],
                      "validated_runs": result["validated_runs"],
                      "capture_attempts": len(result["captures"])}, indent=2))
