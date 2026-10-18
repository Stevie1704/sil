"""`sil benchmark`: repeated fresh Runs and their `sil_benchmark_report` (#303).

The aggregation tests build Run outcomes from the frozen report examples with
chosen wall times, so every expected statistic is a worked literal and no
assertion depends on how long anything took.
"""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT
from sil.benchmark import Policy, RunResult, summarize
from sil.diagnostics_html import render
from sil.diagnostics_report import load_report, validate_benchmark_report
from sil.diagnostics_run import Guards, RunOutcome
from test_process_descendants import (
    _wait_for_observer, assert_cleanup, descendant_manifest,
)
from test_run_diagnostics import MS, add_multitask
from test_run_diagnostics_cli import fake_tools, native_path, sil  # noqa: F401
from toys import toy_manifest

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

        assert summary["status"] == {"complete": False, "exit_code": 1,
                                     "stopped_reason": "report_failed"}
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

    def test_a_participant_seen_only_in_a_later_run_is_kept(self):
        later = run_report(6)
        (record,) = later["observational"]["resources"]["process_participants"]
        later["observational"]["resources"]["process_participants"].append(
            {**record, "participant": "late", "peak_rss_bytes": 3 * 2**20})
        runs = [result("run-001", run_report(5)), result("run-002", later)]

        summary = summarize(policy(repeats=2, warmup=0), MANIFEST, runs)

        rss = {m["participant"]: m for m in summary["aggregates"]["metrics"]
               if m["metric"] == "process_peak_rss_bytes"}
        assert sorted(rss) == ["late", "pyecho"]
        assert rss["late"]["per_run"] == [
            {"label": "run-001", "value": None},
            {"label": "run-002", "value": 3 * 2**20}]
        assert rss["late"]["unavailable"]["mean"] == \
            "unavailable_in_a_measured_run"

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


def resummarize(example_report: dict) -> dict:
    """What `summarize` writes for the Runs an example benchmark embeds."""
    runs = []
    for run in example_report["runs"]:
        outcome = RunOutcome(
            exit_code=run["process_outcome"]["exit_code"],
            signal=run["process_outcome"]["signal"], launcher_error=None,
            report=run["report"],
            report_problem=None if run["report"] else "missing",
            report_path=Path(run["report_path"]))
        runs.append(RunResult(run["label"], run["role"], run["index"],
                              run["directory"], outcome))
    given = example_report["policy"]
    guards = Guards(given["participant_timeout_ms"],
                    given["max_protocol_line_bytes"],
                    given["max_step_output_messages"],
                    given["max_step_inline_payload_bytes"])
    summary = summarize(
        Policy(given["repeats"], given["warmup"], given["recording"], guards),
        Path(example_report["identity"]["manifest_path"]), runs)
    summary["generator"] = example_report["generator"]  # the release version
    return summary


@pytest.mark.parametrize("name", ["benchmark-complete.json",
                                  "benchmark-partial.json",
                                  "benchmark-no-successful-run.json"])
def test_frozen_examples_are_what_the_command_writes(name):
    report = example(name)

    assert resummarize(report) == report


# --- the command, with controlled fake runners -----------------------------

SUCCESS = FIXTURES / "run-success.json"
FAILURE = FIXTURES / "run-failed-partial.json"


def runner_script(counter: Path, fail_at: int | None = None) -> str:
    """A fake runner: notes its cwd and arguments, then writes a report.

    Invocation `fail_at` (one-based) and later ones fail with exit 1.
    """
    fail = "false" if fail_at is None else f'[ "$n" -ge {fail_at} ]'
    return (
        'for a; do last="$a"; done\n'
        f'n=$(cat "{counter}" 2>/dev/null || echo 0); n=$((n+1)); '
        f'echo "$n" > "{counter}"\n'
        'printf "%s\\n" "$PWD" "$@" > args.txt\n'
        'echo "manifest_hash fake"\n'
        f'if {fail}; then echo "run $n failed" >&2; '
        f'cp "{FAILURE}" "$last"; exit 1; fi\n'
        f'cp "{SUCCESS}" "$last"\nexit 0\n')


def invocations(counter: Path) -> int:
    return int(counter.read_text()) if counter.exists() else 0


def run_arguments(directory: Path) -> list[str]:
    return (directory / "args.txt").read_text().splitlines()


