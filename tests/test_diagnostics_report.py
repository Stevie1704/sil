"""The frozen `sil_run_diagnostics` and `sil_benchmark_report` v1 examples (#300).

tests/fixtures/diagnostics/ holds one example per case downstream consumers
must handle. Each one is checked against sil.diagnostics_report, and each
violation the validator exists to catch is checked against a copy of one.
"""

import copy
import json
from pathlib import Path

import pytest

from conftest import ROOT
from sil.diagnostics_report import (
    ReportError,
    load_report,
    parse_report,
    validate_benchmark_report,
    validate_run_report,
)

FIXTURES = ROOT / "tests" / "fixtures" / "diagnostics"
RUN_EXAMPLES = (
    "run-success.json",
    "run-zero-activations.json",
    "run-failed-partial.json",
    "run-unsupported-metric.json",
)
BENCHMARK_EXAMPLES = (
    "benchmark-complete.json",
    "benchmark-partial.json",
    "benchmark-no-successful-run.json",
)


def example(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def all_null_reasons(value, found=None):
    found = [] if found is None else found
    if isinstance(value, dict):
        found.extend(value.get("unavailable", {}).values())
        for item in value.values():
            all_null_reasons(item, found)
    elif isinstance(value, list):
        for item in value:
            all_null_reasons(item, found)
    return found


@pytest.mark.parametrize("name", RUN_EXAMPLES + BENCHMARK_EXAMPLES)
def test_every_example_is_a_valid_report(name):
    load_report(FIXTURES / name)


class TestRunExamples:
    def test_success_is_complete(self):
        report = example("run-success.json")
        assert report["run"] == {
            "exit_code": 0, "status": "succeeded", "failure": None
        }
        assert report["completeness"]["run_complete"] is True

    def test_zero_activations_has_no_statistics(self):
        report = example("run-zero-activations.json")
        (row,) = report["observational"]["tasks"]
        assert row["timing"]["completed"] == 0
        assert row["timing"]["p50_ns"] is None

    def test_failed_run_is_partial(self):
        report = example("run-failed-partial.json")
        assert report["run"]["exit_code"] == 1
        assert report["completeness"]["run_complete"] is False
        assert report["observational"]["virtual_time_speed"]["scheduled_factor"] is None

    def test_unsupported_metric_is_null_with_a_reason(self):
        report = example("run-unsupported-metric.json")
        (child,) = report["observational"]["resources"]["process_participants"]
        assert child["peak_rss_bytes"] is None
        assert child["unavailable"]["peak_rss_bytes"] == "unsupported_platform"
        resources = report["observational"]["resources"]
        assert resources["runner"]["user_cpu_ns"] is None
        # A total over scopes with a missing member is missing, not partial.
        assert resources["cpu_total"]["user_cpu_ns"] is None
        assert resources["cpu_total"]["unavailable"]["user_cpu_ns"] == (
            "incomplete_scopes")
        assert "unsupported_platform" in all_null_reasons(report)


class TestBenchmarkExamples:
    def test_warmups_never_enter_aggregates(self):
        report = example("benchmark-complete.json")
        assert report["counts"]["warmups_attempted"] == 1
        assert report["aggregates"]["sample_count"] == 2
        wall = report["aggregates"]["metrics"][0]
        assert wall["metric"] == "run_wall_ns"
        assert [entry["label"] for entry in wall["per_run"]] == [
            "run-001", "run-002"
        ]
        # The two measured Runs took 61 and 63 ms; the warm-up took 70.
        assert (wall["min"], wall["median"], wall["max"]) == (
            61_000_000, 62_000_000, 63_000_000
        )
        assert wall["stddev"] == 1_000_000

    def test_every_run_embeds_its_report_for_offline_rendering(self):
        report = example("benchmark-complete.json")
        for run in report["runs"]:
            assert run["report"]["sil_run_diagnostics"] == 1

    def test_partial_campaign_is_marked_incomplete(self):
        report = example("benchmark-partial.json")
        assert report["status"] == {
            "complete": False, "exit_code": 1, "stopped_reason": "run_failed"
        }
        assert report["counts"]["requested"] == 5
        assert report["counts"]["attempted"] == 2
        assert report["aggregates"]["sample_count"] == 1

    def test_no_successful_run_has_no_aggregate_values(self):
        report = example("benchmark-no-successful-run.json")
        (run,) = report["runs"]
        assert run["report"] is None and run["report_status"] == "missing"
        assert report["aggregates"]["sample_count"] == 0
        for metric in report["aggregates"]["metrics"]:
            assert metric["per_run"] == []
            assert metric["median"] is None
            assert metric["unavailable"]["median"] == (
                "no_successful_measured_runs"
            )


class TestRejections:
    def test_unknown_version_is_rejected(self):
        report = example("run-success.json")
        report["sil_run_diagnostics"] = 2
        assert validate_run_report(report) == [
            "unsupported sil_run_diagnostics version 2"
        ]

    def test_boolean_version_is_rejected(self):
        report = example("run-success.json")
        report["sil_run_diagnostics"] = True
        assert validate_run_report(report)

    def test_missing_discriminator_is_rejected(self):
        report = example("run-success.json")
        del report["sil_run_diagnostics"]
        assert validate_run_report(report) == [
            "missing discriminator 'sil_run_diagnostics'"
        ]

    def test_nan_is_rejected(self):
        text = (FIXTURES / "run-success.json").read_text().replace(
            '"mean_ns": ', '"mean_ns": NaN, "was": ', 1
        )
        with pytest.raises(ReportError, match="NaN"):
            parse_report(text)

    def test_corrupt_json_is_rejected(self, tmp_path: Path):
        path = tmp_path / "corrupt.json"
        path.write_text('{"sil_run_diagnostics": 1,')
        with pytest.raises(ReportError, match="not valid JSON"):
            load_report(path)

    def test_missing_file_is_rejected(self, tmp_path: Path):
        with pytest.raises(ReportError, match="cannot read"):
            load_report(tmp_path / "absent.json")

    def test_null_without_reason_is_rejected(self):
        report = example("run-zero-activations.json")
        del report["observational"]["tasks"][0]["timing"]["unavailable"]["p50_ns"]
        assert any("p50_ns is null without a reason" in e
                   for e in validate_run_report(report))

    def test_reason_for_a_present_value_is_rejected(self):
        report = example("run-success.json")
        report["observational"]["phases"]["unavailable"]["setup_ns"] = "x"
        assert any("names non-null 'setup_ns'" in e
                   for e in validate_run_report(report))

    def test_counts_that_do_not_add_up_are_rejected(self):
        report = example("run-success.json")
        report["execution"]["participants"][0]["tasks"][0]["attempted"] += 1
        assert any("attempted is not" in e for e in validate_run_report(report))

    def test_unbounded_samples_are_rejected(self):
        report = example("run-success.json")
        timing = report["observational"]["tasks"][0]["timing"]
        timing["samples_ns"] = [1] * 8193
        timing["quantiles"]["retained_count"] = 8193
        assert any("more than 8192" in e for e in validate_run_report(report))

    def test_cpu_total_that_is_not_the_disjoint_sum_is_rejected(self):
        report = example("run-success.json")
        report["observational"]["resources"]["cpu_total"]["user_cpu_ns"] += 1
        assert any("cpu_total" in e for e in validate_run_report(report))

    def test_summed_peak_memory_is_rejected(self):
        """Peaks of different processes need not coincide: no total."""
        report = example("run-success.json")
        report["observational"]["resources"]["total_peak_rss_bytes"] = 1
        assert any("peak" in e for e in validate_run_report(report))

    def test_status_that_contradicts_the_exit_code_is_rejected(self):
        report = example("run-failed-partial.json")
        report["run"]["status"] = "succeeded"
        assert validate_run_report(report)

    def test_aggregate_that_includes_a_warmup_is_rejected(self):
        report = example("benchmark-complete.json")
        report["aggregates"]["sample_count"] = 3
        assert any("sample_count" in e
                   for e in validate_benchmark_report(report))

    def test_invalid_embedded_run_report_is_rejected(self):
        report = example("benchmark-complete.json")
        broken = copy.deepcopy(report)
        broken["runs"][1]["report"]["sil_run_diagnostics"] = 9
        assert any(e.startswith("runs[1].report")
                   for e in validate_benchmark_report(broken))
