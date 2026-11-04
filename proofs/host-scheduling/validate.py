"""Replay saved #362 evidence without perf, Linux or workload binaries."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from decode import prepare
from report import read


def validate(root: Path):
    identity = read(root / "identity.json")
    for name, expected in read(root / "integrity.json").items():
        path = root / name
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("integrity path escapes evidence directory")
        with path.open("rb") as stream:
            assert hashlib.file_digest(stream, "sha256").hexdigest() == expected, name
    rows = read(root / "results.json")
    output = []
    for row in rows:
        directory = root / row["name"]
        assert read(directory / "result.json") == row
        if row["availability"] == "unavailable":
            assert not row["collector"]["ready"] and "run" not in row
            output.append({"name": row["name"], "availability": "unavailable"})
            continue
        if row["observed"]:
            document, analysis = prepare(directory, row, identity["pid_namespace"])
            assert document == read(directory / "input.json")
            assert analysis == read(directory / "analysis.json")
            if row["fault"] is None and row["mode"] in ("success", "failure"):
                assert analysis["clock_verified"]
                assert analysis["namespace_verified"]
            if row["mode"] == "success" and row["fault"] is None:
                assert analysis["completed_steps"] == 6
                assert not analysis["reasons"], analysis["reasons"]
                for phase, expected in (("cpu", "running"), ("sleep", "sleeping"), ("wait", "sleeping")):
                    phases = [w for w in analysis["windows"] if w.get("phase") == phase]
                    assert len(phases) == 6
                    # Presence of direct evidence validates the state semantics;
                    # no minimum duration or contention timing is asserted.
                    assert all(w["states_ns"][expected] > 0 for w in phases)
                assert len({w["lifetime"] for w in analysis["windows"]
                            if w["kind"] == "step"}) == 2
                assert sum(bool(life["exec_ns"]) for life in analysis["lifetimes"]) >= 8
                assert any(w["kind"] == "worker_window_association" for w in analysis["windows"])
            if row["mode"] == "unclosed":
                assert row["run"]["exit_code"] == -9
                assert analysis["activation_footer"] is None
                assert any(w["outcome"] == "unclosed" for w in analysis["windows"])
            if row["fault"] == "truncated":
                assert row["injected_truncation"]
                assert "truncated" in analysis["reasons"]
            if row["fault"] in ("limit", "interrupt"):
                assert row.get("collector_stop_reason")
            for window in analysis["windows"]:
                if window["states_ns"] is not None:
                    assert sum(window["states_ns"].values()) == window["end_ns"] - window["begin_ns"]
            output.append({"name": row["name"], "analysis": analysis})
        if row["mode"] not in ("unclosed", "edge"):
            baseline = next(r for r in rows if not r["observed"] and r["mode"] == row["mode"])
            assert row["run"]["exit_code"] == baseline["run"]["exit_code"] == (1 if row["mode"] == "failure" else 0)
            assert row["recording_sha256"] == baseline["recording_sha256"]
            # Independently worked affine recurrence modulo 2^64, composed by
            # exponentiation, yields this literal for work(15M) XOR work(30M).
            expected = [{"channel": channel, "virtual_ns": i * 1_000_000,
                         "index": i, "value": 3776395645756024896}
                        for i in range(3) for channel in ("peer", "slow")]
            assert row["messages"] == (expected[:3] if row["mode"] == "failure" else expected)
        if row["mode"] == "edge":
            assert row["run"]["exit_code"] in (0, 1)
        recording = directory / "run.mcap"
        with recording.open("rb") as stream:
            assert hashlib.file_digest(stream, "sha256").hexdigest() == row["recording_sha256"]
    return {"decision": "defer", "environment": identity["environment"], "runs": output}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    result = validate(args.evidence)
    print(json.dumps({"decision": result["decision"], "environment": result["environment"],
                      "validated_runs": len(result["runs"])}, indent=2))
