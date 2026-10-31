"""The page of a `sil_benchmark_report` (#304).

Every value comes from summary.json: the aggregates, the per-Run values and
the embedded Run reports. Nothing is computed again.
"""

from __future__ import annotations

from typing import Any

from sil.diagnostics_format import provenance_mode_text
from sil.diagnostics_html_charts import bar_chart
from sil.diagnostics_html_markup import (
    ICONS, REASONS, Html, banner, card, code, definition_list,
    identity_value, join, measured, number, plain, section, table, tag,
    unavailable,
)
from sil.diagnostics_html_run import (
    STATUS, guard_items, host_list, methodology, report_reasons, run_banner,
    run_body,
)
from sil.diagnostics_report import process_participant_names, process_record


METRIC_MEANINGS = {
    "run_wall_ns":
        "Run wall time: host time from before the Manifest is loaded through "
        "Participant cleanup and Recording close.",
    "preflight_ns":
        "Preflight phase: Manifest load, Run preparation and Recording open.",
    "setup_ns":
        "Setup phase: Engine construction and the init or spawn of each "
        "Participant.",
    "scheduled_execution_ns":
        "Scheduled execution phase: the Slot loop that runs every Task and "
        "Step. It is the denominator of the throughput metrics.",
    "shutdown_ns":
        "Shutdown phase: Process participant shutdown, Run directory "
        "removal and Engine teardown.",
    "recording_finalization_ns":
        "Recording finalization phase: the Recording close.",
    "unattributed_ns":
        "Run wall time that is in no phase. With the phases, it adds up to "
        "run_wall_ns.",
    "scheduled_factor":
        "Manifest Duration divided by scheduled-execution time. A value "
        "above 1 means faster than real time during the Slot loop.",
    "end_to_end_factor":
        "Manifest Duration divided by Run wall time, setup and shutdown "
        "included.",
    "native_activations_per_s":
        "Completed Native Task activations per second of scheduled-execution "
        "time.",
    "process_steps_per_s":
        "Completed Process Steps per second of scheduled-execution time.",
    "runner_user_cpu_ns":
        "CPU time of the native runner in user mode, all threads, no child "
        "process. Native participants run in the runner, so their cost is "
        "here.",
    "runner_system_cpu_ns":
        "CPU time of the native runner in the kernel (system calls, I/O, "
        "IPC), all threads, no child process.",
    "runner_peak_rss_bytes":
        "Highest resident memory of the runner process during the Run.",
    "cpu_total_user_cpu_ns":
        "User-mode CPU time of the runner plus every Process participant. "
        "These scopes do not overlap. It can be more than the wall time.",
    "cpu_total_system_cpu_ns":
        "Kernel-mode CPU time of the runner plus every Process participant.",
    "process_user_cpu_ns":
        "User-mode CPU time of the named Process participant, from spawn to "
        "reap, with the descendants it waited for. This is the time its own "
        "code ran.",
    "process_system_cpu_ns":
        "Kernel-mode CPU time of the named Process participant, from spawn "
        "to reap, with the descendants it waited for: system calls such as "
        "the pipe I/O of each Step.",
    "process_peak_rss_bytes":
        "Highest resident memory of the largest single process among the "
        "named Process participant and its waited descendants. It is not a "
        "sum.",
}


def _successful(run: dict) -> bool:
    """docs/diagnostics-report.md: exit 0 and a valid report."""
    return (run["role"] == "measured" and run["run_exit_code"] == 0
            and run["report"] is not None)


def _run_outcome(run: dict) -> tuple[str, str]:
    """(text, tone) of one attempted Run."""
    report = run["report"]
    outcome = run["process_outcome"]
    if report is None:
        ended = (f"signal {outcome['signal']}" if outcome["signal"] is not None
                 else f"exit code {outcome['exit_code']}")
        if outcome["exit_code"] == 0:
            return ("Failed: exited 0 but wrote no diagnostics report", "fail")
        return (f"Failed: ended by {ended}, no diagnostics report", "fail")
    if run["run_exit_code"] == 0:
        return ("Succeeded", "ok")
    label = STATUS.get(report["run"]["status"], (report["run"]["status"],))[0]
    return (f"Failed: {label}, exit code {run['run_exit_code']}", "fail")


def benchmark_page(summary: dict) -> Html:
    return join([
        _benchmark_banner(summary),
        _benchmark_overview(summary),
        _benchmark_identity(summary),
        _runs_section(summary),
        _run_charts(summary),
        _aggregates(summary),
        _run_details(summary),
        methodology(report_reasons(summary), benchmark=True),
        _metric_meanings(summary),
    ])


