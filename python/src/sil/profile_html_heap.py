"""The page body of a heap profile (#318).

Status, scope and method first, then the totals with their boundaries, the
allocation-site rankings by bytes, churn (temporary allocations) and
retention (outstanding at capture end), each with a remainder and the
total, the inclusive sites and the caller tree of `heap/allocations.json`.
Heaptrack has no validated clock mapping, so there are process and
call-site views only, never a view per Activation or Step.
"""

from __future__ import annotations

from typing import Any

from sil.diagnostics_format import format_bytes
from sil.diagnostics_html_markup import Html, card, code, join, plain, \
    section, table, tag
from sil.profile_html_parts import (
    TreeLimits, banner, build_tree, function_cell, identity_section,
    limitations_section, reason_text, reasons_list, render_tree, share,
    source_cell,
)
from sil.profile_report import HEAP_METRICS
from sil.profile_set import ProfileSet

RANKING_ROWS = 25
RANKINGS = (
    ("bytes", "allocated_bytes", "Allocation sites by bytes allocated"),
    ("churn", "temporary_allocations",
     "Churn: sites by temporary allocations"),
    ("retention", "outstanding_bytes",
     "Retention: sites by bytes outstanding at capture end"),
)


def heap_body(profile: ProfileSet) -> Html:
    report = profile.report
    capture = report["capture"]
    correlation = capture.get("activation_correlation")
    status = None
    if correlation is not None:
        status = ("Activation correlation unavailable", "warn",
                  "process and call-site views only",
                  [correlation["reason"]])
    analysis = capture["analysis"]
    return join([
        banner(report, "Heap profile", status),
        identity_section(report, _method(capture)),
        limitations_section(report),
        _no_analysis(capture) if analysis is None else join([
            _totals(analysis),
            *(_ranking(analysis, *ranking) for ranking in RANKINGS),
            _inclusive(analysis),
            _tree(profile),
            _modules(analysis),
        ]),
    ])


def _scope(scope: dict) -> Html:
    if scope["kind"] == "runner":
        return Html("the runner process: every Native participant together "
                    "with the runner's own work")
    if scope["kind"] == "process_participant":
        return join(["Process participant ",
                     tag("span", scope["participant"], class_="name")])
    return join(["unsupported: ", code(scope["requested"]), " (",
                 reason_text(scope["reason"]), ")"])


def _method(capture: dict) -> list[tuple[str, Any]]:
    backend = capture["backend"]
    rows: list[tuple[str, Any]] = [
        ("Collector", join([backend["name"], " ",
                            plain(backend.get("version"), "version unknown")])),
        ("Scope", _scope(capture["scope"])),
    ]
    if "launch" in capture:
        launch, limits = capture["launch"], capture["limits"]
        rows += [
            ("Launch", join([code(launch["method"]), " wraps the ",
                             launch["wrapped"]])),
            ("Preload library", code(backend["preload"])),
            ("Interpreter", join([code(backend["interpreter"]), " exit code ",
                                  plain(backend["interpreter_exit_code"],
                                        "none (stopped)")])),
            ("Capture size", f"{limits['capture_bytes']} of "
                             f"{limits['max_capture_bytes']} bytes"
                             + (" (limit reached)" if limits["limit_reached"]
                                else "")),
        ]
    analysis = capture["analysis"]
    if analysis is not None:
        rows += [("Data file", f"version {analysis['file_version']}, "
                               f"Heaptrack {analysis['heaptrack_version']}"),
                 ("Traced command", code(analysis["traced_executable"])
                  if analysis["traced_executable"] else plain(None))]
    return rows


def _no_analysis(capture: dict) -> Html:
    return section("allocations", "Allocations", tag(
        "div", tag("p", tag("strong", "No allocation analysis."),
                   " The capture has no data that decodes, so there is no "
                   "total and no site. That is not evidence of no "
                   "allocation."),
        reasons_list(capture["reasons"]) or reasons_list(
            list(capture.get("unavailable", {}).values())),
        class_="callout tone-warn"))


def _totals(analysis: dict) -> Html:
    totals, coverage = analysis["totals"], analysis["coverage"]
    count = totals["allocations"]
    cards = [
        card("Allocations", str(count), "calls of the interposed allocation "
                                        "functions: the denominator of counts"),
        card("Bytes allocated", format_bytes(totals["allocated_bytes"]),
             "requested sizes, summed: not live memory, not overhead"),
        card("Temporary allocations", str(totals["temporary_allocations"]),
             "freed directly after their allocation: churn"),
        card("Outstanding at capture end",
             f"{totals['outstanding_allocations']} allocations",
             f"{format_bytes(totals['outstanding_bytes'])}: a snapshot, "
             "not a leak verdict"),
        card("Peak tracked heap", format_bytes(totals["peak_tracked_bytes"]),
             "tracked heap only: not RSS, stack, mapped pages or overhead"),
        card("Frees", str(totals["frees"])),
    ]
    unknown = (coverage["allocations_unknown_module"] +
               coverage["allocations_unknown_symbol"])
    notes = [tag("p", f"Coverage: {unknown} of {count} allocations have no "
                      f"function name ({coverage['allocations_unknown_module']}"
                      f" without a module); {coverage['allocations_with_caller']}"
                      f" have a caller; {coverage['undecodable_records']} "
                      "records did not decode"
                      + ("; the data is cut at its end" if coverage["truncated"]
                         else "") + ".", class_="note")]
    return section("allocations", "Allocations",
                   tag("div", *cards, class_="cards"), *notes,
                   note="Each value counts each allocation event once, over "
                        "the whole life of the scope process under "
                        "Heaptrack, setup and shutdown included.")