class TestCommand:
    def test_default_campaign_is_one_warmup_and_five_runs(self, build_dir,
                                                          tmp_path):
        counter = tmp_path / "count"
        path = fake_tools(tmp_path, build_dir, runner_script(counter))

        proc = sil(["benchmark", "m.json", "--out", "results"], path,
                   tmp_path)

        assert proc.returncode == 0, proc.stderr
        out = tmp_path / "results"
        summary = load_report(out / "summary.json")
        assert [run["label"] for run in summary["runs"]] == [
            "warmup-001", "run-001", "run-002", "run-003", "run-004",
            "run-005"]
        assert summary["status"]["complete"] is True
        assert summary["aggregates"]["sample_count"] == 5
        for run in summary["runs"]:
            directory = out / run["directory"]
            assert run["report_path"] == \
                f"{run['label']}/run.mcap.diagnostics.json"
            assert (out / run["report_path"]).is_file()
            assert (directory / "stdout.log").read_text() == \
                "manifest_hash fake\n"
            assert (directory / "stderr.log").read_text() == ""
        assert sorted(p.name for p in out.iterdir()) == sorted(
            [run["label"] for run in summary["runs"]] + ["summary.json"])
        assert "results/summary.json" in proc.stdout
        assert "5 of 5 measured Runs succeeded" in proc.stdout

    def test_every_run_gets_the_same_manifest_options_and_its_own_cwd(
        self, build_dir, tmp_path
    ):
        counter = tmp_path / "count"
        path = fake_tools(tmp_path, build_dir, runner_script(counter))

        proc = sil(["benchmark", "project/m.json", "--repeats", "2",
                    "--warmup", "0", "--out", "results",
                    "--participant-timeout-ms", "5000",
                    "--max-step-output-messages", "7"], path, tmp_path)

        assert proc.returncode == 0, proc.stderr
        out = (tmp_path / "results").resolve()
        for label in ("run-001", "run-002"):
            cwd, *argv = run_arguments(out / label)
            assert Path(cwd).resolve() == out / label
            assert [Path(argv[0]).resolve(), *argv[1:]] == [
                (tmp_path / "project" / "m.json").resolve(),
                "-o", str(out / label / "run.mcap"),
                "--participant-timeout-ms", "5000",
                "--max-step-output-messages", "7",
                "--diagnostics-out",
                str(out / label / "run.mcap.diagnostics.json")]

    def test_no_recording_is_forwarded(self, build_dir, tmp_path):
        counter = tmp_path / "count"
        path = fake_tools(tmp_path, build_dir, runner_script(counter))

        proc = sil(["benchmark", "m.json", "--repeats", "1", "--warmup", "0",
                    "--no-recording", "--out", "results"], path, tmp_path)

        assert proc.returncode == 0, proc.stderr
        out = (tmp_path / "results").resolve()
        _cwd, *argv = run_arguments(out / "run-001")
        assert "--no-recording" in argv and "-o" not in argv
        assert argv[-1] == str(out / "run-001" / "out.mcap.diagnostics.json")
        summary = load_report(out / "summary.json")
        assert summary["policy"]["recording"] is False

    def test_a_failed_warmup_stops_before_any_measured_run(self, build_dir,
                                                           tmp_path):
        counter = tmp_path / "count"
        path = fake_tools(tmp_path, build_dir, runner_script(counter, 1))

        proc = sil(["benchmark", "m.json", "--out", "results"], path,
                   tmp_path)

        assert proc.returncode == 1
        assert invocations(counter) == 1
        out = tmp_path / "results"
        assert sorted(p.name for p in out.iterdir()) == [
            "summary.json", "warmup-001"]
        summary = load_report(out / "summary.json")
        assert summary["status"] == {"complete": False, "exit_code": 1,
                                     "stopped_reason": "warmup_failed"}
        assert summary["aggregates"]["sample_count"] == 0
        # The Run's own diagnostic reaches the user and its log.
        assert "run 1 failed" in proc.stderr
        assert (out / "warmup-001" / "stderr.log").read_text() == \
            "run 1 failed\n"
        assert "warmup-001" in proc.stderr

    def test_a_later_failed_run_keeps_earlier_results_and_stops(
        self, build_dir, tmp_path
    ):
        counter = tmp_path / "count"
        path = fake_tools(tmp_path, build_dir, runner_script(counter, 3))

        proc = sil(["benchmark", "m.json", "--repeats", "4", "--out",
                    "results"], path, tmp_path)

        assert proc.returncode == 1
        assert invocations(counter) == 3
        summary = load_report(tmp_path / "results" / "summary.json")
        assert [run["label"] for run in summary["runs"]] == [
            "warmup-001", "run-001", "run-002"]
        assert summary["status"]["stopped_reason"] == "run_failed"
        assert summary["counts"]["attempted"] == 2
        assert summary["counts"]["failed"] == 1
        assert summary["aggregates"]["sample_count"] == 1
        assert not (tmp_path / "results" / "run-003").exists()
        assert "incomplete" in proc.stdout

    def test_a_signalled_runner_propagates_128_plus_the_signal(
        self, build_dir, tmp_path
    ):
        path = fake_tools(tmp_path, build_dir, "kill -KILL $$\n")

        proc = sil(["benchmark", "m.json", "--warmup", "0", "--out", "r"],
                   path, tmp_path)

        assert proc.returncode == 137
        summary = load_report(tmp_path / "r" / "summary.json")
        assert summary["runs"][0]["report_status"] == "missing"
        assert summary["runs"][0]["process_outcome"]["signal"] == 9

    def test_an_existing_out_directory_is_rejected_untouched(
        self, build_dir, tmp_path
    ):
        counter = tmp_path / "count"
        path = fake_tools(tmp_path, build_dir, runner_script(counter))
        (tmp_path / "results").mkdir()
        (tmp_path / "results" / "keep.txt").write_text("earlier")

        proc = sil(["benchmark", "m.json", "--out", "results"], path,
                   tmp_path)

        assert proc.returncode == 2
        assert "already exists" in proc.stderr
        assert invocations(counter) == 0
        assert [p.name for p in (tmp_path / "results").iterdir()] == [
            "keep.txt"]

    @pytest.mark.parametrize("argv, message", [
        (["--repeats", "0"], "--repeats"),
        (["--repeats", "x"], "--repeats"),
        (["--warmup", "-1"], "--warmup"),
        (["--repeats", "2", "--repeats", "3"], "only once"),
        (["-o", "x.mcap"], "unrecognized"),
        (["--participant-timeout-ms", "0"], "--participant-timeout-ms"),
        (["--html", "a.html", "--html", "b.html"], "only once"),
        (["--html", "taken.html"], "HTML report 'taken.html' already exists"),
        (["--html", "elsewhere/r.html"], "does not exist"),
        (["--html", "results"], "is the --out directory"),
        (["--html", "results/summary.json"], "is also the benchmark summary"),
        (["--html", "results/run-001"], "is also a Run directory"),
        (["--html", "results/run-001/r.html"], "does not exist"),
    ])
    def test_invalid_options_exit_2_before_any_run(self, build_dir, tmp_path,
                                                   argv, message):
        counter = tmp_path / "count"
        path = fake_tools(tmp_path, build_dir, runner_script(counter))
        (tmp_path / "taken.html").write_text("keep")

        proc = sil(["benchmark", "m.json", "--out", "results", *argv], path,
                   tmp_path)

        assert proc.returncode == 2
        assert message in proc.stderr
        assert invocations(counter) == 0
        assert not (tmp_path / "results").exists()

    @pytest.mark.parametrize("html", ["results/report.html", "report.html"])
    def test_html_presents_the_written_summary(self, build_dir, tmp_path,
                                               html):
        counter = tmp_path / "count"
        path = fake_tools(tmp_path, build_dir, runner_script(counter))

        proc = sil(["benchmark", "m.json", "--repeats", "2", "--out",
                    "results", "--html", html], path, tmp_path)

        assert proc.returncode == 0, proc.stderr
        summary = (tmp_path / "results" / "summary.json").read_text()
        assert (tmp_path / html).read_text() == render(summary)
        assert proc.stdout.endswith(f"HTML report: {html}\n")

    def test_html_of_a_failed_campaign_keeps_its_exit_code(self, build_dir,
                                                           tmp_path):
        counter = tmp_path / "count"
        path = fake_tools(tmp_path, build_dir, runner_script(counter, 2))

        proc = sil(["benchmark", "m.json", "--out", "results", "--html",
                    "results/report.html"], path, tmp_path)

        assert proc.returncode == 1
        page = (tmp_path / "results" / "report.html").read_text()
        assert "Incomplete benchmark: run failed" in page

    @pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
    def test_an_unwritable_html_report_exits_1_after_good_runs(
        self, build_dir, tmp_path
    ):
        counter = tmp_path / "count"
        path = fake_tools(tmp_path, build_dir, runner_script(counter))
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0o555)
        try:
            proc = sil(["benchmark", "m.json", "--repeats", "1", "--out",
                        "results", "--html", "locked/r.html"], path, tmp_path)
        finally:
            locked.chmod(0o755)

        assert proc.returncode == 1
        assert ("sil benchmark: cannot write the HTML report "
                "'locked/r.html': ") in proc.stderr
        assert load_report(tmp_path / "results" / "summary.json")[
            "status"]["exit_code"] == 0
        assert list(locked.iterdir()) == []

    def test_out_is_required(self, build_dir, tmp_path):
        path = fake_tools(tmp_path, build_dir, "exit 0\n")

        proc = sil(["benchmark", "m.json"], path, tmp_path)

        assert proc.returncode == 2
        assert "--out" in proc.stderr

    def test_a_missing_native_tool_exits_127_without_output(self, tmp_path):
        proc = sil(["benchmark", "m.json", "--out", "results"], tmp_path,
                   tmp_path)

        assert proc.returncode == 127
        assert "sil-run-diagnostics" in proc.stderr
        assert not (tmp_path / "results").exists()