def _benchmark_banner(summary: dict) -> Html:
    status, counts = summary["status"], summary["counts"]
    if status["complete"]:
        label, tone = "Complete benchmark", "ok"
    else:
        reason = status["stopped_reason"] or "not all Runs attempted"
        label = "Incomplete benchmark: " + reason.replace("_", " ")
        tone = "fail" if status["exit_code"] else "warn"
    lines = [
        tag("p", f"Exit code {status['exit_code']} · ",
            f"{counts['completed']} of {counts['requested']} measured Runs "
            f"succeeded ({counts['attempted']} attempted, "
            f"{counts['failed']} failed) · "
            f"{counts['warmups_completed']} of {counts['warmups_requested']} "
            f"warm-ups succeeded ({counts['warmups_failed']} failed), never "
            "aggregated", class_="banner-detail"),
        tag("p", "Measurement mode ", summary["measurement_mode"],
            ": every Run used the instrumented diagnostics runner; these are "
            "not uninstrumented timings.", class_="banner-detail"),
    ]
    if not status["complete"]:
        lines.append(tag("p", "Statistics use only the "
                              f"{summary['aggregates']['sample_count']} "
                              "successful measured Runs.",
                         class_="banner-detail"))
    result = banner(label, tone, lines, 1,
                    subtitle=summary["identity"]["manifest_path"])
    return Html(result.replace('<p class="eyebrow">Run diagnostics</p>',
                               '<p class="eyebrow">Benchmark</p>', 1))


def _benchmark_identity(summary: dict) -> Html:
    identity, policy = summary["identity"], summary["policy"]
    return section(
        "identity", "Identity and policy",
        tag("div",
            tag("div", tag("h3", "Manifest and build"), definition_list([
                ("Manifest", identity_value(identity, "manifest_path")),
                ("Manifest hash", identity_value(identity, "manifest_hash")),
                ("SiL version", identity_value(identity, "sil_version")),
                ("Source revision",
                 identity_value(identity, "source_revision")),
                ("Diagnostics collector version",
                 identity_value(identity, "diagnostics_collector_version")),
                ("Generator", f"{summary['generator']['name']} "
                              f"{summary['generator']['sil_version']}"),
            ])),
            tag("div", tag("h3", "Machine"), host_list(summary["host"])),
            tag("div", tag("h3", "Policy"), definition_list([
                ("Measured Runs requested", str(policy["repeats"])),
                ("Warm-up Runs requested", str(policy["warmup"])),
                ("Recording", plain(policy["recording"])),
                ("Provenance mode", provenance_mode_text(policy)),
                ("Execution", policy["execution"]),
                *guard_items(policy),
            ])),
            class_="grid-2"))


def _metric(summary: dict, name: str,
            participant: str | None = None) -> dict | None:
    for metric in summary["aggregates"]["metrics"]:
        if metric["metric"] == name and metric.get("participant") == participant:
            return metric
    return None


def _statistic(metric: dict | None, key: str) -> Html:
    if metric is None:
        return unavailable("not reported")
    return measured(metric, key, metric["unit"])[0] if metric[key] is not None \
        else unavailable(metric["unavailable"].get(key))


def _short_statistic(metric: dict, key: str) -> Html:
    """A statistic, or `unavailable` whose reason is in its own column."""
    if metric[key] is None:
        reason = metric["unavailable"].get(key)
        return tag("span", "unavailable", class_="na",
                   title=REASONS.get(reason, reason))
    return number(metric[key], metric["unit"])


def _benchmark_overview(summary: dict) -> Html:
    count = summary["aggregates"]["sample_count"]
    counts = summary["counts"]

    def spread(name: str) -> Html:
        metric = _metric(summary, name)
        if metric is None or metric["median"] is None:
            return Html("")
        return join(["Min ", _statistic(metric, "min"), " · max ",
                     _statistic(metric, "max")])

    cards = [
        card("Run wall time, median", _statistic(
            _metric(summary, "run_wall_ns"), "median"), spread("run_wall_ns"),
            f"Over {count} successful measured Runs"),
        card("Scheduled speed, median", _statistic(
            _metric(summary, "scheduled_factor"), "median"),
            spread("scheduled_factor"),
            "Manifest Duration over scheduled-execution time"),
        card("Measured Runs",
             tag("span", f"{counts['completed']} of {counts['requested']}"),
             f"succeeded; {counts['failed']} failed, "
             f"{counts['requested'] - counts['attempted']} not attempted",
             f"{counts['warmups_attempted']} warm-ups, never aggregated"),
        card("Runner peak RSS, median", _statistic(
            _metric(summary, "runner_peak_rss_bytes"), "median"),
            spread("runner_peak_rss_bytes"),
            "process_peak_rss of the runner; Process participants have "
            "their own records and are never added"),
    ]
    return section("overview", "Overview", tag("div", *cards, class_="cards"))


