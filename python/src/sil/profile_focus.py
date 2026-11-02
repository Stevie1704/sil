"""Bounded bottom-up views of retained stacks, without new capture evidence."""

from __future__ import annotations

from sil.diagnostics_html_markup import Html, code, join, section, tag, void
from sil.profile_analysis import cpu_rows
from sil.profile_html_parts import (TreeLimits, build_tree, function_cell,
                                    render_tree, share)
from sil.profile_set import ProfileSet

METRICS = ("allocations", "allocated_bytes", "temporary_allocations",
           "outstanding_allocations", "outstanding_bytes")
FUNCTION_OPTIONS = 50
INTERACTIVE_ROWS = 200_000
INTERACTIVE_PATH_KEYS = 1_000_000
INTERACTIVE_NAME_CHARS = 8 * 1024 * 1024
END = "[stack ends here; caller unavailable or truncated]"
DEEP = "[deeper callers omitted]"
RULE = ("Inclusive selection counts each sample/allocation row once, anchored "
        "at the leafmost occurrence. Recursive ancestors remain explicit; "
        "their caller relationship is ambiguous in recursion. Leaf-site mode "
        "selects only the captured leaf. These are sampled/retained stack "
        "relationships, never exact function call counts.")


def focus_data(profile: ProfileSet) -> dict | None:
    capture = profile.report["capture"]
    cpu = capture["kind"] == "cpu"
    document = profile.samples if cpu else profile.allocations
    if document is None:
        return None
    modules = capture["modules"] if cpu else capture["analysis"]["modules"]
    module_paths = {m["id"]: m["path"] for m in modules}
    keys: dict[tuple, int] = {}
    frame_keys = [keys.setdefault((f["module"], f["symbol"]), len(keys))
                  for f in document["frames"]]
    unknown = keys.setdefault((None, None), len(keys))
    paths = [[frame_keys[f] for f in reversed(stack)] or [unknown]
             for stack in document["stacks"]]
    rows = cpu_rows(profile) if cpu else [
        [stack, "", "", "", count, size * count, temporary, outstanding,
         size * outstanding]
        for stack, size, count, temporary, outstanding in document["allocations"]]
    names = [[symbol, module_paths.get(module)] for module, symbol in keys]
    interactive = (len(rows) <= INTERACTIVE_ROWS
                   and sum(map(len, paths)) <= INTERACTIVE_PATH_KEYS
                   and len(names) <= INTERACTIVE_ROWS
                   and sum(len(part or "") for fn in names for part in fn)
                   <= INTERACTIVE_NAME_CHARS)
    return {"kind": "cpu" if cpu else "heap", "paths": paths,
            "functions": names,
            "rows": rows, "interactive": interactive,
            "metrics": ["sampled_weight_ns"] if cpu else list(METRICS),
            "limits": {"max_depth": TreeLimits().max_depth,
                       "max_nodes": TreeLimits().max_nodes,
                       "max_children": TreeLimits().max_children,
                       "min_share": TreeLimits().min_share,
                       "options": FUNCTION_OPTIONS}}



def interactive_focus_data(profile: ProfileSet) -> dict | None:
    """Never embed an over-budget normalized table in the offline page."""
    data = focus_data(profile)
    return data if data is not None and data["interactive"] else None


def _metric_value(data: dict, row: list, metric: str) -> int:
    if data["kind"] == "cpu":
        return row[5]
    return row[(4, 5, 6, 7, 8)[METRICS.index(metric)]]


def caller_tree(data: dict, selected: int, metric: str,
                leaf_only: bool = False):
    """Each row contributes once; reverse callers from its leafmost anchor."""
    end, deep = len(data["functions"]), len(data["functions"]) + 1
    limit = TreeLimits().max_depth
    paths = []
    for row in data["rows"]:
        path = data["paths"][row[0]]
        if selected not in path or (leaf_only and path[-1] != selected):
            continue
        anchor = len(path) - 1 - path[::-1].index(selected)
        callers = list(reversed(path[:anchor]))
        callers = callers[:limit] + [deep] if len(callers) > limit else callers + [end]
        paths.append((callers, row[4], _metric_value(data, row, metric)))
    return build_tree(paths)


