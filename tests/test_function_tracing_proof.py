"""Independent events at the proof's file-in/report-out boundary (#320)."""
import json
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def analyze(tmp_path, events, activations, *, observation_footer=None, **metadata):
    raw = b"".join(struct.pack("<QIHH", *event) for event in events)
    (tmp_path / "events.bin").write_bytes(raw)
    (tmp_path / "events.bin.json").write_text(json.dumps({
        "clock": "host_monotonic", "pid": 42, "records": len(events),
        "capacity": 65536, "dropped": 0, "bytes_written": len(raw),
        "clock_errors": 0, "clock_bracket_ns": [90, 91, 92], **metadata}))
    rows = [{"sil_activation_observations": 1, "clock": "host_monotonic",
             "runner_pid": 42, "runner_tid": 42}, *activations,
            observation_footer if observation_footer is not None else
            {"kind": "end", "records": len(activations), "dropped": 0,
             "truncated": False, "write_failed": False}]
    (tmp_path / "observations.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows))
    result = subprocess.run([
        sys.executable, str(ROOT / "proofs/function-tracing/analyze.py"),
        str(tmp_path / "events.bin"), str(tmp_path / "observations.jsonl"),
        str(tmp_path / "report.json")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads((tmp_path / "report.json").read_text())


def activation(begin=100, end=110, index=0, **overrides):
    return {"kind": "activation", "participant": "clear", "task": "control",
            "task_kind": "native_task", "index": index, "virtual_time_ns": 0,
            "tid": 42, "begin_ns": str(begin), "end_ns": str(end),
            "outcome": "completed", **overrides}


def test_calls_join_half_open_activations_and_retain_order_and_duration(tmp_path):
    report = analyze(tmp_path, [(100, 42, 4, 0), (101, 42, 2, 0),
                               (105, 42, 2, 1), (109, 42, 4, 1),
                               (110, 42, 1, 0), (115, 42, 1, 1)],
                     [activation(), activation(110, 120, 1)])
    assert report["status"] == "complete"
    first, second = report["activations"]
    assert first["call_order"] == ["select_lead", "confirmed"]
    assert [(c["function"], c["elapsed_host_ns"], c["depth"])
            for c in first["calls"]] == [("select_lead", 9, 0),
                                         ("confirmed", 4, 1)]
    assert second["counts"] == {"check_object": 1}


def test_lost_events_cannot_claim_exact_per_activation_calls(tmp_path):
    report = analyze(tmp_path, [(101, 42, 3, 0), (105, 42, 3, 1)],
                     [activation()], dropped=2)
    assert report["status"] == "partial"
    assert report["problems"] == ["dropped_events"]
    assert report["activations"][0]["counts"] == {}
    assert len(report["unattributed"]) == 1


def test_recursion_keeps_entry_order_and_worker_and_boundary_calls_unattributed(tmp_path):
    report = analyze(tmp_path, [(101, 42, 2, 0), (102, 42, 2, 0),
                               (103, 7, 1, 0), (104, 7, 1, 1),
                               (105, 42, 2, 1), (106, 42, 2, 1),
                               (110, 42, 1, 0), (111, 42, 1, 1)],
                     [activation()])
    assert report["activations"][0]["counts"] == {"confirmed": 2}
    assert [(c["depth"], c["elapsed_host_ns"])
            for c in report["activations"][0]["calls"]] == [(0, 5), (1, 3)]
    assert [c["tid"] for c in report["unattributed"]] == [7, 42]


def test_ambiguous_or_failed_activations_never_receive_calls(tmp_path):
    report = analyze(tmp_path, [(101, 42, 1, 0), (105, 42, 1, 1)],
                     [activation(), activation(participant="eight"),
                      activation(outcome="failed")])
    assert all(a["counts"] == {} for a in report["activations"])
    assert len(report["unattributed"]) == 1


def test_unclosed_call_reports_partial_without_inventing_a_duration(tmp_path):
    report = analyze(tmp_path, [(101, 42, 1, 0)], [activation()])
    assert report["status"] == "partial"
    assert report["problems"] == ["unclosed_calls"]
    assert report["unattributed"][0]["elapsed_host_ns"] is None


def test_missing_loss_accounting_cannot_claim_a_complete_capture(tmp_path):
    report = analyze(tmp_path, [(101, 42, 1, 0), (105, 42, 1, 1)],
                     [activation()], observation_footer={"kind": "end", "records": 1})
    assert report["status"] == "partial"
    assert report["problems"] == ["incomplete_activations"]
    assert report["activations"][0]["counts"] == {}
