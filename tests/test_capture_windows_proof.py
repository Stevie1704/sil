"""Independent facts at the #361 proof's file-input/report-output seam."""
import json
from pathlib import Path
import subprocess
import sys


ANALYZER = Path(__file__).resolve().parents[1] / "proofs/capture-windows/analyze.py"


def write(path, value):
    path.write_text(json.dumps(value))


def report(tmp_path, *, overrides=None, samples=None, controls=None, markers=None,
           observer=None, expect_error=False):
    run = tmp_path / "native-window-0"
    run.mkdir()
    row = {"variant": "native", "mode": "window", "requested": {
        "from_ns": 610_000_000, "to_ns": 611_000_000, "task": None},
        "exit_code": 0, "run_exit_code": 0, "elapsed_ns": 1000,
        "controls": controls or [], "stop_reason": None,
        "perf_bytes": 100, "exports": {},
        "observer": observer or {"sent": 4, "received": 4, "dropped": 0},
        **(overrides or {})}
    write(run / "result.json", row)
    write(tmp_path / "results.json", {"runs": [row]})
    write(run / "notices.json", [[99, 610_000_000, 0, 0, 100],
                                  [119, 610_000_000, 1, 0, 120]])
    write(run / "markers.json", {"lost": 0, "events": markers or [
        [100, 0, 610, 0], [110, 0, 610, 1],
        [110, 1, 610, 0], [120, 1, 610, 1],
        [120, 0, 611, 0], [130, 0, 611, 1]]})
    write(run / "sample-times.json", samples or [])
    rows = [{"kind": "activation", "outcome": "completed"},
            {"kind": "end", "dropped": 0, "truncated": False, "write_failed": False}]
    (run / "activations.jsonl").write_text("".join(json.dumps(r)+"\n" for r in rows))
    completed = subprocess.run([sys.executable, str(ANALYZER), str(tmp_path)],
                               capture_output=True, text=True)
    if expect_error:
        assert completed.returncode != 0
        assert not (tmp_path / "analysis.json").exists()
        return completed.stderr
    assert completed.returncode == 0, completed.stderr
    return json.loads((tmp_path / "analysis.json").read_text())["runs"][0]["analysis"]


def test_collector_failure_does_not_mark_successful_run_unclosed(tmp_path):
    result = report(tmp_path, overrides={"exit_code": 1, "run_exit_code": 0})
    assert "unclosed_run_work" not in result["reasons"]
    assert "collector_failed" in result["reasons"]
    assert result["completed_activations"] == 1


def test_ack_before_command_is_rejected_before_writing_report(tmp_path):
    error = report(tmp_path, controls=[{"command": "enable", "write_ns": 105,
                                       "ack_ns": 104}], expect_error=True)
    assert "acknowledgment precedes command" in error


def test_half_open_edges_keep_equal_virtual_time_tasks_distinct(tmp_path):
    result = report(tmp_path, samples=[99, 100, 109, 110, 119, 120, 130])
    assert result["known_interval_sample_counts"] == [
        {"task": 0, "index": 610, "samples": 2},
        {"task": 1, "index": 610, "samples": 2}]
    assert result["samples_inside_known_selected_work"] == 4
    assert result["samples_outside_known_selected_work"] == 3


def test_failed_run_does_not_invent_an_unclosed_activation(tmp_path):
    result = report(tmp_path, overrides={"exit_code": 255, "run_exit_code": 1})
    assert "run_failed" in result["reasons"]
    assert "unclosed_run_work" not in result["reasons"]
    assert result["failed_activations"] == 0