def _report_value(report: dict | None, path: str) -> Any:
    value: Any = report
    for key in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _runs_section(summary: dict) -> Html:
    rows, classes = [], []
    for index, run in enumerate(summary["runs"]):
        text, tone = _run_outcome(run)
        report = run["report"]
        outcome = run["process_outcome"]
        process = (f"signal {outcome['signal']}" if outcome["signal"] is not None
                   else f"exit code {outcome['exit_code']}")
        wall = report["observational"]["run_wall_time"] if report else None
        speed = report["observational"]["virtual_time_speed"] if report \
            else None
        runner = report["observational"]["resources"]["runner"] if report \
            else None
        missing = (tag("span", "no report", class_="na"), "")
        rows.append([
            tag("a", run["label"], href=f"#run-{index}", class_="run-label",
                data_run=str(index)),
            "Warm-up (never aggregated)" if run["role"] == "warmup"
            else "Measured",
            (tag("span", tag("span", ICONS[tone], aria_hidden="true",
                             class_="icon"), " ", text,
                 class_=f"pill tone-{tone}"), text),
            (plain(run["run_exit_code"], "none"),
             "" if run["run_exit_code"] is None else str(run["run_exit_code"])),
            process,
            run["report_status"],
            measured(wall, "wall_ns", "ns") if wall else missing,
            measured(speed, "scheduled_factor", "factor") if speed else missing,
            measured(runner, "peak_rss_bytes", "bytes") if runner else missing,
            tag("code", run["report_path"], class_="run-path"),
        ])
        classes.append(f"role-{run['role']} tone-{tone}")
    headers = [("Run", False), ("Role", False), ("Outcome", False),
               ("Run exit code", True), ("Process", False), ("Report", False),
               ("Run wall time", True), ("Scheduled speed", True),
               ("Runner peak RSS", True), ("Report path", False)]
    not_attempted = (summary["counts"]["requested"]
                     - summary["counts"]["attempted"])
    return section(
        "runs", "Runs",
        table("runs-table", "Every attempted warm-up and measured Run, in "
                            "order", headers, rows, row_classes=classes),
        tag("p", f"{not_attempted} requested measured Runs were not "
                 "attempted.", class_="note") if not_attempted else None,
        note="Warm-ups are kept for inspection and never enter a statistic. "
             "The campaign stops at the first failed Run.")


def _run_bars(summary: dict, value_of) -> list[tuple[str, Any, str, str]]:
    bars = []
    for run in summary["runs"]:
        _text, tone = _run_outcome(run)
        bars.append((run["label"], value_of(run["report"]), run["role"], tone))
    return bars


def _run_charts(summary: dict) -> Html:
    charts = [bar_chart(
        "chart-wall", "Run wall time", "ns",
        _run_bars(summary, lambda r: _report_value(
            r, "observational.run_wall_time.wall_ns")),
        _metric(summary, "run_wall_ns"))]
    charts.append(bar_chart(
        "chart-runner-rss", "Runner peak RSS (process_peak_rss)", "bytes",
        _run_bars(summary, lambda r: _report_value(
            r, "observational.resources.runner.peak_rss_bytes")),
        _metric(summary, "runner_peak_rss_bytes")))
    names = process_participant_names(run["report"]
                                      for run in summary["runs"])
    for index, name in enumerate(names):
        charts.append(bar_chart(
            f"chart-process-rss-{index}",
            f"Process {name} peak RSS (largest_process_peak_rss)", "bytes",
            _run_bars(summary, lambda r, n=name: (
                process_record(r, n) or {}).get("peak_rss_bytes")),
            _metric(summary, "process_peak_rss_bytes", name)))
    return section(
        "run-charts", "Per-Run comparison",
        tag("div", *charts, class_="charts"),
        note="Each memory chart is one resource scope; scopes are never "
             "added up. The Runs table above holds the same values as text.")


def _per_run_reason(summary: dict, metric: dict, label: str) -> str | None:
    """The Run report's reason for a null per-Run value, at `source`."""
    run = next((r for r in summary["runs"] if r["label"] == label), None)
    report = run["report"] if run else None
    if report is None:
        return None
    if "participant" in metric:
        record = process_record(report, metric["participant"]) or {}
        member = metric["source"].rsplit(".", 1)[1]
        return record.get("unavailable", {}).get(member, "not_started"
                                                 if not record else None)
    parent, member = metric["source"].rsplit(".", 1)
    holder = _report_value(report, parent)
    if isinstance(holder, dict):
        return holder.get("unavailable", {}).get(member)
    return None


def _metric_label(metric: dict) -> Html:
    if "participant" not in metric:
        return tag("code", metric["metric"])
    return join([tag("code", metric["metric"]), " · Participant ",
                 tag("span", metric["participant"], class_="name")])


