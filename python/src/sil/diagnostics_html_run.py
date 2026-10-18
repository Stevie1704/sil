"""The page of a `sil_run_diagnostics` report (#304).

The benchmark page reuses its banner, body and methodology for each embedded
Run report; `prefix` keeps the element ids of each copy distinct.
"""

from __future__ import annotations

from typing import Any

from sil.diagnostics_format import format_duration_ns
from sil.diagnostics_html_charts import histogram, phase_chart
from sil.diagnostics_html_markup import (
    PHASE_LABELS, REASONS, Html, banner, card, code, definition_list,
    identity_value, join, measured, number, plain, section, table, tag,
)
from sil.diagnostics_report import PHASES


STATUS = {
    "succeeded": ("Succeeded", "ok"),
    "run_failure": ("Run failure", "fail"),
    "configuration_error": ("Configuration error", "fail"),
    "interrupted": ("Interrupted", "warn"),
}


LIMITATIONS = {
    "peak_includes_parent_rss_at_spawn":
        "Linux: a high-water mark survives exec, so the peak is at least the "
        "resident size of the parent when it forked.",
    "descendants_counted_only_when_waited":
        "A descendant the child did not wait for is in no record.",
}


GUARDS = {
    "participant_timeout_ms": ("Participant timeout", "ms"),
    "max_protocol_line_bytes": ("Max protocol line", "bytes"),
    "max_step_output_messages": ("Max Step output Messages", ""),
    "max_step_inline_payload_bytes": ("Max Step inline payload", "bytes"),
}


def run_page(report: dict) -> Html:
    return join([
        run_banner(report, level=1),
        _overview(report, "", 2),
        _identity_section(report),
        _run_details_body(report, "", 2),
        methodology(report_reasons(report)),
    ])


def run_banner(report: dict, level: int) -> Html:
    run = report["run"]
    label, tone = STATUS.get(run["status"], (run["status"], "warn"))
    completeness = report["completeness"]
    state = ("Complete Run" if completeness["run_complete"] else
             "Partial Run: " + ", ".join(completeness["reasons"]))
    lines: list[Any] = [tag("p", state, f" · exit code {run['exit_code']}",
                            " · measurement mode ",
                            report["configuration"]["measurement_mode"],
                            class_="banner-detail")]
    failure = run["failure"]
    if failure is not None:
        context = [failure["category"]]
        if failure.get("participant"):
            context.append(f"Participant {failure['participant']}")
        if failure.get("virtual_time_ns") is not None:
            context.append(
                "Virtual time " + format_duration_ns(failure["virtual_time_ns"]))
        lines.append(tag("p", tag("strong", "First failure: "),
                         tag("span", failure["message"], class_="message"),
                         f" ({', '.join(context)})", class_="banner-detail"))
    if not completeness["run_complete"]:
        execution = report["execution"]
        lines.append(tag(
            "p", "Last Virtual time: ",
            measured(execution, "last_virtual_time_ns", "ns")[0],
            " of Duration ", measured(execution, "duration_ns", "ns")[0],
            ". Completion is not inferred from the last Slot.",
            class_="banner-detail"))
    for error in report["collection_errors"]:
        lines.append(tag("p", tag("strong", "Collection error: "),
                         error["scope"], ": ", error["message"],
                         class_="banner-detail"))
    return banner(label, tone, lines, level,
                  subtitle=report["identity"]["manifest_path"])


def _identity_section(report: dict) -> Html:
    identity, host = report["identity"], report["host"]
    configuration, collector = report["configuration"], report["collector"]
    artifacts = report["artifacts"]
    return section(
        "identity", "Identity and environment",
        tag("div",
            tag("div", tag("h3", "Manifest and build"), definition_list([
                ("Manifest", identity_value(identity, "manifest_path")),
                ("Manifest hash", identity_value(identity, "manifest_hash")),
                ("SiL version", identity_value(identity, "sil_version")),
                ("Source revision",
                 identity_value(identity, "source_revision")),
                ("Source repository",
                 identity_value(identity, "source_repository")),
            ])),
            tag("div", tag("h3", "Machine"), host_list(host)),
            tag("div", tag("h3", "Measurement"), definition_list([
                ("Measurement mode", configuration["measurement_mode"]),
                ("Recording", plain(configuration["recording"])),
                *guard_items(configuration),
                ("Collector", f"{collector['name']} version "
                              f"{collector['collector_version']}"),
                ("Clock", collector["clock"]),
            ])),
            tag("div", tag("h3", "Artifacts"), definition_list([
                ("Recording", code(artifacts["recording"])),
                ("Provenance", code(artifacts["provenance"])),
                ("Failure report", code(artifacts["failure_report"])),
            ])),
            class_="grid-2"))


