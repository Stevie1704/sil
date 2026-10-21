"""The page body of a CPU profile (#318).

The status, the method and the coverage come first, then the hotspot tables
of `profile.json` (self and inclusive, with the remainder and the total),
the caller tree of `cpu/samples.json`, the Activation evidence of
`activation/correlation.json` and the processes, threads and modules.

Without JavaScript every table and the caller tree of all samples are
complete. The script (report_assets/profile.js) adds filters by class,
Participant/Task, Virtual time, process and thread; it reads `explorer_data`,
which carries only names, identities, counts and weights.
"""

from __future__ import annotations

from collections import Counter
from pathlib import PurePosixPath
from typing import Any

from sil.diagnostics_format import format_duration_ns
from sil.diagnostics_html_markup import (
    Html, card, code, join, plain, section, table, tag, void,
)
from sil.profile_html_parts import (
    CORRELATION_LABELS, TreeLimits, banner, build_tree, function_cell,
    identity_section, limitations_section, reason_text, reasons_list,
    render_tree, share, source_cell, unlisted_note,
)
from sil.profile_set import ProfileSet
from sil.profile_analysis import profile_tasks
from sil.profile_html_views import TASK_LIMIT, flame_section, task_drilldowns, timeline

HOTSPOT_ROWS = 50
ACTIVATION_ROWS = 500
MAX_EXPLORER_ROWS = 200_000
CLASS_TEXT = {
    "execution_context":
        "Runner thread inside a Native activation or a Process Step: the "
        "Activation's inclusive execution context (routing and Recording "
        "work that it called included).",
    "window_associated":
        "A Process participant's process tree inside one of its Steps: "
        "associated with the Step window only, not proven to be the Step's "
        "own work.",
    "lifecycle": "Runner thread in a lifecycle phase, outside every "
                 "Activation.",
    "replay": "Runner thread in a Replay publication Slot: not a Task.",
    "unattributed": "Every other sample, with its reason.",
}
CLASSES = tuple(CLASS_TEXT)


def weight(value: int) -> str:
    return format_duration_ns(value)


def cpu_body(profile: ProfileSet) -> Html:
    report = profile.report
    capture = report["capture"]
    correlation = report["activation_correlation"]
    return join([
        banner(report, "CPU profile", _correlation_status(correlation)),
        identity_section(report, _method(capture)),
        _coverage(capture),
        limitations_section(report),
        _hotspots(capture, profile.samples),
        _tree_section(profile),
        _activations(profile),
        timeline(profile),
        task_drilldowns(profile),
        _processes(capture),
        _modules(capture),
    ])


def _correlation_status(correlation: dict):
    if correlation["kind"] == "not_requested":
        return ("Activation correlation not requested", "ok",
                "run with --activations to relate samples to Tasks", ())
    label, tone = CORRELATION_LABELS.get(correlation["status"],
                                         (correlation["status"], "warn"))
    return label, tone, None, correlation["reasons"]


def _method(capture: dict) -> list[tuple[str, Any]]:
    sampling, limits = capture["sampling"], capture["limits"]
    backend = capture["backend"]
    return [
        ("Collector", f"{backend['name']} ({backend['version']})"),
        ("Event", join([code(sampling["event"]),
                        f" ({sampling['event_type']}, "
                        f"{sampling['privilege']} space)"])),
        ("Sampling", f"{sampling['mode']} {sampling['frequency_hz']} Hz"),
        ("Sample weight", join([code(sampling["weight"]), " in ",
                                code(sampling["weight_unit"]),
                                ": sampled on-CPU time, an estimate"])),
        ("Clock", code(sampling["clock"])),
        ("Call graph", f"{capture['call_graph']['method']}, "
                       f"{capture['call_graph']['stack_bytes']} stack bytes "
                       "per sample"),
        ("Scope", join([code(capture["scope"]["kind"]),
                        " (inherit: ", plain(capture["scope"]["inherit"]),
                        "; PID namespace: ",
                        code(capture["scope"]["pid_namespace"]), ")"])),
        ("Capture size", f"{limits['capture_bytes']} of "
                         f"{limits['max_capture_bytes']} bytes"
                         + (" (limit reached)" if limits["limit_reached"]
                            else "")),
        ("Time origin", join([code(capture["time_origin_ns"])
                              if capture["time_origin_ns"] else
                              plain(None, "none"),
                              " ns host CLOCK_MONOTONIC"])),
        ("Backend command", tag("details", tag("summary", "show"),
                                code(" ".join(backend["command"])))),
    ]