HEADERS = [("#", True), ("Function and module", False),
           ("Allocations", True), ("Bytes allocated", True),
           ("Temporary", True), ("Outstanding", True),
           ("Outstanding bytes", True), ("Innermost source lines", False)]


def _cells(row: dict) -> list:
    return [(str(row["allocations"]), str(row["allocations"])),
            (tag("span", format_bytes(row["allocated_bytes"]), class_="num"),
             str(row["allocated_bytes"])),
            (str(row["temporary_allocations"]),
             str(row["temporary_allocations"])),
            (str(row["outstanding_allocations"]),
             str(row["outstanding_allocations"])),
            (tag("span", format_bytes(row["outstanding_bytes"]),
                 class_="num"), str(row["outstanding_bytes"]))]


def _rows(rows: list, modules: dict) -> list:
    return [[str(index + 1),
             function_cell(row["symbol"], modules.get(row["module"])),
             *_cells(row), source_cell(row["source_lines"], "allocations")]
            for index, row in enumerate(rows)]


def _modules_by_id(analysis: dict) -> dict:
    return {m["id"]: m["path"] for m in analysis["modules"]}


def _ranking(analysis: dict, id_: str, metric: str, title: str) -> Html:
    totals = analysis["totals"]
    rows = sorted((r for r in analysis["hotspots"]["self"] if r[metric]),
                  key=lambda r: -r[metric])[:RANKING_ROWS]
    rest = {key: totals[key] - sum(r[key] for r in rows)
            for key in HEAP_METRICS}
    footer = [["", "Other sites (not listed)", *(
        cell[0] for cell in _cells(rest)), ""],
        ["", tag("strong", "All allocations"),
         *(cell[0] for cell in _cells(totals)), ""]]
    return section(
        f"heap-{id_}", title,
        table(f"{id_}-table", f"{title}, top {RANKING_ROWS}; the innermost "
              "frame of each allocation", HEADERS, _rows(rows,
                                                         _modules_by_id(
                                                             analysis)),
              footer=footer),
        note=f"Share of all {metric.replace('_', ' ')} in the listed rows: "
             f"{share(totals[metric] - rest[metric], totals[metric])}. "
             "The listed rows and the remainder add up to the totals.",
        level=3)


def _inclusive(analysis: dict) -> Html:
    return section(
        "heap-inclusive", "Sites by inclusive bytes",
        table("heap-inclusive-table", "Functions anywhere in the allocation "
              "stack", HEADERS, _rows(analysis["hotspots"]["inclusive"],
                                      _modules_by_id(analysis))),
        note="Inclusive: each function anywhere in an allocation's stack, "
             "once per allocation. Inclusive values overlap and are never "
             "totals.", level=3)


def _tree(profile: ProfileSet) -> Html:
    if profile.allocations is None:
        return section("tree", "Allocation caller tree", tag(
            "p", "No allocations file, so there is no caller tree.",
            class_="note"))
    document = profile.allocations
    keys: dict[tuple, int] = {}
    frame_keys = [keys.setdefault((f["module"], f["symbol"]), len(keys))
                  for f in document["frames"]]
    functions = list(keys)
    paths = [[frame_keys[f] for f in reversed(stack)]
             for stack in document["stacks"]]
    tree = build_tree((paths[stack], count, size * count)
                      for stack, size, count, _, _ in document["allocations"])
    modules = _modules_by_id(profile.report["capture"]["analysis"])

    def label(key: int) -> Html:
        module, symbol = functions[key]
        return function_cell(symbol, modules.get(module))

    return section(
        "tree", "Allocation caller tree",
        tag("div", render_tree(tree, label, format_bytes, "allocations",
                               TreeLimits()),
            id="heap-tree", class_="tree-wrap", tabindex="0", role="region",
            aria_label="Allocation caller tree"),
        note="Top-down by bytes allocated. Self is the innermost frame. "
             "Small nodes and many siblings are grouped into a visible "
             "remainder.")


def _modules(analysis: dict) -> Html:
    rows = [[tag("code", m["path"], class_="path"),
             plain(m["build_id"], "none") if m["build_id"] or not
             m["unavailable"].get("build_id") else
             join(["unknown (", reason_text(m["unavailable"]["build_id"]),
                   ")"]),
             str(m["allocations_leaf"]), str(m["allocations_unknown_symbol"])]
            for m in analysis["modules"]]
    return section("modules", "Modules", table(
        "module-table", "Modules of the traced process", [
            ("Path", False), ("Build ID", False),
            ("Innermost allocations", True),
            ("Without function name", True)], rows),
        note="Heaptrack records paths, not build IDs: the build ID is the "
             "one of the file after the Run.")
