"""`sil benchmark`: repeated fresh Runs and their `sil_benchmark_report` (#303).

The aggregation tests build Run outcomes from the frozen report examples with
chosen wall times, so every expected statistic is a worked literal and no
assertion depends on how long anything took.
"""

import copy
import json
from pathlib import Path

import pytest

from conftest import ROOT
from sil.benchmark import Policy, RunResult, summarize
from sil.diagnostics_report import validate_benchmark_report
from sil.diagnostics_run import Guards, RunOutcome

FIXTURES = ROOT / "tests" / "fixtures" / "diagnostics"
MANIFEST = Path("/work/mixed.json")


def example(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def run_report(wall_ns: int, *, failed: bool = False) -> dict:
    report = example("run-failed-partial.json" if failed
                     else "run-success.json")
    report["observational"]["run_wall_time"]["wall_ns"] = wall_ns
    return report


def result(label: str, report: dict | None, *, exit_code: int | None = None,
           signal: int | None = None) -> RunResult:
    role = "warmup" if label.startswith("warmup") else "measured"
    if exit_code is None and signal is None:
        exit_code = report["run"]["exit_code"]
    outcome = RunOutcome(
        exit_code=exit_code, signal=signal, launcher_error=None,
        report=report,
        report_problem=None if report else "no diagnostics report was written",
        report_path=Path(f"/out/{label}/run.mcap.diagnostics.json"))
    return RunResult(label, role, int(label[-3:]), label, outcome)


def policy(repeats: int = 3, warmup: int = 1) -> Policy:
    return Policy(repeats=repeats, warmup=warmup, recording=True,
                  guards=Guards())


def metric(summary: dict, name: str) -> dict:
    [found] = [m for m in summary["aggregates"]["metrics"]
               if m["metric"] == name]
    return found


class TestAggregation:
    def test_statistics_use_only_successful_measured_runs(self):
        runs = [result("warmup-001", run_report(900_000_000)),
                result("run-001", run_report(10_000_000)),
                result("run-002", run_report(60_000_000)),
                result("run-003", run_report(20_000_000))]

        summary = summarize(policy(), MANIFEST, runs)

        assert validate_benchmark_report(summary) == []
        wall = metric(summary, "run_wall_ns")
        assert wall["unit"] == "ns"
        assert wall["source"] == "observational.run_wall_time.wall_ns"
        assert wall["per_run"] == [
            {"label": "run-001", "value": 10_000_000},
            {"label": "run-002", "value": 60_000_000},
            {"label": "run-003", "value": 20_000_000}]
        assert (wall["min"], wall["median"], wall["max"]) == (
            10_000_000, 20_000_000, 60_000_000)
        assert wall["mean"] == 30_000_000.0
        # sqrt(((10-30)^2 + (60-30)^2 + (20-30)^2) / 3) ms, population.
        assert wall["stddev"] == pytest.approx(21_602_468.994692867)
        assert wall["unavailable"] == {}
        assert summary["aggregates"]["sample_count"] == 3
        assert summary["aggregates"]["scope"] == "successful_measured_runs"

    def test_one_successful_run_has_zero_spread(self):
        summary = summarize(policy(repeats=1, warmup=0), MANIFEST,
                            [result("run-001", run_report(7_000_000))])

        wall = metric(summary, "run_wall_ns")
        assert (wall["min"], wall["median"], wall["max"], wall["mean"],
                wall["stddev"]) == (7_000_000, 7_000_000, 7_000_000,
                                    7_000_000.0, 0.0)
        assert summary["status"] == {"complete": True, "exit_code": 0,
                                     "stopped_reason": None}

    def test_even_count_median_is_the_mean_of_the_middle_two(self):
        runs = [result(f"run-00{i}", run_report(wall))
                for i, wall in enumerate((4, 1, 3, 10), start=1)]

        wall = metric(summarize(policy(repeats=4, warmup=0), MANIFEST, runs),
                      "run_wall_ns")

        assert wall["median"] == 3.5

    def test_no_successful_run_gives_null_statistics_with_a_reason(self):
        runs = [result("warmup-001", None, signal=9)]

        summary = summarize(policy(repeats=5), MANIFEST, runs)

        assert validate_benchmark_report(summary) == []
        assert summary["aggregates"]["sample_count"] == 0
        for found in summary["aggregates"]["metrics"]:
            assert found["per_run"] == []
            assert all(found[key] is None for key in
                       ("min", "median", "max", "mean", "stddev"))
            assert set(found["unavailable"].values()) == {
                "no_successful_measured_runs"}
        assert summary["status"] == {"complete": False, "exit_code": 137,
                                     "stopped_reason": "warmup_failed"}
        assert summary["runs"][0]["run_exit_code"] is None
        assert summary["runs"][0]["process_outcome"] == {"exit_code": None,
                                                         "signal": 9}
        assert summary["runs"][0]["report_status"] == "missing"
        # Without any Run report, identity and host are honestly unknown.
        assert summary["identity"]["manifest_hash"] is None
        assert summary["identity"]["unavailable"]["manifest_hash"] == \
            "no_run_report"
        assert summary["host"]["os"] is None

    def test_a_failed_measured_run_is_kept_but_never_aggregated(self):
        runs = [result("warmup-001", run_report(1)),
                result("run-001", run_report(5_000_000)),
                result("run-002", run_report(99_000_000, failed=True))]

        summary = summarize(policy(repeats=5), MANIFEST, runs)

        assert validate_benchmark_report(summary) == []
        assert summary["status"] == {"complete": False, "exit_code": 1,
                                     "stopped_reason": "run_failed"}
        assert summary["counts"] == {
            "requested": 5, "attempted": 2, "completed": 1, "failed": 1,
            "warmups_requested": 1, "warmups_attempted": 1,
            "warmups_completed": 1, "warmups_failed": 0}
        assert metric(summary, "run_wall_ns")["per_run"] == [
            {"label": "run-001", "value": 5_000_000}]
        assert summary["runs"][2]["report"]["run"]["exit_code"] == 1

    def test_a_run_that_succeeded_without_a_report_counts_as_failed(self):
        runs = [result("run-001", None, exit_code=0)]

        summary = summarize(policy(repeats=2, warmup=0), MANIFEST, runs)

        assert summary["status"]["exit_code"] == 1
        assert summary["counts"]["failed"] == 1
        assert summary["aggregates"]["sample_count"] == 0

    def test_interruption_marks_the_campaign_incomplete(self):
        runs = [result("run-001", run_report(5))]

        summary = summarize(policy(repeats=3, warmup=0), MANIFEST, runs,
                            interrupt_signal=2)

        assert summary["status"] == {"complete": False, "exit_code": 130,
                                     "stopped_reason": "interrupted"}

    def test_an_interrupted_run_keeps_its_own_exit_code(self):
        runs = [result("run-001", run_report(5, failed=True))]

        summary = summarize(policy(repeats=3, warmup=0), MANIFEST, runs,
                            interrupt_signal=2)

        assert summary["status"]["exit_code"] == 1
        assert summary["status"]["stopped_reason"] == "interrupted"

    def test_a_metric_missing_in_one_run_has_no_statistics(self):
        missing = run_report(5)
        speed = missing["observational"]["virtual_time_speed"]
        speed["scheduled_factor"] = None
        speed["unavailable"] = {"scheduled_factor": "no_scheduled_execution"}
        runs = [result("run-001", run_report(5)), result("run-002", missing)]

        found = metric(summarize(policy(repeats=2, warmup=0), MANIFEST, runs),
                       "scheduled_factor")

        assert found["per_run"][1] == {"label": "run-002", "value": None}
        assert found["mean"] is None
        assert found["unavailable"]["mean"] == "unavailable_in_a_measured_run"

    def test_process_memory_is_per_participant_and_never_summed(self):
        runs = [result("run-001", run_report(5)),
                result("run-002", run_report(6))]

        summary = summarize(policy(repeats=2, warmup=0), MANIFEST, runs)

        rss = [m for m in summary["aggregates"]["metrics"]
               if m["metric"] == "process_peak_rss_bytes"]
        assert [m["participant"] for m in rss] == ["pyecho"]
        assert rss[0]["per_run"][0]["value"] == 14 * 2**20
        names = [m["metric"] for m in summary["aggregates"]["metrics"]]
        assert not any("rss" in name and "total" in name for name in names)

    def test_activation_percentiles_are_not_pooled_across_runs(self):
        # Runs keep their own distributions and throughput denominators; no
        # cross-Run percentile exists among the aggregates.
        own = run_report(5)
        runs = [result("run-001", own), result("run-002", run_report(6))]

        summary = summarize(policy(repeats=2, warmup=0), MANIFEST, runs)

        names = [m["metric"] for m in summary["aggregates"]["metrics"]]
        assert not any(name.startswith("p") and name[1:3].isdigit()
                       or "_p9" in name or "_p5" in name for name in names)
        assert summary["runs"][0]["report"]["observational"] == \
            own["observational"]
        assert metric(summary, "scheduled_execution_ns")["per_run"][0][
            "value"] == own["observational"]["throughput"][
            "scheduled_execution_ns"]

    def test_policy_reports_the_guards_the_runner_used(self):
        summary = summarize(policy(repeats=1, warmup=0), MANIFEST,
                            [result("run-001", run_report(5))])

        assert summary["policy"] == {
            "repeats": 1, "warmup": 0, "recording": True,
            "execution": "serial_fresh_processes",
            "participant_timeout_ms": None,
            "max_protocol_line_bytes": 16777216,
            "max_step_output_messages": 1024,
            "max_step_inline_payload_bytes": 67108864}
        assert summary["identity"]["diagnostics_collector_version"] == 2