def _coverage(capture: dict) -> Html:
    totals, symbols = capture["totals"], capture["symbolization"]
    samples = totals["samples"]
    lost = totals["lost_samples"]
    source = symbols["source_lines"]
    lines = source["samples_with_line"]
    of = f"of {samples} samples"
    cards = [
        card("Samples", str(samples), "the denominator of every share"),
        card("Sampled weight", weight(totals["weight_ns"]),
             "estimated on-CPU time, not a duration"),
        card("Lost samples", "unknown" if lost is None else str(lost),
             f"{totals['lost_records']} lost records"),
        card("Function names", str(symbols["function_names"][
            "samples_with_name"]), f"leaf frames named, {of}"),
        card("Unknown leaf", str(totals["samples_unknown_symbol"]
                                 + totals["samples_unknown_module"]),
             f"{totals['samples_unknown_module']} without a module, "
             f"{totals['samples_unknown_symbol']} without a function name"),
        card("With a caller", str(totals["samples_with_caller"]),
             f"DWARF unwinding found a caller, {of}"),
        card("Source lines", "unavailable" if lines is None else str(lines),
             f"{source['status']}, {source['scope']}"),
    ]
    notes = [tag("p", "No sample: this is not evidence that no CPU time was "
                      "used.", class_="note warn")] if samples == 0 else []
    if capture["reasons"]:
        notes.append(tag("div", tag("p", "The capture is not complete:"),
                         reasons_list(capture["reasons"]),
                         class_="callout tone-warn"))
    unavailable = {**capture.get("unavailable", {}),
                   **source.get("unavailable", {})}
    if unavailable:
        notes.append(tag("p", "Unavailable: ", join(
            join([code(k), " ", reason_text(v), "; "])
            for k, v in unavailable.items()), class_="note"))
    return section("coverage", "Coverage and loss",
                   tag("div", *cards, class_="cards"), *notes,
                   note="Read these before the hotspots: a hotspot share is "
                        "a share of the samples that the capture kept.")


def _modules_by_id(capture: dict) -> dict[str, str]:
    return {m["id"]: m["path"] for m in capture["modules"]}


def _hotspot_rows(rows: list, modules: dict, total: int) -> list:
    return [[str(index + 1),
             function_cell(row["symbol"], modules.get(row["module"])),
             (str(row["self_samples"]), str(row["self_samples"])),
             (tag("span", weight(row["self_weight_ns"]), class_="num"),
              str(row["self_weight_ns"])),
             (share(row["self_weight_ns"], total), str(row["self_weight_ns"])),
             (tag("span", weight(row["inclusive_weight_ns"]), class_="num"),
              str(row["inclusive_weight_ns"])),
             (share(row["inclusive_weight_ns"], total),
              str(row["inclusive_weight_ns"])),
             source_cell(row["source_lines"], "samples")]
            for index, row in enumerate(rows)]


HOTSPOT_HEADERS = [("#", True), ("Function and module", False),
                   ("Self samples", True), ("Self weight", True),
                   ("Self share", True), ("Inclusive weight", True),
                   ("Inclusive share", True), ("Leaf source lines", False)]


def _hotspots(capture: dict, samples: dict | None) -> Html:
    hotspots, totals = capture["hotspots"], capture["totals"]
    modules = _modules_by_id(capture)
    total = totals["weight_ns"]
    remainder = hotspots["self_remainder"]
    footer = [
        ["", "Other functions (not listed)", str(remainder["samples"]),
         weight(remainder["weight_ns"]), share(remainder["weight_ns"], total),
         "", "", ""],
        ["", tag("strong", "All samples"), str(totals["samples"]),
         weight(total), share(total, total), "", "", ""]]
    return section(
        "hotspots", "Hotspots",
        tag("h3", "By self weight"),
        tag("p", "Self: the function is the leaf frame of the sample. The "
                 "listed rows and the remainder add up to all samples.",
            class_="note"),
        table("self-table", f"Functions by self weight, top "
              f"{hotspots['limit']}", HOTSPOT_HEADERS,
              _hotspot_rows(hotspots["self"], modules, total),
              footer=footer),
        tag("h3", "By inclusive weight"),
        tag("p", "Inclusive: the function is anywhere in the stack, counted "
                 "once per sample. Inclusive values overlap, so they do not "
                 "add up.", class_="note"),
        table("inclusive-table", f"Functions by inclusive weight, top "
              f"{hotspots['limit']}", HOTSPOT_HEADERS,
              _hotspot_rows(hotspots["inclusive"], modules, total)),
        tag("p", unlisted_note(samples, len(hotspots["inclusive"])),
            class_="note"),
        note="Shares are of all sample weight ("
             f"{weight(total)} in {totals['samples']} samples). A weight is "
             "a sampled estimate of on-CPU time, never an exact duration.")


