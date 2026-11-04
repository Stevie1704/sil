"""Independent facts at the #361 proof's file-input/report-output seam."""
import json
from pathlib import Path
import subprocess
import sys
import shutil


REPORTER = Path(__file__).resolve().parents[1] / "proofs/capture-windows/validate.py"


def write(path, value):
    path.write_text(json.dumps(value))


def report(tmp_path, *, overrides=None, samples=None, controls=None,
           work_markers=None, perf_stats=None, expect_error=False):
    row = {"variant": "native", "mode": "window", "requested": {
        "from_ns": 610_000_000, "to_ns": 611_000_000, "task": None},
        "exit_code": 0, "run_exit_code": 0, "elapsed_ns": 1000,
        "controls": controls or [], "stop_reason": None,
        "perf_bytes": 100, "exports": {},
        "recording_sha256": "a"*64,
        "observer": {"sent": 4, "received": 4, "dropped": 0},
        "collector_start_host_ns": 90, "collector_exit_observed_host_ns": 140,
        **(overrides or {})}
    run = tmp_path / f"{row['variant']}-{row['mode']}-0"
    run.mkdir()
    write(run / "result.json", row)
    write(tmp_path / "results.json", {"runs": [row]})
    write(run / "notices.json", [
        [99, 610_000_000, 0, 0, 100], [119, 610_000_000, 1, 0, 120]])
    write(run / "markers.json", {"sil_capture_workload_markers": 2, "lost": 0,
        "events": work_markers if work_markers is not None else [
        [100, 0, 610, 610_000_000, 0], [110, 0, 610, 610_000_000, 1],
        [110, 1, 610, 610_000_000, 0], [120, 1, 610, 610_000_000, 1],
        [120, 0, 611, 611_000_000, 0], [130, 0, 611, 611_000_000, 1]]})
    write(run / "perf-sample-host-times.json", samples or [])
    if perf_stats is not None:
        (run / "stats.txt").write_text(perf_stats)
    rows = [{"kind": "activation", "outcome": "completed"},
            {"kind": "end", "dropped": 0, "truncated": False, "write_failed": False}]
    (run / "activations.jsonl").write_text("".join(json.dumps(r)+"\n" for r in rows))
    completed = subprocess.run([sys.executable, str(REPORTER), "--report-only", str(tmp_path)],
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


def test_failure_before_requested_end_does_not_claim_end_was_reached(tmp_path):
    result = report(tmp_path, overrides={
        "mode": "failure", "exit_code": 1, "run_exit_code": 1,
        "requested": {"from_ns": 610_000_000, "to_ns": 615_000_000, "task": None}},
        controls=[{"command": "enable", "write_ns": 102, "ack_ns": 104}])
    assert result["selection_state"] == "terminated_before_requested_end"
    assert result["requested_end_state"] == "not_reached_run_failed"
    assert result["requested_end_notice"] is None


def test_disabled_intent_regions_keep_control_boundaries_and_loss_separate(tmp_path):
    result = report(tmp_path,
        overrides={"observer": {"sent": 4, "received": 4, "dropped": 2}},
        controls=[{"command": "enable", "write_ns": 102, "ack_ns": 105},
                  {"command": "disable", "write_ns": 116, "ack_ns": 120}],
        samples=[105, 111, 117, 119])
    regions = result["disabled_regions"]
    assert [(r["begin_request_host_ns"], r["end_request_host_ns"]) for r in regions] == [(90, 102), (116, 140)]
    assert regions[1]["disable_ack_host_ns"] == 120
    assert regions[1]["observed_samples_in_request_interval"] == 2
    assert all(r["effective_boundaries"] == "unknown" for r in regions)
    assert "notification_loss" in result["reasons"]


def test_selection_uses_observed_virtual_time_instead_of_index_times_period(tmp_path):
    result = report(tmp_path, overrides={"requested": {
        "from_ns": 700_000_000, "to_ns": 701_000_000, "task": None}},
        work_markers=[[100, 0, 610, 700_000_000, 0], [110, 0, 610, 700_000_000, 1],
                      [110, 1, 610, 710_000_000, 0], [120, 1, 610, 710_000_000, 1]],
        samples=[100, 109, 110, 119])
    assert result["known_selected_work"] == [{"task": 0, "index": 610,
        "virtual_ns": 700_000_000, "begin_ns": 100, "end_ns": 110}]
    assert result["samples_inside_known_selected_work"] == 2


def experiment_rejection(tmp_path, mode, overrides):
    rows = []
    for variant in ("native", "process"):
        modes = ("ordinary", "ordinary-failure", mode) if variant == "native" else ("ordinary", "ordinary-failure")
        for current in modes:
            source = tmp_path / f"fixture-{variant}-{current}"
            source.mkdir()
            code = 1 if current == "ordinary-failure" else 0
            fields = {"variant": variant, "mode": current, "exit_code": code, "run_exit_code": code}
            if current == mode:
                fields.update(overrides)
            report(source, overrides=fields)
            rows.extend(json.loads((source / "results.json").read_text())["runs"])
            shutil.move(source / f"{variant}-{current}-0", tmp_path)
    write(tmp_path / "results.json", {"runs": rows})
    result = subprocess.run([sys.executable, str(REPORTER), str(tmp_path)], capture_output=True, text=True)
    assert result.returncode != 0
    assert not (tmp_path / "analysis.json").exists()
    return result.stderr


def test_experiment_validator_rejects_sigkill_case_with_successful_run(tmp_path):
    error = experiment_rejection(tmp_path, "unclosed", {"exit_code": 0, "run_exit_code": 0})
    assert "required evidence state missing: unclosed_run_work" in error


def test_experiment_validator_rejects_exhaustion_without_storage_fault(tmp_path):
    error = experiment_rejection(tmp_path, "exhaustion", {
        "storage_fault_injected": False, "storage_fault_errno": None})
    assert "storage exhaustion was not actually injected" in error


def test_lost_samples_counter_remains_loss_without_a_lost_event_record(tmp_path):
    result = report(tmp_path, overrides={"mode": "perf-loss", "metadata": {"lost_records": 0}},
        perf_stats="  LOST_SAMPLES events: 2 (2.7%)\n  LOST_SAMPLES events: 142\n")
    assert "perf_sample_loss" in result["reasons"]
    assert result["perf_loss_sources"] == ["perf_report_LOST_SAMPLES"]
    assert result["effective_transition_times"] == "not_observed"
