"""Partition CPU sample leaves by retained module/source identity."""

from __future__ import annotations

from collections import defaultdict

from sil.diagnostics_html_markup import Html, code, section, table, tag, void
from sil.profile_analysis import cpu_rows
from sil.profile_html_parts import share, source_cell
from sil.profile_report import SOURCE_DEBUG_STATUSES
from sil.profile_set import ProfileSet

LINE_ROWS = 50


def source_data(profile: ProfileSet) -> dict | None:
    if profile.samples is None:
        return None
    capture, document = profile.report["capture"], profile.samples
    modules = {m["id"]: m for m in capture["modules"]}
    available = capture["symbolization"]["source_lines"]["status"] != "unavailable"
    leaves = []
    locations: dict[tuple, int] = {}
    for stack in document["stacks"]:
        frame = document["frames"][stack[0]] if stack else None
        source = frame["source"] if frame else None
        module = modules.get(frame["module"]) if frame else None
        valid = (available and source is not None and source["file"]
                 and source["line"] > 0 and module is not None
                 and module["debug"]["status"] in SOURCE_DEBUG_STATUSES)
        location = ((frame["module"], source["file"], source["line"])
                    if valid else (None, None, None))
        leaves.append(locations.setdefault(location, len(locations)))
    return {"leaves": leaves, "locations": list(locations),
            "modules": {key: [m["path"], m["build_id"]]
                        for key, m in modules.items()}, "limit": LINE_ROWS}


def line_partition(profile: ProfileSet):
    """All selected rows partition into known lines and one unknown bucket."""
    data = source_data(profile)
    assert data is not None
    known: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])
    unknown = [0, 0]
    for row in cpu_rows(profile):
        leaf = tuple(data["locations"][data["leaves"][row[0]]])
        target = known[leaf] if leaf[1] is not None else unknown
        target[0] += row[4]
        target[1] += row[5]
    return data, known, unknown


def source_lines_section(profile: ProfileSet) -> Html:
    if profile.samples is None:
        return section("source-lines", "CPU source-line hotspots", tag(
            "p", "Unavailable: no retained normalized samples."))
    data, known, unknown = line_partition(profile)
    ranked = sorted(known.items(), key=lambda item: (-item[1][1], item[0]))
    shown, omitted = ranked[:LINE_ROWS], ranked[LINE_ROWS:]
    attributed = [sum(v[i] for v in known.values()) for i in (0, 1)]
    total = [attributed[i] + unknown[i] for i in (0, 1)]
    other = [sum(v[i] for _, v in omitted) for i in (0, 1)]
    rows = [[code(module), code(data["modules"][module][0]),
             code(data["modules"][module][1] or "[unknown build ID]"),
             source_cell([{"file": file, "line": line, "samples": count}], "samples"),
             str(count), f"{value} ns", share(value, total[1])]
            for (module, file, line), (count, value) in shown]
    footer = [[label, "", "", "", str(count), f"{value} ns", share(value, total[1])]
              for label, (count, value) in (
                  (f"Other known lines ({len(omitted)} omitted)", other),
                  ("Unknown/missing/untrusted leaf source", unknown),
                  ("All selected samples", total))]
    return section("source-lines", "CPU source-line hotspots",
        tag("div", tag("label", void("input", type="checkbox", id="source-selected"),
            " Only the selected caller-view function's leaf samples"),
            id="source-controls", hidden=True),
        tag("p", f"{attributed[0]} samples / {attributed[1]} ns attributed; "
            f"{unknown[0]} samples / {unknown[1]} ns unknown. "
            f"{share(attributed[1], total[1])} of selected weight has a retained leaf source line. "
            f"Denominator: {total[0]} samples / {total[1]} ns.",
            id="source-summary", aria_live="polite"),
        tag("div", table("source-line-table", "Retained leaf source locations, "
            f"top {LINE_ROWS} by sampled weight", [
                ("Module ID", False), ("Captured module path", False),
                ("Build ID", False), ("Captured file and line", False),
                ("Samples", True), ("Estimated weight", True),
                ("Share of selection", True)], rows, footer=footer),
            id="source-table-wrap"),
        note="Each sample contributes only its captured leaf location, never "
             "a location copied to an ancestor or inline caller. Module IDs, "
             "build IDs and captured paths stay distinct. Missing locations, "
             "line zero, empty paths, unavailable/mismatched debug evidence "
             "and unknown modules enter the unknown bucket. No source file "
             "is read. Counts and weights describe sampled self attribution, "
             "never execution counts, exact duration or code coverage. Known "
             "rows, omitted known lines and unknown samples reconcile with "
             "the selected denominator.")