def _default(data: dict, metric: str) -> int:
    totals: dict[int, int] = {}
    own: dict[int, int] = {}
    for row in data["rows"]:
        leaf = data["paths"][row[0]][-1]
        own[leaf] = own.get(leaf, 0) + _metric_value(data, row, metric)
        for key in set(data["paths"][row[0]]):
            totals[key] = totals.get(key, 0) + _metric_value(data, row, metric)
    return min(totals, key=lambda key: (-totals[key], -own.get(key, 0), key)) if totals else 0


def callers_section(profile: ProfileSet) -> Html:
    data = focus_data(profile)
    if data is None:
        return section("callers", "Bottom-up callers", tag(
            "p", "Unavailable: no retained normalized stacks."))
    metric = "sampled_weight_ns" if data["kind"] == "cpu" else "allocated_bytes"
    selected = _default(data, metric)
    tree = caller_tree(data, selected, metric)
    denominator = sum(_metric_value(data, row, metric) for row in data["rows"])
    fn = data["functions"][selected]

    def label(key: int) -> Html:
        if key >= len(data["functions"]):
            return tag("span", END if key == len(data["functions"]) else DEEP,
                       class_="na")
        symbol, module = data["functions"][key]
        return join([function_cell(symbol, module),
                     " [recursive ancestor; ambiguous caller relationship]"
                     if key == selected else ""])

    summary = (f"Selected function: {fn[0] or '[unknown function]'}. "
               f"{tree.count} {'samples' if data['kind'] == 'cpu' else 'allocations'} / "
               f"{tree.weight} {'ns' if data['kind'] == 'cpu' else metric}. "
               f"{share(tree.weight, denominator)} of the enclosing selection "
               f"({denominator} {'ns' if data['kind'] == 'cpu' else metric}).")
    controls = tag("form", tag("fieldset", tag("legend", "Choose captured function"),
        tag("label", "Search symbol/module", void("input", type="search", id="caller-search")),
        tag("label", "Function", tag("select", id="caller-function")),
        tag("button", "Previous functions", type="button", id="caller-prev"),
        tag("button", "Next functions", type="button", id="caller-next"),
        tag("span", id="caller-page", aria_live="polite"),
        tag("label", "Selection", tag("select",
            tag("option", "Inclusive selected function", value="inclusive"),
            tag("option", "Leaf allocation/sample site", value="leaf"), id="caller-mode")),
        tag("label", "Metric", tag("select", *(
            tag("option", m.replace("_", " "), value=m, selected=m == metric)
            for m in data["metrics"]), id="caller-metric")), class_="filters"),
        id="caller-controls", hidden=True)
    return section("callers", "Bottom-up callers", controls,
        tag("p", summary, id="caller-summary", aria_live="polite"),
        tag("p", RULE, class_="note"),
        tag("p", "Heap has no Activation/thread/time attribution; metrics "
            "describe the captured process lifetime. Outstanding values are "
            "a capture-end snapshot, never a leak verdict.", class_="note")
        if data["kind"] == "heap" else None,
        tag("div", render_tree(tree, label, lambda v: f"{v} " +
                ("ns" if data["kind"] == "cpu" else metric),
                "samples" if data["kind"] == "cpu" else "allocations"),
            id="caller-tree", class_="tree-wrap", tabindex="0", role="region",
            aria_label="Bottom-up callers"),
        note=f"At most {TreeLimits().max_nodes} nodes, "
             f"{TreeLimits().max_children} siblings and {TreeLimits().max_depth} "
             "levels are shown, with explicit remainders. Stack endings do not "
             "prove a complete call path. Interactive selection requires at "
             f"most {INTERACTIVE_ROWS} normalized rows, {INTERACTIVE_PATH_KEYS} "
             f"path keys and {INTERACTIVE_NAME_CHARS} name characters; "
             "otherwise the bounded "
             "static summary remains available.")