# --- caller tree and explorer -------------------------------------------


def _function_keys(profile: ProfileSet) -> tuple[list, list]:
    """Each frame's function key (module, symbol), and the keys."""
    assert profile.samples is not None
    keys: dict[tuple, int] = {}
    frame_keys = []
    for frame in profile.samples["frames"]:
        key = (frame["module"], frame["symbol"])
        frame_keys.append(keys.setdefault(key, len(keys)))
    keys.setdefault((None, None), len(keys))
    return frame_keys, list(keys)


def _paths(profile: ProfileSet, frame_keys: list) -> list[list[int]]:
    assert profile.samples is not None
    unknown = len(set(frame_keys))
    for index, frame in enumerate(profile.samples["frames"]):
        if frame["module"] is None and frame["symbol"] is None:
            unknown = frame_keys[index]
            break
    return [[frame_keys[f] for f in reversed(stack)] or [unknown]
            for stack in profile.samples["stacks"]]


def _label(functions: list, modules: dict):
    def label(key: int) -> Html:
        module, symbol = functions[key]
        name = (tag("span", symbol, class_="fn") if symbol is not None
                else tag("span", "[unknown function]", class_="na"))
        path = modules.get(module)
        where = (tag("code", PurePosixPath(path).name, title=path,
                     class_="module") if path else
                 tag("span", "[unknown module]", class_="na"))
        return join([name, " ", where])
    return label


def _tree_section(profile: ProfileSet) -> Html:
    capture = profile.report["capture"]
    if profile.samples is None:
        return section("tree", "Caller tree", tag(
            "p", "No samples file, so there is no caller tree: ",
            reason_text(capture["unavailable"].get("samples_file",
                                                   "no_samples")),
            class_="note"))
    frame_keys, functions = _function_keys(profile)
    paths = _paths(profile, frame_keys)
    tree = build_tree((paths[row[4]], 1, row[3])
                      for row in profile.samples["samples"])
    modules = _modules_by_id(capture)
    return section(
        "tree", "Caller tree",
        _explorer_form(profile),
        tag("div", render_tree(tree, _label(functions, modules), weight,
                               "samples", TreeLimits()),
            id="cpu-tree", class_="tree-wrap", tabindex="0", role="region",
            aria_label="Caller tree"),
        tag("div", id="selection-tables"),
        flame_section(tree, lambda key: f"{functions[key][1] or '[unknown function]'} "
                      f"({modules.get(functions[key][0], '[unknown module]')})"),
        note="Top-down: each node is a function in its callers' context, "
             "with its inclusive weight, its share of the tree's weight and "
             "its self weight. Small nodes, many siblings and very deep "
             "frames are grouped into a visible remainder, so each level "
             "still adds up. Recursion gives deeper nodes; no sample counts "
             "twice in one node.")