def guard_items(values: dict) -> list[tuple[str, Html]]:
    """The timeout and Step-protocol guards; null is the runner default."""
    items = []
    for name, (label, unit) in GUARDS.items():
        value = values[name]
        items.append((label, tag("span", plain(value, "runner default"),
                                 f" {unit}" if value is not None and unit
                                 else "", title=name)))
    return items


def host_list(host: dict) -> Html:
    return definition_list([
        ("Operating system", identity_value(host, "os")),
        ("OS release", identity_value(host, "os_release")),
        ("Architecture", identity_value(host, "architecture")),
        ("Logical CPUs", identity_value(host, "logical_cpu_count")),
        ("CPU", identity_value(host, "cpu_description")),
    ])


def run_body(report: dict, prefix: str, level: int) -> Html:
    return join([_overview(report, prefix, level),
                 _run_details_body(report, prefix, level)])


def _run_details_body(report: dict, prefix: str, level: int) -> Html:
    return join([
        _lifecycle(report, prefix, level),
        _tasks(report, prefix, level),
        _replay(report, prefix, level),
        _distributions(report, prefix, level),
        _slowest(report, prefix, level),
        _resources(report, prefix, level),
    ])


def _overview(report: dict, prefix: str, level: int) -> Html:
    observational = report["observational"]
    wall = observational["run_wall_time"]
    speed = observational["virtual_time_speed"]
    throughput = observational["throughput"]
    resources = observational["resources"]
    replay_slots = sum(p["replay"]["completed"] for p in
                       report["execution"]["participants"]
                       if p["replay"] is not None)
    processes = resources["process_participants"]
    memory_details = [
        tag("span", tag("span", "Process ", record["participant"], ": "),
            measured(record, "peak_rss_bytes", "bytes")[0],
            f" ({record['peak_rss_kind'] or 'no record'})")
        for record in processes]
    cards = [
        card("Run wall time", measured(wall, "wall_ns", "ns")[0],
             f"Boundary: {wall['boundary']}"),
        card("Virtual-time speed, scheduled",
             measured(speed, "scheduled_factor", "factor")[0],
             tag("span", "End-to-end ",
                 measured(speed, "end_to_end_factor", "factor")[0]),
             "Manifest Duration over scheduled-execution and Run wall time"),
        card("Completed activations",
             tag("span", number(throughput["completed_native_activations"],
                                "count"), " Native activations"),
             tag("span", number(throughput["completed_process_steps"],
                                "count"), " Process Steps"),
             tag("span", number(replay_slots, "count"),
                 " Replay publication Slots") if observational["replay"]
             else None,
             join(["Native ",
                   measured(throughput, "native_activations_per_s", "1/s")[0],
                   " · Process ",
                   measured(throughput, "process_steps_per_s", "1/s")[0]]),
             join(["Per second of scheduled execution (",
                   measured(throughput, "scheduled_execution_ns", "ns")[0],
                   ")"])),
        card("Runner peak RSS",
             measured(resources["runner"], "peak_rss_bytes", "bytes")[0],
             f"{resources['runner']['peak_rss_kind'] or 'no record'}; "
             "includes the Native participants, which run in the runner",
             *memory_details,
             "Peaks of different processes are never added up"
             if processes else None),
    ]
    return section(f"{prefix}overview", "Overview",
                   tag("div", *cards, class_="cards"), level=level)