# --- real Runs ---------------------------------------------------------------


def fresh_state_manifest(directory: Path) -> Path:
    """Native Tasks, and a Process participant that publishes its call count."""
    m = toy_manifest(duration_ns=30 * MS)
    m.add_channel("ticks", schema="toy.Counter")
    m.add_channel("calls", schema="toy.Counter")
    add_multitask(m, fast_period_ns=1 * MS, slow_period_ns=5 * MS)
    m.add_process(
        "counter",
        command=[sys.executable,
                 str(ROOT / "tests" / "participants" / "call_counter.py")],
        step_period_ns=10 * MS, publishes=["calls"])
    return m.write(directory / "fresh.json").path


class TestRealRuns:
    def test_every_run_starts_fresh_and_records_what_one_run_records(
        self, native_path, tmp_path
    ):
        project = tmp_path / "my project"
        project.mkdir()
        fresh_state_manifest(project)
        single = sil(["run", "my project/fresh.json", "-o", "single.mcap"],
                     native_path, tmp_path)
        assert single.returncode == 0, single.stderr

        proc = sil(["benchmark", "my project/fresh.json", "--repeats", "2",
                    "--warmup", "1", "--out", "bench"], native_path, tmp_path)

        assert proc.returncode == 0, proc.stderr
        out = tmp_path / "bench"
        summary = load_report(out / "summary.json")
        assert summary["status"]["complete"] is True
        expected = (tmp_path / "single.mcap").read_bytes()
        # A reused Participant would continue its call count, and the later
        # Recordings would differ from a single fresh Run.
        for label in ("warmup-001", "run-001", "run-002"):
            assert (out / label / "run.mcap").read_bytes() == expected
        tasks = {(row["participant"], row["task"])
                 for row in summary["runs"][1]["report"]["observational"]
                 ["tasks"]}
        assert tasks == {("multi", "fast"), ("multi", "slow"),
                         ("counter", "step")}
        # Only the Run's evidence is left; no Run working directory remains.
        for label in ("warmup-001", "run-001", "run-002"):
            assert sorted(p.name for p in (out / label).iterdir()) == [
                "run.mcap", "run.mcap.diagnostics.json",
                "run.mcap.provenance.json", "stderr.log", "stdout.log"]

    def test_a_signal_stops_the_campaign_and_launches_no_later_run(
        self, native_path, tmp_path
    ):
        regions = tmp_path / "regions"
        regions.mkdir()
        observer = tmp_path / "observer.json"
        descendant_manifest("descendant-timeout", observer).write(
            tmp_path / "run.json")
        env = {**os.environ, "TMPDIR": str(regions),
               "PATH": os.pathsep.join([str(native_path), "/usr/bin:/bin"])}
        command = subprocess.Popen(
            [sys.executable, "-m", "sil.cli", "benchmark", "run.json",
             "--warmup", "0", "--repeats", "3", "--out", "bench"],
            cwd=tmp_path, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        _wait_for_observer(observer)

        command.send_signal(signal.SIGINT)
        stdout, stderr = command.communicate(timeout=30)

        assert "run interrupted" in stderr
        assert_cleanup(command, observer, regions, expected_code=1,
                       stderr=stderr)
        out = tmp_path / "bench"
        assert sorted(p.name for p in out.iterdir()) == ["run-001",
                                                         "summary.json"]
        summary = load_report(out / "summary.json")
        assert summary["status"] == {"complete": False, "exit_code": 1,
                                     "stopped_reason": "interrupted"}
        assert summary["runs"][0]["report"]["run"]["status"] == "interrupted"