def _explorer_form(profile: ProfileSet) -> Html:
    assert profile.samples is not None
    capture = profile.report["capture"]
    correlation = profile.report["activation_correlation"]
    sampled = [p for p in capture["processes"] if p["samples"]]
    threads = [t for t in capture["threads"] if t["samples"]]
    fields = [
        _select("f-process", "Process", [
            (p["id"], f"{p['id']} pid {p['pid']} {p['commands'][-1]} "
                      f"({p['role']})") for p in sampled]),
        _select("f-thread", "Thread", [
            (t["id"], f"{t['id']} tid {t['tid']} {t['commands'][-1]}")
            for t in threads]),
    ]
    if profile.correlation is not None:
        fields = [
            _select("f-class", "Class", [(c, c) for c in CLASSES]),
            _select("f-task", "Participant / Task", [
                (str(index), f"{p} / {t}")
                for index, (p, t) in enumerate(profile_tasks(profile))]),
            tag("label", "Virtual time from (ns, inclusive)",
                void("input", id="f-vt-from", type="text",
                     inputmode="numeric", pattern="[0-9]*",
                     autocomplete="off")),
            tag("label", "Virtual time to (ns, exclusive)",
                void("input", id="f-vt-to", type="text", inputmode="numeric",
                     pattern="[0-9]*", autocomplete="off")),
            *fields]
        hint = ("Participant, Task and Virtual time select the samples that "
                "the correlation relates to an Activation: execution "
                "context and window-associated samples. Virtual time is "
                "the Activation's, as [from, to).")
    else:
        reasons = correlation.get("reasons") or ["not_requested"]
        hint = join(["No filter by Participant, Task or Virtual time: no "
                     "sample is related to an Activation (",
                     join(code(r) for r in reasons), ")."])
    if len(profile.samples["samples"]) and _explorer_rows(profile) is None:
        return tag("p", "The filters are not available: the samples have "
                        f"more than {MAX_EXPLORER_ROWS} distinct rows. The "
                        "tables and the tree show all samples.",
                   class_="note")
    return tag("form", tag("fieldset", tag("legend", "Filter the samples"),
                           *fields, class_="filters"),
               tag("p", hint, class_="note"),
               tag("p", "", id="selection", class_="selection",
                   aria_live="polite"),
               tag("button", "Clear filters", type="reset", id="f-clear"),
               id="explorer", hidden=True)


def _select(id_: str, label: str, options: list[tuple[str, str]]) -> Html:
    return tag("label", label, tag(
        "select", tag("option", "all", value=""),
        *(tag("option", text, value=value) for value, text in options),
        id=id_))


def _explorer_rows(profile: ProfileSet) -> list | None:
    """Samples with equal stack, thread and class in one row, or None."""
    assert profile.samples is not None
    classes = (profile.correlation["samples"] if profile.correlation
               else None)
    rows: Counter = Counter()
    weights: Counter = Counter()
    for index, sample in enumerate(profile.samples["samples"]):
        cls, ref = classes[index] if classes else ("", "")
        key = (sample[4], sample[2], cls, ref)
        rows[key] += 1
        weights[key] += sample[3]
        if len(rows) > MAX_EXPLORER_ROWS:
            return None
    return [[*key, rows[key], weights[key]] for key in rows]


def explorer_data(profile: ProfileSet) -> dict | None:
    """What profile.js needs for the filters; None without samples."""
    if profile.samples is None:
        return None
    rows = _explorer_rows(profile)
    if rows is None:
        return None
    capture = profile.report["capture"]
    frame_keys, functions = _function_keys(profile)
    modules = _modules_by_id(capture)
    limits = TreeLimits()
    activations = None
    if profile.correlation is not None:
        # Virtual times as decimal strings: the script compares BigInts.
        tasks = {task: index for index, task in enumerate(profile_tasks(profile))}
        activations = [[tasks[a["participant"], a["task"]],
                        str(a["virtual_time_ns"])]
                       for a in profile.correlation["activations"]]
    return {
        "functions": [[symbol, modules.get(module)]
                      for module, symbol in functions],
        "paths": _paths(profile, frame_keys),
        "rows": rows,
        "threads": {t["id"]: t["process"] for t in capture["threads"]},
        "tasks": [list(task) for task in profile_tasks(profile)]
        if profile.correlation is not None else None,
        "activations": activations,
        "total": [capture["totals"]["samples"], capture["totals"]["weight_ns"]],
        "limits": {"min_share": limits.min_share,
                   "max_children": limits.max_children,
                   "max_depth": limits.max_depth,
                   "max_nodes": limits.max_nodes, "rows": HOTSPOT_ROWS},
    }


# --- Activations ----------------------------------------------------------


