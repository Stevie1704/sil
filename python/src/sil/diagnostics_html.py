"""The standalone HTML report of a saved diagnostics report (#304).

`render(text)` turns the text of a `sil_run_diagnostics` or a
`sil_benchmark_report` document into one HTML file that opens offline, from
`file://`, with no other file. The HTML presents the saved JSON and nothing
else: every value is a value of the report, formatted as the terminal
summary formats it (sil.diagnostics_run), and no statistic is computed again.

Python writes the complete page, so its status, cards, tables and charts
(inline SVG) need no JavaScript. The inline script only adds conveniences:
sorting and filtering tables, the Run selector, the theme switch and a Blob
download. The source JSON is embedded once, base64-encoded in the download
link, so report content never reaches a parser as markup or script, and the
download is the source byte for byte. A Content-Security-Policy that allows
only the page's own style and script blocks any network request.
"""

from __future__ import annotations

import base64
import hashlib
import html
import math
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterable, Sequence

from sil.diagnostics_report import (
    BENCHMARK_REPORT_KEY, PHASES, parse_report, process_participant_names,
    process_record, write_new_file,
)
from sil.diagnostics_run import format_duration_ns, format_value

ASSETS = files("sil").joinpath("report_assets")

STATUS = {
    "succeeded": ("Succeeded", "ok"),
    "run_failure": ("Run failure", "fail"),
    "configuration_error": ("Configuration error", "fail"),
    "interrupted": ("Interrupted", "warn"),
}
ICONS = {"ok": "✓", "fail": "✕", "warn": "!"}

# docs/diagnostics-report.md: the reasons of v1. An unknown reason is shown
# as it is.
REASONS = {
    "not_reached": "The Run ended before this phase",
    "no_recording": "--no-recording, so there is no Recording to finalize",
    "no_completed_activations": "No completed activation to describe",
    "insufficient_samples":
        "Fewer completed samples than the percentile needs",
    "run_not_successful": "The factor is defined only for a successful full Run",
    "no_scheduled_execution": "The scheduled-execution time is missing or zero",
    "no_slot_opened": "The Run opened no Slot",
    "manifest_not_loaded": "The Manifest was not loaded",
    "not_reported_by_os": "The host does not report this value",
    "not_measured": "The collector has no measurement for this value",
    "unsupported_platform":
        "The collector cannot measure resources on this host",
    "not_started": "The Process participant was never started",
    "incomplete_scopes":
        "A total is missing because one of the scopes it adds is missing",
    "not_collected": "Collector version 1 did not collect resource values",
    "no_successful_measured_runs":
        "No measured Run succeeded, so the statistic has no sample",
    "unavailable_in_a_measured_run":
        "A successful measured Run has no value for this metric, so no "
        "statistic uses fewer Runs than the sample count",
    "no_run_report": "No Run wrote a report to take this value from",
}
LIMITATIONS = {
    "peak_includes_parent_rss_at_spawn":
        "Linux: a high-water mark survives exec, so the peak is at least the "
        "resident size of the parent when it forked.",
    "descendants_counted_only_when_waited":
        "A descendant the child did not wait for is in no record.",
}
PHASE_LABELS = {
    "preflight_ns": "Preflight",
    "setup_ns": "Setup",
    "scheduled_execution_ns": "Scheduled execution",
    "shutdown_ns": "Shutdown",
    "recording_finalization_ns": "Recording finalization",
    "unattributed_ns": "Unattributed",
}
TIMING_SCOPES = {
    "inclusive_native_activation": "inclusive Native activation",
    "inclusive_process_step": "inclusive Process Step",
    "inclusive_replay_publication_slot": "inclusive Replay publication Slot",
}
GUARDS = {
    "participant_timeout_ms": ("Participant timeout", "ms"),
    "max_protocol_line_bytes": ("Max protocol line", "bytes"),
    "max_step_output_messages": ("Max Step output Messages", ""),
    "max_step_inline_payload_bytes": ("Max Step inline payload", "bytes"),
}
HISTOGRAM_MAX_BINS = 40