def _lifecycle(report: dict, prefix: str, level: int) -> Html:
    phases = report["observational"]["phases"]
    wall = report["observational"]["run_wall_time"]
    names = (*PHASES, "unattributed_ns")
    rows = [[PHASE_LABELS[name], measured(phases, name, "ns")]
            for name in names]
    rows.append([tag("strong", "Run wall time"),
                 measured(wall, "wall_ns", "ns")])
    return section(
        f"{prefix}lifecycle", "Lifecycle phases",
        phase_chart(phases),
        table(f"{prefix}phases-table", "Lifecycle phases (non-overlapping)",
              [("Phase", False), ("Wall time", True)], rows,
              filterable=False),
        level=level,
        note="The phases do not overlap; with the unattributed residual they "
             "add up to the Run wall time. Activation times are not added to "
             "them: routing and recording inside a Task are in both.")


def _task_index(report: dict) -> dict[tuple[str, str], int]:
    return {(row["participant"], row["task"]): index
            for index, row in enumerate(report["observational"]["tasks"])}


def _tasks(report: dict, prefix: str, level: int) -> Html:
    timings = {(row["participant"], row["task"]): row["timing"]
               for row in report["observational"]["tasks"]}
    index = _task_index(report)
    rows, ids = [], []
    for participant in report["execution"]["participants"]:
        name = participant["participant"]
        for task in participant["tasks"]:
            timing = timings[(name, task["task"])]
            rows.append([
                tag("span", name, class_="name"),
                tag("span", task["task"], class_="name"),
                "Native activation" if task["kind"] == "native_task"
                else "Process Step",
                (number(task["period_ns"], "ns"), str(task["period_ns"])),
                *((number(task[key], "count"), str(task[key]))
                  for key in ("attempted", "completed", "failed",
                              "interrupted")),
                _first_failed(task.get("failed_activation")),
                *(measured(timing, key, "ns") for key in (
                    "mean_ns", "p50_ns", "p95_ns", "p99_ns", "min_ns",
                    "max_ns", "stddev_ns", "sum_ns")),
                _samples_cell(timing),
            ])
            ids.append(f"{prefix}task-{index[(name, task['task'])]}")
    headers = [("Participant", False), ("Task", False), ("Kind", False),
               ("Period", True), ("Attempted", True), ("Completed", True),
               ("Failed", True), ("Interrupted", True),
               ("First failed or interrupted", False),
               ("Mean", True), ("p50", True), ("p95", True), ("p99", True),
               ("Min", True), ("Max", True), ("Stddev", True), ("Sum", True),
               ("Samples", True)]
    return section(
        f"{prefix}tasks", "Participants and Tasks",
        table(f"{prefix}tasks-table",
              "Inclusive wall time of completed activations per Task",
              headers, rows, row_ids=ids),
        level=level,
        note="Times describe completed activations only; failed and "
             "interrupted activations are counted, not timed. Percentiles "
             "are nearest rank over the retained samples; p95 needs 20 and "
             "p99 100 completed samples. A maximum is not a worst-case "
             "execution time.")


def _first_failed(failed: dict | None) -> Html:
    if failed is None:
        return tag("span", "none", class_="muted")
    return tag("span", f"{failed['outcome']} at activation "
                       f"{failed['activation_index']}, Virtual time "
                       f"{format_duration_ns(failed['virtual_time_ns'])}",
               class_="flag")


def _samples_cell(timing: dict) -> tuple[Html, str]:
    quantiles = timing["quantiles"]
    retained, total = quantiles["retained_count"], quantiles["sample_count"]
    text = f"{retained} of {total}"
    if quantiles["approximate"]:
        return (tag("span", text, " ", tag("span", "approximate",
                                           class_="badge"),
                    class_="num"), str(total))
    return tag("span", text, class_="num"), str(total)


