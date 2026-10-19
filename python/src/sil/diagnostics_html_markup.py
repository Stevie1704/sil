"""Markup for the HTML report (#304): escaped elements and shared parts.

`Html` is text that is already markup; `tag` and `join` escape every other
value, so names, paths and messages from a report are always text. The parts
here (sections, tables, cards, unavailable values) and the label maps are
shared by the Run and the benchmark page.
"""

from __future__ import annotations

import html
from typing import Any, Iterable, Sequence

from sil.diagnostics_format import format_value


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


PHASE_LABELS = {
    "preflight_ns": "Preflight",
    "setup_ns": "Setup",
    "scheduled_execution_ns": "Scheduled execution",
    "shutdown_ns": "Shutdown",
    "recording_finalization_ns": "Recording finalization",
    "unattributed_ns": "Unattributed",
}


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
          row_classes: Sequence[str | None] | None = None,
          footer: Sequence[Sequence[Any]] = ()) -> Html:
    """A table; a cell is markup, or (markup, sort key) for sorting.

    `footer` rows (a remainder, a total) stay last: sorting and filtering
    change the body only."""
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
    # Shown by the script while the table is wider than its box; without the
    # script, edge shadows (report.css) show that the table scrolls.
    hint = tag("p", "Scroll the table sideways for more columns \u2192",
               class_="scroll-hint", hidden=True)
    return tag(
        "div",
        tag("div", search, count, class_="table-tools") if search else None,
        hint,
        tag("div",
            tag("table", tag("caption", caption), tag("thead", head),
                tag("tbody", *body_rows),
                tag("tfoot", *(tag("tr", *(
                    tag("td", cell, class_="numeric" if numeric else None)
                    for (_, numeric), cell in zip(headers, row)))
                    for row in footer)) if footer else None, id=id_,
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