def _aggregates(summary: dict) -> Html:
    aggregates = summary["aggregates"]
    rows = []
    for metric in aggregates["metrics"]:
        reasons = sorted(set(metric["unavailable"].values()))
        rows.append([
            _metric_label(metric), metric["unit"],
            (number(len(metric["per_run"]), "count"),
             str(len(metric["per_run"]))),
            *((_short_statistic(metric, key), "" if metric[key] is None
               else str(metric[key]))
              for key in ("min", "median", "max", "mean", "stddev")),
            join([tag("code", reason, title=REASONS.get(reason, reason))
                  for reason in reasons]) if reasons
            else tag("span", "none", class_="muted"),
        ])
    headers = [("Metric", False), ("Unit", False), ("Runs", True),
               ("Min", True), ("Median", True), ("Max", True), ("Mean", True),
               ("Population stddev", True), ("Unavailable because", False)]
    labels = [run["label"] for run in summary["runs"] if _successful(run)]
    per_run_rows = []
    for metric in aggregates["metrics"]:
        values = {item["label"]: item["value"] for item in metric["per_run"]}
        cells: list[Any] = [_metric_label(metric)]
        for label in labels:
            value = values.get(label)
            if value is None:
                cells.append((unavailable(
                    _per_run_reason(summary, metric, label)), ""))
            else:
                cells.append((number(value, metric["unit"]), str(value)))
        per_run_rows.append(cells)
    return section(
        "aggregates", "Aggregate statistics",
        table("aggregates-table",
              f"Statistics over {aggregates['sample_count']} successful "
              "measured Runs", headers, rows),
        tag("h3", "Per-Run values"),
        table("per-run-table", "The value of each metric in each successful "
                               "measured Run",
              [("Metric", False), *((label, True) for label in labels)],
              per_run_rows) if labels else
        tag("p", "No measured Run succeeded, so no Run enters a statistic.",
            class_="note"),
        note="Statistics use only successful measured Runs. When one of them "
             "has no value for a metric, every statistic of that metric is "
             "unavailable (unavailable_in_a_measured_run). An average of "
             "per-Run percentiles is never shown as an overall percentile.")


def _metric_meanings(summary: dict) -> Html:
    names = list(dict.fromkeys(metric["metric"]
                               for metric in summary["aggregates"]["metrics"]))
    rows = [[tag("code", name), METRIC_MEANINGS.get(name, "as reported")]
            for name in names]
    return section(
        "metric-meanings", "Metrics",
        tag("details",
            tag("summary", "What each metric means"),
            table("metric-meanings-table", "The metrics of the aggregate "
                                           "statistics",
                  [("Metric", False), ("Meaning", False)], rows,
                  filterable=False) if rows else
            tag("p", "This benchmark has no metric.", class_="note"),
            tag("p", "A metric that starts with process_ exists once for "
                     "each Process participant, and the table names that "
                     "Participant from the Manifest. User CPU is time in the "
                     "program's own code; system CPU is time in the "
                     "operating system kernel for it.", class_="note"),
            class_="methodology"))


def _run_details(summary: dict) -> Html:
    options = [tag("option", "All Runs", value="all")]
    details = []
    for index, run in enumerate(summary["runs"]):
        text, tone = _run_outcome(run)
        options.append(tag("option", f"{run['label']} ({text})",
                           value=str(index)))
        heading = f"{run['label']}: {text}"
        report = run["report"]
        if report is None:
            outcome = run["process_outcome"]
            body = tag("p", "No diagnostics report: the process ended with ",
                       f"signal {outcome['signal']}"
                       if outcome["signal"] is not None else
                       f"exit code {outcome['exit_code']}",
                       ". No metric exists for this Run.", class_="note")
        else:
            body = join([run_banner(report, level=4),
                         run_body(report, f"run-{index}-", 4)])
        details.append(tag(
            "section",
            tag("h3", heading, id=f"run-{index}-h"),
            tag("p", "Warm-up: kept for inspection, never aggregated."
                if run["role"] == "warmup" else "Measured Run.",
                class_="note"),
            body, class_=f"run-detail role-{run['role']}", id=f"run-{index}",
            data_run=str(index), aria_labelledby=f"run-{index}-h"))
    runs = summary["runs"]
    first = next((i for i, run in enumerate(runs)
                  if _run_outcome(run)[1] != "ok"),
                 next((i for i, run in enumerate(runs) if _successful(run)),
                      None))
    selector = tag("div", tag("label", "Show Run ", tag(
        "select", *options, id="run-select",
        data_default="all" if first is None else str(first)),
        for_="run-select"),
        class_="run-selector", hidden=True)
    return section(
        "run-details", "Run details", selector, *details,
        note="Per-Run activation distributions, slow activations and "
             "resources, from each embedded Run report.")