def _activations(profile: ProfileSet) -> Html:
    correlation = profile.report["activation_correlation"]
    if correlation["kind"] == "not_requested":
        return section("activations", "Activations", tag(
            "p", "Not requested: this capture ran without --activations, so "
                 "no sample is related to a Task, a Step or a Virtual time.",
            class_="note"))
    summary = correlation["summary"]
    if summary is None:
        return section("activations", "Activations", tag(
            "div", tag("p", tag("strong", "No sample is related to an "
                                          "Activation."),
                       " The join of the two clocks or processes is not "
                       "proven, so this report shows no Activation view:"),
            reasons_list(correlation["reasons"]),
            class_="callout tone-warn"))
    samples = summary["samples"]
    total = samples["total"]
    class_rows = [[code(name), CLASS_TEXT[name],
                   str(samples[name]["samples"]),
                   weight(samples[name]["weight_ns"]),
                   share(samples[name]["weight_ns"], total["weight_ns"])]
                  for name in CLASSES]
    reasons = samples["unattributed"]["reasons"]
    content = [
        tag("div", tag("p", tag("strong", "The correlation is partial."),
                       " Activations after the gap are missing; their "
                       "samples are unattributed:"),
            reasons_list(correlation["reasons"]),
            class_="callout tone-warn")
        if correlation["status"] == "partial" else None,
        tag("p", "Intervals are half open, [begin, end). No class is an "
                 "exclusive, causal or ownership attribution, and a sample "
                 "weight is not an Activation's duration. An Activation "
                 "without samples is not evidence that it used no CPU time.",
            class_="note"),
        table("class-table", "Samples by class", [
            ("Class", False), ("Meaning", False), ("Samples", True),
            ("Weight", True), ("Share", True)], class_rows,
            filterable=False, footer=[[tag("strong", "All samples"), "",
                                       str(total["samples"]),
                                       weight(total["weight_ns"]), "100.0%"]]),
        table("unattributed-table", "Unattributed samples by reason", [
            ("Reason", False), ("Samples", True), ("Weight", True)],
            [[reason_text(r["reason"]), str(r["samples"]),
              weight(r["weight_ns"])] for r in reasons], filterable=False),
        _tasks_table(summary, profile),
        table("lifecycle-table", "Lifecycle samples by phase", [
            ("Phase", False), ("Samples", True), ("Weight", True)],
            [[code(p["phase"]), str(p["samples"]), weight(p["weight_ns"])]
             for p in summary["lifecycle"]], filterable=False),
        table("mapping-table", "Process participants and their perf process",
              [("Participant", False), ("PID", True), ("Process", False),
               ("Mapping", False)],
              [[tag("span", p["participant"], class_="name"), str(p["pid"]),
                plain(p["process"], "none"), code(p["mapping"])]
               for p in summary["processes"]], filterable=False),
        _stream_facts(summary["stream"]),
        _activation_list(profile),
    ]
    return section("activations", "Activations", *content)


def _tasks_table(summary: dict, profile: ProfileSet) -> Html:
    tasks = {task: i for i, task in enumerate(profile_tasks(profile))}
    rows = [[tag("span", t["participant"], class_="name"),
             tag("a", t["task"], href=f"#task-detail-{tasks[t['participant'], t['task']]}"
                 if tasks[t["participant"], t["task"]] < TASK_LIMIT else "#task-drilldowns"), code(t["kind"]),
             str(t["activations"]),
             f"{t['completed']} / {t['failed']} / {t['interrupted']}",
             str(t["with_samples"]), str(t["without_samples"]),
             str(t["execution_context"]["samples"]),
             weight(t["execution_context"]["weight_ns"]),
             str(t["window_associated"]["samples"]),
             weight(t["window_associated"]["weight_ns"])]
            for t in summary["tasks"][:ACTIVATION_ROWS]]
    hidden = summary["tasks"][ACTIVATION_ROWS:]
    return join([table("tasks-table", "Tasks", [
        ("Participant", False), ("Task", False), ("Kind", False),
        ("Activations", True), ("Completed / failed / interrupted", True),
        ("With samples", True), ("Without samples", True),
        ("Execution context samples", True), ("Execution context weight", True),
        ("Window-associated samples", True),
        ("Window-associated weight", True)], rows),
        tag("p", f"{len(hidden)} other Tasks not listed; "
            f"{sum(t['activations'] for t in hidden)} Activations; " + "; ".join(
                f"{cls}: {sum(t[cls]['samples'] for t in hidden)} samples / "
                f"{sum(t[cls]['weight_ns'] for t in hidden)} ns"
                for cls in ("execution_context", "window_associated")), class_="note")])


