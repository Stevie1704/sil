"""The standalone HTML view of a saved source-scope capture (#373).

docs/proposals/native-source-scopes.md, "Presentation": the views of the
terminal, offline, in the page frame of the other reports
(sil.diagnostics_html.page): the same policy, theme switch, keyboard-
scrollable tables, sorting and filtering. Every report text goes through
the escaping element builders. Each listed view is bounded at ROW_LIMIT rows
and names its remainder. An unavailable Activation shows its reason and
withheld count, never counts.
"""

from __future__ import annotations

from typing import Any

from sil.diagnostics_html import page
from sil.diagnostics_html_markup import (
    Html, code, definition_list, join, plain, section, table, tag,
)
from sil.profile_html_parts import (
    CAPTURE_LABELS, RUN_LABELS, run_detail, status_line,
)
from sil.source_scope_report import LIMITATIONS
from sil.source_scope_set import ScopeSet
from sil.source_scope_summary import (
    ROW_LIMIT, activation_title, scope_names,
)


def render_scopes(scope_set: ScopeSet) -> str:
    """The page of a checked capture; the download is scopes.json."""
    return page("SiL source scopes", _body(scope_set), scope_set.source,
                styles=("report.css", "profile.css"), scripts=("report.js",),
                download_name="scopes.json")


def _body(scope_set: ScopeSet) -> Html:
    report = scope_set.report
    return join([
        _banner(report),
        section("limitations", "Limitations",
                tag("ul", *(tag("li", item) for item in LIMITATIONS),
                    class_="limitations"),
                note="These statements apply to every value of this view."),
        _identities(report),
        _tasks(report["summary"]["tasks"]),
        _scopes(report["summary"]["scopes"], scope_set.scope_map),
        _activations(scope_set),
        _unattributed(scope_set.calls),
        _files(report)])


def _banner(report: dict) -> Html:
    run, capture = report["run"], report["capture"]
    run_label, run_tone = RUN_LABELS.get(run["status"], (run["status"], "warn"))
    capture_label, capture_tone = CAPTURE_LABELS.get(
        capture["status"], (capture["status"], "warn"))
    loss = capture["loss"]
    loss_text = ("unknown (the raw header could not be read)" if loss is None
                 else ", ".join(f"{k} {v}" for k, v in loss.items()))
    tone = "fail" if "fail" in (run_tone, capture_tone) else \
        "warn" if "warn" in (run_tone, capture_tone) else "ok"
    return tag(
        "section",
        tag("p", "Source scopes", class_="eyebrow"),
        tag("h1", f"{run_label} · {capture_label}", class_="status-title"),
        tag("ul",
            status_line(run_label, run_tone, run_detail(run)),
            status_line(capture_label, capture_tone,
                        f"capture ID {capture['capture_id'] or 'unknown'}",
                        capture["reasons"]),
            status_line("Loss", "ok" if loss and not any(loss.values())
                        else "warn", loss_text),
            status_line("Records kept", "ok",
                        "unknown" if capture["records"] is None
                        else str(capture["records"])),
            class_="status-lines"),
        tag("p", "The Run outcome and the capture outcome are separate: a "
                 "capture problem never replaces the Run's exit code.",
            class_="banner-detail"),
        class_=f"banner tone-{tone}", aria_label="Status")


def _library(identity: dict | None) -> Html:
    if identity is None:
        return plain(None, "not recorded")
    return tag("span", code(identity["path"]), " · build ID ",
               code(identity["build_id"] or "unknown"), " · SHA-256 ",
               code(identity["sha256"] or "unknown"))


def _scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _identities(report: dict) -> Html:
    identities = report["identities"]
    configuration = report["configuration"]
    return section(
        "identities", "Builds and configuration",
        definition_list([
            ("Instrumented build", _library(identities.get("instrumented"))),
            ("Original build", _library(identities.get("original"))),
            ("Observer version", plain(identities.get("observer_version"),
                                       "unknown")),
            ("Compiler", plain(identities.get("compiler"), "unknown")),
            ("Producer", f"{report['producer']['name']} "
                         f"{report['producer'].get('sil_version')}"),
            *((key, plain(value)) for key, value in configuration.items()
              if _scalar(value))]),
        note="Durations describe the instrumented build only.")


def _tasks(tasks: list) -> Html:
    rows = [[t["participant"], t["task"], str(t["activations"]),
             str(t["exact"]), str(sum(t["unavailable"].values())),
             ", ".join(f"{r} {n}" for r, n in t["unavailable"].items())]
            for t in tasks]
    return section("tasks", "Activations per Task", table(
        "tasks-table", "Exact and unavailable Activations per Task",
        [("Participant", False), ("Task", False), ("Activations", True),
         ("Exact", True), ("Unavailable", True), ("Reasons", False)], rows))