def _replay(report: dict, prefix: str, level: int) -> Html:
    replays = {row["participant"]: row["timing"]
               for row in report["observational"]["replay"]}
    rows = []
    for participant in report["execution"]["participants"]:
        replay = participant["replay"]
        if replay is None:
            continue
        timing = replays.get(participant["participant"])
        rows.append([
            tag("span", participant["participant"], class_="name"),
            (number(replay["publications"], "count"),
             str(replay["publications"])),
            *((number(replay[key], "count"), str(replay[key]))
              for key in ("attempted", "completed", "failed", "interrupted")),
            *(measured(timing, key, "ns") for key in (
                "mean_ns", "p50_ns", "p95_ns", "p99_ns", "max_ns")),
        ])
    if not rows:
        return Html("")
    headers = [("Participant", False), ("Messages", True),
               ("Publication Slots", True), ("Completed", True),
               ("Failed", True), ("Interrupted", True), ("Mean", True),
               ("p50", True), ("p95", True), ("p99", True), ("Max", True)]
    return section(
        f"{prefix}replay", "Replay work",
        table(f"{prefix}replay-table",
              "Replay publication Slots (never Steps or activations)",
              headers, rows),
        level=level,
        note="A publication Slot is a Slot where at least one recorded "
             "Message was due. Replay work is never a Process Step.")


def _distributions(report: dict, prefix: str, level: int) -> Html:
    figures = []
    for index, row in enumerate(report["observational"]["tasks"]):
        figures.append(histogram(
            f"{prefix}dist-{index}", f"{row['participant']} / {row['task']}",
            row["timing"], f"#{prefix}task-{index}"))
    for index, row in enumerate(report["observational"]["replay"]):
        figures.append(histogram(
            f"{prefix}replay-dist-{index}", f"{row['participant']} (Replay)",
            row["timing"], f"#{prefix}replay"))
    if not figures:
        return Html("")
    return section(
        f"{prefix}distributions", "Timing distributions",
        tag("div", *figures, class_="charts"), level=level,
        note="Each chart draws the retained samples of one Task. Up to "
             "8,192 completed activations every sample is retained; above "
             "that a uniform reservoir sample is drawn and the percentiles "
             "are approximate.")


def _slowest(report: dict, prefix: str, level: int) -> Html:
    rows = []
    for index, row in enumerate(report["observational"]["tasks"]):
        for rank, slow in enumerate(row["timing"]["slowest"], start=1):
            rows.append([
                tag("a", row["participant"], href=f"#{prefix}task-{index}",
                    class_="name"),
                tag("a", row["task"], href=f"#{prefix}dist-{index}",
                    class_="name"),
                (number(rank, "count"), str(rank)),
                (number(slow["activation_index"], "count"),
                 str(slow["activation_index"])),
                (number(slow["virtual_time_ns"], "ns"),
                 str(slow["virtual_time_ns"])),
                (number(slow["wall_ns"], "ns"), str(slow["wall_ns"])),
            ])
    headers = [("Participant", False), ("Task", False), ("Rank", True),
               ("Activation index", True), ("Virtual time", True),
               ("Wall time", True)]
    return section(
        f"{prefix}slowest", "Slow activations",
        table(f"{prefix}slowest-table",
              "Slowest completed activations, at most 20 per Task", headers,
              rows),
        level=level,
        note="The activation index is zero-based. Equal times are in "
             "activation-index order. This is not a full trace.")