def render(text: str) -> str:
    """The HTML page of a report text; raise ReportError if it is invalid."""
    document = parse_report(text)
    if BENCHMARK_REPORT_KEY in document:
        title, body = "SiL benchmark report", _benchmark_page(document)
    else:
        title, body = "SiL Run diagnostics", _run_page(document)
    return _page(title, body, text)


def write_html(text: str, path: Path) -> str | None:
    """Render `text` into the new file `path`; return why not, or None."""
    try:
        write_new_file(path, render(text))
    except OSError as error:
        return str(error)
    except Exception as error:  # noqa: BLE001 - the Run's outcome must stand
        return f"the report cannot be rendered: {error}"
    return None


# --- markup -----------------------------------------------------------------


class Html(str):
    """Text that is already markup; every other value is escaped."""


def esc(value: Any) -> Html:
    if isinstance(value, Html):
        return value
    return Html(html.escape(str(value), quote=True))


def join(parts: Iterable[Any]) -> Html:
    return Html("".join(esc(part) for part in parts if part is not None))


def _attributes(attrs: dict[str, Any]) -> str:
    text = ""
    for key, value in attrs.items():
        if value is None or value is False:
            continue
        name = key.rstrip("_").replace("_", "-")
        text += f" {name}" if value is True else f' {name}="{esc(value)}"'
    return text


def tag(element: str, *children: Any, **attrs: Any) -> Html:
    return Html(f"<{element}{_attributes(attrs)}>{join(children)}"
                f"</{element}>")


def void(element: str, **attrs: Any) -> Html:
    return Html(f"<{element}{_attributes(attrs)}>")


def _asset(name: str) -> str:
    return ASSETS.joinpath(name).read_text(encoding="utf-8")