def _stream_facts(stream: dict) -> Html:
    dropped = stream["dropped"]
    return tag("p", f"Observation stream: {stream['records']} records, "
                    f"dropped {'unknown' if dropped is None else dropped}, "
                    f"truncated {'yes' if stream['truncated'] else 'no'}, "
                    f"write failed {'yes' if stream['write_failed'] else 'no'}"
                    f", ended {'yes' if stream['terminated'] else 'no'}, "
                    f"limit {stream['limit_bytes']} bytes, "
                    f"{stream['undecodable_lines']} undecodable lines.",
               class_="note")


def _activation_list(profile: ProfileSet) -> Html:
    assert profile.correlation is not None
    activations = profile.correlation["activations"]
    origin = int(profile.report["capture"]["time_origin_ns"] or 0)
    shown = activations[:ACTIVATION_ROWS]
    rows = [[tag("span", a["participant"], class_="name"),
             tag("span", a["task"], class_="name"), str(a["index"]),
             (code(str(a["virtual_time_ns"])), str(a["virtual_time_ns"])),
             (code(f"[{int(a['begin_ns']) - origin}, "
                   f"{int(a['end_ns']) - origin})"),
              str(int(a["begin_ns"]) - origin)),
             code(a["outcome"]), str(a["samples"]),
             (weight(a["weight_ns"]), str(a["weight_ns"]))]
            for a in shown]
    note = (f"{len(activations) - len(shown)} later Activations are not "
            "listed here; the Tasks table counts all of them."
            if len(activations) > len(shown) else None)
    block = table("activation-table", "Activations in stream order "
                  "(Virtual time in ns; host interval in ns from the capture "
                  "start)", [
                      ("Participant", False), ("Task", False), ("Index", True),
                      ("Virtual time (ns)", True), ("Host interval", True),
                      ("Outcome", False), ("Samples", True),
                      ("Weight", True)], rows,
                  row_ids=[f"activation-{i}" for i in range(len(shown))],
                  row_classes=["no-samples" if a["samples"] == 0 else None
                               for a in shown])
    return join([tag("h3", "Activations"), tag("p", note, class_="note")
                 if note else None, block])


# --- processes and modules -------------------------------------------------


def _processes(capture: dict) -> Html:
    total = capture["totals"]["weight_ns"]
    processes = [[code(p["id"]), str(p["pid"]), plain(p["parent"], "none"),
                  code(p["role"]),
                  tag("span", " → ".join(p["commands"]), class_="name"),
                  str(p["execs"]), str(p["samples"]), weight(p["weight_ns"]),
                  share(p["weight_ns"], total), str(p["threads"])]
                 for p in capture["processes"]]
    threads = [[code(t["id"]), code(t["process"]), str(t["tid"]),
                tag("span", " → ".join(t["commands"]), class_="name"),
                str(t["samples"]), weight(t["weight_ns"]),
                share(t["weight_ns"], total)] for t in capture["threads"]]
    return section(
        "processes", "Processes and threads",
        table("process-table", "Observed processes", [
            ("Identity", False), ("PID", True), ("Parent", False),
            ("Role", False), ("Commands", False), ("Execs", True),
            ("Samples", True), ("Weight", True), ("Share", True),
            ("Threads", True)], processes),
        table("thread-table", "Observed threads", [
            ("Identity", False), ("Process", False), ("TID", True),
            ("Commands", False), ("Samples", True), ("Weight", True),
            ("Share", True)], threads),
        note="Every process that perf saw is listed, also one without "
             "samples: no sample is not evidence of no CPU use. A PID can be "
             "reused, so identities p1, p2, ... tell lifetimes apart.")


def _modules(capture: dict) -> Html:
    rows = [[tag("code", m["path"], class_="path"),
             plain(m["build_id"], "none"), code(m["material"]),
             join([code(m["debug"]["status"]),
                   join([" ", tag("code", m["debug"]["path"])])
                   if m["debug"]["path"] else None]),
             str(m["samples_leaf"]), str(m["samples_unknown_symbol"])]
            for m in capture["modules"]]
    return section(
        "modules", "Modules",
        table("module-table", "Loaded modules", [
            ("Path", False), ("Build ID", False), ("Material", False),
            ("Debug information", False), ("Leaf samples", True),
            ("Without function name", True)], rows),
        note="A source line is shown only from a module whose debug "
             "information matches its build ID. A module without a copy "
             "keeps its path and build ID, and its samples have no function "
             "name.")