def _resources(report: dict, prefix: str, level: int) -> Html:
    resources = report["observational"]["resources"]
    records = [("Runner process", None, resources["runner"])] + [
        ("Process participant", record["participant"], record)
        for record in resources["process_participants"]]
    rows = []
    for scope, participant, record in records:
        rows.append([
            scope,
            tag("span", participant, class_="name") if participant
            else tag("span", "Native participants run here", class_="muted"),
            code(record["collector"]),
            code(record["interval"]),
            plain(record["includes_waited_descendants"], "unavailable"),
            measured(record, "user_cpu_ns", "ns"),
            measured(record, "system_cpu_ns", "ns"),
            measured(record, "peak_rss_bytes", "bytes"),
            code(record["peak_rss_kind"]),
            join([tag("code", item, title=LIMITATIONS.get(item, item))
                  for item in record["limitations"]]) or
            tag("span", "none", class_="muted"),
        ])
    headers = [("Scope", False), ("Participant", False), ("Collector", False),
               ("Interval", False), ("Waited descendants", False),
               ("User CPU", True), ("System CPU", True), ("Peak RSS", True),
               ("Peak RSS kind", False), ("Limitations", False)]
    total = resources["cpu_total"]
    used = sorted({code_ for _s, _p, record in records
                   for code_ in record["limitations"]})
    return section(
        f"{prefix}resources", "Resources",
        table(f"{prefix}resources-table",
              "CPU time and peak resident memory per scope", headers, rows),
        definition_list([
            ("CPU total of runner and Process participants",
             join(["user ", measured(total, "user_cpu_ns", "ns")[0],
                   ", system ", measured(total, "system_cpu_ns", "ns")[0]])),
            ("Native participants",
             "share the runner process; no per-Native attribution"),
            ("Memory total", "none: peaks of different processes need not "
                             "occur at the same time, so they are never "
                             "added up"),
        ]),
        tag("ul", *(tag("li", tag("code", item), ": ",
                        LIMITATIONS.get(item, "unknown limitation"))
                    for item in used), class_="limitations") if used
        else None,
        level=level,
        note="CPU time can exceed wall time for multithreaded software; the "
             "ratio is not a single-core percentage. A Process participant "
             "peak is the largest process among the child and its waited "
             "descendants, not the memory of the process tree at one time.")


def report_reasons(report: Any) -> set[str]:
    """Every reason any `unavailable` map of the document names."""
    found: set[str] = set()
    if isinstance(report, dict):
        for key, value in report.items():
            if key == "unavailable" and isinstance(value, dict):
                found.update(str(reason) for reason in value.values())
            else:
                found |= report_reasons(value)
    elif isinstance(report, list):
        for item in report:
            found |= report_reasons(item)
    return found


def methodology(reasons: set[str], benchmark: bool = False) -> Html:
    points = [
        "Run wall time is the host monotonic time of the native runner from "
        "before the Manifest is loaded through Participant cleanup and "
        "Recording close. Python startup and report writing are outside it.",
        "A Native activation is timed from the call into the Task to its "
        "return, including routing and recording it calls synchronously.",
        "A Process Step is timed from preparing its inputs through response "
        "handling and output publication: codec work, IPC, OS waiting, the "
        "Participant and routing. It is not isolated algorithm time.",
        "Percentiles are nearest rank, rank = ceil(p × n / 100), over "
        "the retained samples. Up to 8,192 completed samples they are exact; "
        "above that a uniform reservoir (algorithm R, splitmix64, a private "
        "seed) keeps 8,192 samples and the percentiles are approximate.",
        "p95 needs 20 and p99 100 completed samples. These minimums do not "
        "make a tail statistically reliable. No outlier is removed, and a "
        "maximum is not a worst-case execution time.",
        "CPU and memory are process resource records with their collector, "
        "interval and scope. Native participants run in the runner. No "
        "memory total exists.",
        "Host measurements never change Virtual time or scheduling, and the "
        "diagnostics runner is instrumented: its times are not uninstrumented "
        "timings.",
    ]
    if benchmark:
        points += [
            "Every Run is a fresh process in a fresh directory, one after the "
            "other. Warm-ups and failed Runs never enter a statistic, and "
            "activations are never pooled across Runs.",
            "Statistics are min, median, max, mean and population standard "
            "deviation over the successful measured Runs. The median of an "
            "even count is the mean of the two middle values. No confidence "
            "interval is given for a few repetitions.",
            "A Run is successful when its process exited 0 and it wrote a "
            "valid report; a Run that exited 0 without a report failed.",
        ]
    reason_rows = [[tag("code", reason), REASONS.get(reason, "as reported")]
                   for reason in sorted(reasons)]
    return section(
        "methodology", "Methodology and limitations",
        tag("details",
            tag("summary", "How these values were measured"),
            tag("ul", *(tag("li", point) for point in points)),
            table("reasons-table", "Reasons for unavailable values in this "
                                   "report",
                  [("Reason", False), ("Meaning", False)], reason_rows,
                  filterable=False) if reason_rows else None,
            class_="methodology"))