def _digest(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()


def _page(title: str, body: Html, source: str) -> str:
    style, script = _asset("report.css"), _asset("report.js")
    policy = ("default-src 'none'; img-src data:; "
              f"style-src 'sha256-{_digest(style)}'; "
              f"script-src 'sha256-{_digest(script)}'")
    download = "data:application/json;base64," + base64.b64encode(
        source.encode("utf-8")).decode("ascii")
    toolbar = tag(
        "div",
        tag("a", "Download JSON", href=download, download="report.json",
            class_="button", id="download"),
        tag("button", "Theme: auto", type="button", id="theme", hidden=True,
            aria_label="Switch between automatic, light and dark theme"),
        tag("button", "Print", type="button", id="print", hidden=True),
        class_="toolbar")
    return ("<!doctype html>\n" + tag(
        "html",
        tag("head",
            void("meta", charset="utf-8"),
            void("meta", name="viewport",
                 content="width=device-width, initial-scale=1"),
            void("meta", http_equiv="Content-Security-Policy",
                 content=policy),
            tag("title", title),
            Html(f"<style>{style}</style>")),
        tag("body",
            tag("a", "Skip to content", href="#main", class_="skip"),
            tag("header", tag("div", tag("p", "SiL", class_="brand"),
                              toolbar, class_="topbar"),
                class_="site"),
            tag("main", body, id="main"),
            Html(f"<script>{script}</script>")),
        lang="en") + "\n")


def section(id_: str, heading: str, *content: Any, level: int = 2,
            note: Any = None) -> Html:
    return tag("section",
               tag(f"h{level}", heading, id=f"{id_}-h"),
               tag("p", note, class_="note") if note is not None else None,
               *content, id=id_, aria_labelledby=f"{id_}-h")


def unavailable(reason: str | None) -> Html:
    reason = reason or "not reported"
    return tag("span", "unavailable ", tag("span", f"({reason})",
                                           class_="reason"),
               class_="na", title=REASONS.get(reason, reason))


def exact(value: Any, unit: str) -> str:
    """The exact JSON value, for a tooltip: integers keep every digit."""
    suffix = {"ns": " ns", "bytes": " bytes", "1/s": " per second"}
    return f"{value}{suffix.get(unit, '')}"


def measured(record: dict | None, key: str, unit: str) -> tuple[Html, str]:
    """A value with its unit, or unavailable (reason); and its sort key."""
    value = None if record is None else record.get(key)
    if value is None:
        reason = None if record is None else \
            record.get("unavailable", {}).get(key)
        return unavailable(reason), ""
    return number(value, unit), str(value)


def number(value: Any, unit: str) -> Html:
    text = str(value) if unit == "count" else format_value(unit, value)
    return tag("span", text, class_="num", title=exact(value, unit))


def table(id_: str, caption: str, headers: Sequence[tuple[str, bool]],
          rows: Sequence[Sequence[Any]], *, filterable: bool = True,
          row_ids: Sequence[str | None] | None = None,
          row_classes: Sequence[str | None] | None = None) -> Html:
    """A table; a cell is markup, or (markup, sort key) for sorting."""
    head = tag("tr", *(tag("th", label, scope="col",
                           class_="numeric" if numeric else None)
                       for label, numeric in headers))
    body_rows = []
    for index, row in enumerate(rows):
        cells = []
        for (label, numeric), cell in zip(headers, row):
            content, key = cell if isinstance(cell, tuple) else (cell, None)
            cells.append(tag("td", content, data_sort=key,
                             class_="numeric" if numeric else None))
        body_rows.append(tag(
            "tr", *cells,
            id=row_ids[index] if row_ids else None,
            class_=row_classes[index] if row_classes else None))
    if not rows:
        body_rows.append(tag("tr", tag("td", "No rows.",
                                       colspan=str(len(headers)),
                                       class_="empty")))
    search = void("input", type="search", hidden=True,
                  placeholder="Filter rows", data_filter=id_,
                  aria_label=f"Filter {caption}") \
        if filterable and len(rows) > 1 else None
    count = tag("span", "", class_="filter-count", aria_live="polite",
                id=f"{id_}-count") if search else None
    return tag(
        "div",
        tag("div", search, count, class_="table-tools") if search else None,
        tag("div",
            tag("table", tag("caption", caption), tag("thead", head),
                tag("tbody", *body_rows), id=id_,
                data_sortable="true" if len(rows) > 1 else None),
            class_="table-wrap", tabindex="0", role="region",
            aria_label=caption),
        class_="table-block")


def definition_list(items: Sequence[tuple[str, Any]]) -> Html:
    return tag("dl", *(tag("div", tag("dt", term), tag("dd", value))
                       for term, value in items), class_="facts")


def card(label: str, value: Any, *details: Any) -> Html:
    return tag("div", tag("p", label, class_="card-label"),
               tag("p", value, class_="card-value"),
               *(tag("p", detail, class_="card-detail") for detail in details
                 if detail is not None), class_="card")


def plain(value: Any, missing: str = "not set") -> Html:
    if value is None:
        return tag("span", missing, class_="na")
    if value is True:
        return esc("yes")
    if value is False:
        return esc("no")
    return esc(value)


def code(value: Any) -> Html:
    return tag("code", value) if value is not None else plain(None)


def identity_value(record: dict, key: str) -> Html:
    if record.get(key) is None:
        reason = record.get("unavailable", {}).get(key)
        return unavailable(reason) if reason else plain(None)
    return code(record[key])


# --- a Run report ------------------------------------------------------------


def _run_page(report: dict) -> Html:
    return join([
        _run_banner(report, level=1),
        _overview(report, "", 2),
        _identity_section(report),
        _run_details_body(report, "", 2),
        _methodology(report_reasons(report)),
    ])


def _run_banner(report: dict, level: int) -> Html:
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


def banner(label: str, tone: str, lines: Sequence[Any], level: int,
           subtitle: Any = None) -> Html:
    title = "Run diagnostics" if level == 1 else None
    return tag(
        "section",
        tag("p", title, class_="eyebrow") if title else None,
        tag(f"h{level}",
            tag("span", ICONS[tone], class_="icon", aria_hidden="true"),
            " ", label, class_="status-title"),
        tag("p", subtitle, class_="subtitle") if subtitle else None,
        *lines, class_=f"banner tone-{tone}", aria_label=f"Status: {label}")


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
            tag("div", tag("h3", "Machine"), _host_list(host)),
            tag("div", tag("h3", "Measurement"), definition_list([
                ("Measurement mode", configuration["measurement_mode"]),
                ("Recording", plain(configuration["recording"])),
                *_guard_items(configuration),
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


def _guard_items(values: dict) -> list[tuple[str, Html]]:
    """The timeout and Step-protocol guards; null is the runner default."""
    items = []
    for name, (label, unit) in GUARDS.items():
        value = values[name]
        items.append((label, tag("span", plain(value, "runner default"),
                                 f" {unit}" if value is not None and unit
                                 else "", title=name)))
    return items


def _host_list(host: dict) -> Html:
    return definition_list([
        ("Operating system", identity_value(host, "os")),
        ("OS release", identity_value(host, "os_release")),
        ("Architecture", identity_value(host, "architecture")),
        ("Logical CPUs", identity_value(host, "logical_cpu_count")),
        ("CPU", identity_value(host, "cpu_description")),
    ])


def _run_body(report: dict, prefix: str, level: int) -> Html:
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
        _phase_chart(phases),
        table(f"{prefix}phases-table", "Lifecycle phases (non-overlapping)",
              [("Phase", False), ("Wall time", True)], rows,
              filterable=False),
        level=level,
        note="The phases do not overlap; with the unattributed residual they "
             "add up to the Run wall time. Activation times are not added to "
             "them: routing and recording inside a Task are in both.")


def _phase_chart(phases: dict) -> Html:
    names = [name for name in (*PHASES, "unattributed_ns")
             if phases.get(name)]
    total = sum(phases[name] for name in names)
    if total <= 0:
        return tag("p", "No phase has a measured time to draw.",
                   class_="note")
    width, x = 1000.0, 0.0
    bars, legend = [], []
    for index, name in enumerate(names):
        share = phases[name] / total * width
        label = f"{PHASE_LABELS[name]}: {format_duration_ns(phases[name])}"
        bars.append(tag("rect", tag("title", label), x=f"{x:.2f}", y="0",
                        width=f"{max(share, 0.5):.2f}", height="40",
                        class_=f"phase phase-{index}"))
        legend.append(tag("li", tag("span", "", class_=f"swatch phase-{index}",
                                    aria_hidden="true"), label))
        x += share
    svg = tag("svg", *bars, viewBox="0 0 1000 40", preserveAspectRatio="none",
              class_="phase-bar", role="img",
              aria_label="Lifecycle phases as shares of their sum: "
                         + "; ".join(f"{PHASE_LABELS[n]} "
                                     f"{format_duration_ns(phases[n])}"
                                     for n in names))
    return tag("figure", svg, tag("ul", *legend, class_="legend"),
               tag("figcaption", "Bar widths are the phase times in the "
                                 "table below; zero and unavailable phases "
                                 "have no bar."),
               class_="chart")


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
        figures.append(_histogram(
            f"{prefix}dist-{index}", f"{row['participant']} / {row['task']}",
            row["timing"], f"#{prefix}task-{index}"))
    for index, row in enumerate(report["observational"]["replay"]):
        figures.append(_histogram(
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


def _bins(samples: Sequence[int]) -> tuple[list[tuple[float, float, int]], bool]:
    """Histogram bins of the retained samples; log-spaced for a wide range."""
    low, high = samples[0], samples[-1]
    if low == high:
        return [(low, high, len(samples))], False
    count = min(HISTOGRAM_MAX_BINS, max(1, math.ceil(math.sqrt(len(samples)))))
    logarithmic = low > 0 and high / low >= 50
    if logarithmic:
        a, b = math.log(low), math.log(high)
        edges = [math.exp(a + (b - a) * i / count) for i in range(count + 1)]
    else:
        edges = [low + (high - low) * i / count for i in range(count + 1)]
    counts = [0] * count
    for value in samples:
        position = 0
        while position < count - 1 and value >= edges[position + 1]:
            position += 1
        counts[position] += 1
    return ([(edges[i], edges[i + 1], counts[i]) for i in range(count)],
            logarithmic)


def _histogram(id_: str, title: str, timing: dict, anchor: str) -> Html:
    quantiles = timing["quantiles"]
    samples = timing["samples_ns"]
    retained, total = quantiles["retained_count"], quantiles["sample_count"]
    if quantiles["approximate"]:
        sampled = (f"Sampled data: a uniform reservoir of {retained} of "
                   f"{total} completed activations; percentiles are "
                   "approximate.")
    else:
        sampled = (f"Sampled data: all {retained} of {total} completed "
                   "activations retained; percentiles are exact.")
    heading = tag("h4", tag("a", title, href=anchor))
    if not samples:
        reason = timing.get("unavailable", {}).get("mean_ns",
                                                    "no_completed_activations")
        return tag("figure", heading,
                   tag("p", "No retained sample to draw: ",
                       unavailable(reason), class_="empty-chart"),
                   class_="chart histogram", id=id_)
    bins, logarithmic = _bins(samples)
    tallest = max(count for _a, _b, count in bins)
    width, height, left, bottom = 600.0, 180.0, 44.0, 30.0
    plot_w, plot_h = width - left - 8, height - bottom - 10
    bar_w = plot_w / len(bins)
    shapes: list[Any] = [
        tag("line", x1=f"{left}", y1=f"{10 + plot_h}", x2=f"{width - 8}",
            y2=f"{10 + plot_h}", class_="axis"),
        tag("text", str(tallest), x=f"{left - 6}", y="18", class_="tick",
            text_anchor="end"),
        tag("text", "0", x=f"{left - 6}", y=f"{10 + plot_h}", class_="tick",
            text_anchor="end"),
    ]
    for index, (start, end, count) in enumerate(bins):
        bar_h = plot_h * count / tallest
        shapes.append(tag(
            "rect",
            tag("title", f"{format_duration_ns(start)} to "
                         f"{format_duration_ns(end)}: {count} samples"),
            x=f"{left + index * bar_w + 0.5:.2f}",
            y=f"{10 + plot_h - bar_h:.2f}",
            width=f"{max(bar_w - 1, 0.5):.2f}", height=f"{bar_h:.2f}",
            class_="bar"))

    def x_of(value: float) -> float:
        low, high = bins[0][0], bins[-1][1]
        if high == low:
            return left + plot_w / 2
        if logarithmic:
            return left + plot_w * (math.log(value) - math.log(low)) / (
                math.log(high) - math.log(low))
        return left + plot_w * (value - low) / (high - low)

    marks = []
    for label, key in (("p50", "p50_ns"), ("p95", "p95_ns"), ("p99", "p99_ns")):
        value = timing.get(key)
        if value is None:
            marks.append(f"{label} unavailable "
                         f"({timing['unavailable'].get(key, 'not reported')})")
            continue
        x = min(max(x_of(value), left), width - 8)
        shapes.append(tag("line", x1=f"{x:.2f}", y1="10", x2=f"{x:.2f}",
                          y2=f"{10 + plot_h}", class_="marker"))
        shapes.append(tag("text", label, x=f"{x + 3:.2f}", y="20",
                          class_="marker-label"))
        marks.append(f"{label} {format_duration_ns(value)}")
    shapes.append(tag("text", format_duration_ns(bins[0][0]), x=f"{left}",
                      y=f"{height - 8}", class_="tick"))
    shapes.append(tag("text", format_duration_ns(bins[-1][1]),
                      x=f"{width - 8}", y=f"{height - 8}", class_="tick",
                      text_anchor="end"))
    scale = "logarithmic time axis" if logarithmic else "linear time axis"
    description = (f"Histogram of {retained} retained samples of {title}, "
                   f"{scale}, from {format_duration_ns(bins[0][0])} to "
                   f"{format_duration_ns(bins[-1][1])}; " + "; ".join(marks))
    svg = tag("svg", *shapes, viewBox=f"0 0 {width:.0f} {height:.0f}",
              class_="histogram-svg", role="img", aria_label=description)
    bin_rows = [[f"{format_duration_ns(start)} to {format_duration_ns(end)}",
                 (number(count, "count"), str(count))]
                for start, end, count in bins]
    return tag(
        "figure", heading, svg,
        tag("figcaption", sampled, " ", tag("span", scale, class_="muted"),
            " · ", "; ".join(marks)),
        tag("details", tag("summary", "Bin counts as a table"),
            table(f"{id_}-bins", f"Samples per bin of {title}",
                  [("Wall time range", False), ("Samples", True)], bin_rows,
                  filterable=False)),
        class_="chart histogram", id=id_)


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


def _methodology(reasons: set[str], benchmark: bool = False) -> Html:
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


# --- a benchmark report -------------------------------------------------------


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


def _benchmark_page(summary: dict) -> Html:
    return join([
        _benchmark_banner(summary),
        _benchmark_overview(summary),
        _benchmark_identity(summary),
        _runs_section(summary),
        _run_charts(summary),
        _aggregates(summary),
        _run_details(summary),
        _methodology(report_reasons(summary), benchmark=True),
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
            tag("div", tag("h3", "Machine"), _host_list(summary["host"])),
            tag("div", tag("h3", "Policy"), definition_list([
                ("Measured Runs requested", str(policy["repeats"])),
                ("Warm-up Runs requested", str(policy["warmup"])),
                ("Recording", plain(policy["recording"])),
                ("Execution", policy["execution"]),
                *_guard_items(policy),
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
            code(run["report_path"]),
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


def _bar_chart(id_: str, title: str, unit: str,
               bars: Sequence[tuple[str, Any, str, str]],
               spread: dict | None) -> Html:
    """Per-Run bars: (label, value or None, role, tone)."""
    values = [value for _l, value, _r, _t in bars if value is not None]
    if not values:
        return tag("figure", tag("h4", title),
                   tag("p", "No Run report has a value to draw.",
                       class_="empty-chart"), class_="chart", id=id_)

    def fmt(value: Any) -> str:
        return format_value(unit, value)

    top = max(values + ([spread["max"]] if spread and spread["max"] is not None
                        else []))
    top = top if top > 0 else 1
    width, left, bottom, height = 600.0, 8.0, 46.0, 220.0
    plot_h = height - bottom - 24
    slot = (width - left * 2) / len(bars)
    shapes: list[Any] = []
    base = 24 + plot_h
    if spread and spread["min"] is not None:
        y_max = base - plot_h * spread["max"] / top
        y_min = base - plot_h * spread["min"] / top
        y_med = base - plot_h * spread["median"] / top
        shapes += [
            tag("rect", tag("title", f"Aggregate spread: min "
                                     f"{fmt(spread['min'])}, max "
                                     f"{fmt(spread['max'])}"),
                x=f"{left}", y=f"{y_max:.2f}", width=f"{width - 2 * left}",
                height=f"{max(y_min - y_max, 1):.2f}", class_="spread"),
            tag("line", x1=f"{left}", x2=f"{width - left}", y1=f"{y_med:.2f}",
                y2=f"{y_med:.2f}", class_="median"),
            tag("text", f"median {fmt(spread['median'])}", x=f"{width - left}",
                y=f"{y_med - 4:.2f}", text_anchor="end", class_="median-label"),
        ]
    shapes.append(tag("line", x1=f"{left}", x2=f"{width - left}",
                      y1=f"{base}", y2=f"{base}", class_="axis"))
    described = []
    for index, (label, value, role, tone) in enumerate(bars):
        x = left + index * slot
        marker = {"fail": "✕ ", "warn": "! "}.get(tone, "")
        role_text = "warm-up" if role == "warmup" else ""
        if value is None:
            shapes.append(tag("text", "no value", x=f"{x + slot / 2:.2f}",
                              y=f"{base - 6}", text_anchor="middle",
                              class_="tick"))
            described.append(f"{label} no value")
        else:
            bar_h = plot_h * value / top
            shapes.append(tag(
                "rect", tag("title", f"{label}: {fmt(value)}"),
                x=f"{x + slot * 0.15:.2f}", y=f"{base - bar_h:.2f}",
                width=f"{slot * 0.7:.2f}", height=f"{bar_h:.2f}",
                class_=f"bar role-{role} tone-{tone}"))
            described.append(f"{label} {fmt(value)}"
                             + (" warm-up" if role == "warmup" else "")
                             + (" failed" if tone == "fail" else ""))
        every = max(1, math.ceil(70 / slot))
        short = label if slot >= 70 else label.rsplit("-", 1)[-1]
        if index % every == 0 or tone != "ok":
            shapes.append(tag("text", marker + short,
                              x=f"{x + slot / 2:.2f}", y=f"{base + 16}",
                              text_anchor="middle", class_="tick"))
        if role_text and slot >= 70:
            shapes.append(tag("text", role_text, x=f"{x + slot / 2:.2f}",
                              y=f"{base + 30}", text_anchor="middle",
                              class_="tick muted-text"))
    svg = tag("svg", *shapes, viewBox=f"0 0 {width:.0f} {height:.0f}",
              class_="bars-svg", role="img",
              aria_label=f"{title} per Run: " + "; ".join(described))
    return tag(
        "figure", tag("h4", title), svg,
        tag("figcaption",
            "Dashed outline bars are warm-ups; \u2715 marks a failed Run. ",
            "The band is the min-max spread and the line the median of the "
            "successful measured Runs, from the aggregates."
            if spread and spread["min"] is not None else
            "No aggregate spread: " + (
                ", ".join(sorted(set(spread["unavailable"].values())))
                if spread else "no aggregate metric for this scope") + "."),
        class_="chart", id=id_)


def _run_bars(summary: dict, value_of) -> list[tuple[str, Any, str, str]]:
    bars = []
    for run in summary["runs"]:
        _text, tone = _run_outcome(run)
        bars.append((run["label"], value_of(run["report"]), run["role"], tone))
    return bars


def _run_charts(summary: dict) -> Html:
    charts = [_bar_chart(
        "chart-wall", "Run wall time", "ns",
        _run_bars(summary, lambda r: _report_value(
            r, "observational.run_wall_time.wall_ns")),
        _metric(summary, "run_wall_ns"))]
    charts.append(_bar_chart(
        "chart-runner-rss", "Runner peak RSS (process_peak_rss)", "bytes",
        _run_bars(summary, lambda r: _report_value(
            r, "observational.resources.runner.peak_rss_bytes")),
        _metric(summary, "runner_peak_rss_bytes")))
    names = process_participant_names(run["report"]
                                      for run in summary["runs"])
    for index, name in enumerate(names):
        charts.append(_bar_chart(
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
    return join([tag("code", metric["metric"]),
                 tag("span", " ", metric["participant"], class_="name")
                 if "participant" in metric else None])


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
            body = join([_run_banner(report, level=4),
                         _run_body(report, f"run-{index}-", 4)])
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