def _scopes(scopes: list, scope_map: dict | None) -> Html:
    sites = {s["id"]: s["sites"] for s in (scope_map or {}).get("scopes", [])}
    rows = []
    for scope in scopes:
        per = scope["per_exact_activation"]
        rows.append([
            (str(scope["id"]), str(scope["id"])), scope["name"],
            ", ".join(f"{s['file']}:{s['line']}"
                      for s in sites.get(scope["id"], [])) or "unavailable",
            (str(scope["attributed_calls"]), str(scope["attributed_calls"])),
            *(("unavailable", "") if per[k] is None else (str(per[k]),
                                                          str(per[k]))
              for k in ("min", "median", "max"))])
    return section("scopes", "Calls per scope", table(
        "scopes-table", "Attributed calls per scope",
        [("ID", True), ("Scope", False), ("Sites", False),
         ("Attributed calls", True), ("Min per exact Activation", True),
         ("Median", True), ("Max", True)], rows),
        note="Counts come from exact Activations only.")


def _bounded(rows: list, noun: str, extra: int = 0) -> tuple[list, list]:
    """The first ROW_LIMIT rows and a footer that names the rest."""
    rest = len(rows) - min(len(rows), ROW_LIMIT) + extra
    return rows[:ROW_LIMIT], ([[f"{rest} more {noun} not shown"]]
                              if rest else [])


def _activations(scope_set: ScopeSet) -> Html:
    calls = scope_set.calls
    if calls is None:
        return section("activations", "Activations",
                       tag("p", "unavailable: no decoded calls file",
                           class_="na"))
    activations = calls["activations"] if scope_set.selection is None else \
        [calls["activations"][i] for i in scope_set.selection]
    shown, footer = _bounded([[activation_title(a)] for a in activations],
                             "Activations")
    parts = [tag("p", f"Selection: {scope_set.selection_label}")
             if scope_set.selection_label else None,
             table("activations-table", "Activations and their status",
                   [("Activation", False)], shown, footer=footer)]
    if len(activations) == 1 and activations[0]["status"] == "exact":
        parts.append(_calls(activations[0], scope_names(scope_set)))
    return section("activations", "Activations", *parts)


def _calls(activation: dict, names: dict[int, str]) -> Html:
    calls, footer = _bounded(activation["calls"], "calls")
    rows = [[str(c["depth"]),
             "  " * c["depth"] + names.get(c["scope"],
                                                     "[unnamed scope]"),
             str(c["scope"]), c["entry_ns"], c["return_ns"], c["elapsed_ns"]]
            for c in calls]
    return section("call-order", "Calls in entry order", table(
        "calls", "Calls of the selected Activation in entry order",
        [("Depth", True), ("Scope", False), ("ID", True),
         ("Entry (host ns)", True), ("Return (host ns)", True),
         ("Elapsed (host ns)", True)], rows, footer=footer,
        filterable=False), level=3,
        note="Elapsed time is inclusive: nested durations overlap.")


def _unattributed(calls: dict | None) -> Html | None:
    if calls is None:
        return None
    unattributed = calls["unattributed"]
    reasons = table("unattributed-reasons", "Unattributed calls per reason",
                    [("Reason", False), ("Calls", True)],
                    [[r, str(n)] for r, n in unattributed["reasons"].items()])
    rows = [[str(r["scope"]), str(r["tid"]), r["entry_ns"] or "none",
             r["return_ns"] or "none", r["reason"]]
            for r in unattributed["rows"]]
    shown, footer = _bounded(rows, "rows", unattributed["rows_remainder"])
    return section("unattributed", "Unattributed calls", reasons, table(
        "unattributed-rows", "Unattributed rows in entry order",
        [("Scope", True), ("Thread", True), ("Entry (host ns)", True),
         ("Return (host ns)", True), ("Reason", False)], shown,
        footer=footer))


def _files(report: dict) -> Html:
    rows = [[a["role"], code(a["path"]), str(a["bytes"]), code(a["sha256"])]
            for a in report["artifacts"]]
    rows += [[m["role"], code(m["path"]), "unavailable",
              f"unavailable ({m['reason']})"] for m in report["missing"]]
    return section("files", "Files", table(
        "files-table", "Verified and missing files",
        [("Role", False), ("Path", False), ("Bytes", True),
         ("SHA-256", False)], rows))
