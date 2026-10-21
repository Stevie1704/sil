"""Bounded offline CPU flame, Activation timeline and Task detail views."""

from __future__ import annotations

from collections import defaultdict
from typing import Callable

from sil.diagnostics_html_markup import Html, code, join, section, table, tag, void
from sil.profile_analysis import cpu_ranking, cpu_rows, function_paths
from sil.profile_html_parts import TreeLimits, build_tree, render_tree, share
from sil.profile_set import ProfileSet

INTERVAL_LIMIT = 500
TASK_LIMIT = 50
ROW_LIMIT = 50
FLAME_LIMITS = TreeLimits()


def flame_graph(root, label: Callable[[int], str], id_: str) -> Html:
    """An icicle: each level partitions parent weight, with visible remainders."""
    if root.weight == 0:
        return tag("p", "No sampled weight to draw. No sample is not evidence "
                        "of no CPU use.", class_="note")
    shapes = []
    budget, deepest = FLAME_LIMITS.max_nodes, 0

    def box(name, count, weight, x, depth, path=None):
        nonlocal deepest
        deepest = max(deepest, depth)
        width = 1000 * weight / root.weight
        title = f"{name}: {count} samples, {weight} ns ({share(weight, root.weight)})"
        characters = max(0, int(width / 7) - 2)
        text = name if len(name) <= characters else name[:max(0, characters - 1)] + "…"
        clip = f"{id_}-clip-{len(shapes)}"
        shapes.append(tag(
            "g", tag("defs", tag("clipPath", tag("rect", x=str(x+2), y=str(depth*28),
                width=str(max(0, width-4)), height="26"), id=clip)), tag("title", title),
            tag("rect", x=f"{x:.4f}", y=str(depth * 28),
                width=f"{width:.4f}", height="26", class_="flame-rect"),
            tag("text", text if characters >= 2 else "", x=f"{x + 3:.4f}",
                y=str(depth * 28 + 18), class_="flame-label", clip_path=f"url(#{clip})"),
            tabindex="0", role="button" if path is not None else "img",
            aria_label=title, data_flame_path=path,
            data_name=name, data_weight=str(weight), class_="flame-frame"))

    def walk(node, x, depth, path):
        nonlocal budget
        ordered = sorted(node.children.items(), key=lambda e: (-e[1].weight, e[0]))
        shown, hidden = [], []
        for key, child in ordered:
            if (budget > 0 and depth < FLAME_LIMITS.max_depth and
                    len(shown) < FLAME_LIMITS.max_children and
                    child.weight >= max(1, root.weight * FLAME_LIMITS.min_share)):
                budget -= 1
                shown.append((key, child))
            else:
                hidden.append(child)
        for key, child in shown:
            child_path = f"{path},{key}" if path else str(key)
            box(label(key), child.count, child.weight, x, depth + 1, child_path)
            walk(child, x, depth + 1, child_path)
            x += 1000 * child.weight / root.weight
        if hidden:
            value = sum(c.weight for c in hidden)
            box(f"{len(hidden)} other / deeper callees not shown", sum(c.count for c in hidden),
                value, x, depth + 1)
            x += 1000 * value / root.weight
        if node.self_count:
            box("[self / stack ends here; possibly cut off]", node.self_count, node.self_weight,
                x, depth + 1)
    box("All samples", root.count, root.weight, 0, 0, "")
    walk(root, 0, 0, "")
    return tag("svg", *shapes, id=id_, viewBox=f"0 0 1000 {(deepest + 1)*28}",
               class_="flame-graph", role="group", aria_label="Flame graph: width is sample weight")


def flame_section(root, label: Callable[[int], str]) -> Html:
    return section(
        "flame", "Flame graph",
        tag("p", f"Total: {root.count} samples / {root.weight} ns sampled weight. "
            "Widths use this denominator. Inline frames keep one weight per sample; "
            "stack ends are the retained caller path, which may be cut off.", id="flame-total"),
        tag("div", tag("label", "Search functions ", void("input", id="flame-search",
                    type="search")), tag("button", "Reset zoom and search", id="flame-reset",
                    type="button"), tag("p", id="flame-search-status", aria_live="polite"),
            id="flame-controls", hidden=True),
        tag("p", id="flame-focus", class_="frame-detail", aria_live="polite"),
        tag("div", flame_graph(root, label, "cpu-flame-svg"), id="cpu-flame"),
        note="Top-down icicle; Tab focuses a frame and Enter zooms. Full names "
             "are available on hover or focus. Unknown frames and omitted "
             "callees retain their sample weight. The caller tree and hotspot "
             "tables provide the equivalent data without JavaScript.")


def _function_name(function: tuple) -> str:
    module, symbol = function
    return f"{symbol or '[unknown function]'} ({module or '[unknown module]'})"


def _rank_table(id_, title, values, functions, count, weight, own):
    ordered = sorted(values.items(), key=lambda e: (-e[1][1], e[0]))
    shown = ordered[:ROW_LIMIT]
    footer = []
    if own:
        footer = [["Other leaf frames (not listed)",
                   str(count - sum(v[0] for _, v in shown)),
                   str(weight - sum(v[1] for _, v in shown)), ""],
                  ["All selected samples", str(count), str(weight), share(weight, weight)]]
    return join([table(id_, title, [("Function and module", False), ("Samples", True),
                                  ("Weight (ns)", True), ("Share", True)],
                       [[_function_name(functions[k]), str(v[0]), str(v[1]), share(v[1], weight)]
                        for k, v in shown], footer=footer),
                 tag("p", f"{max(0, len(ordered) - len(shown))} other functions not listed; "
                     "inclusive shares overlap and do not add up.") if not own else None])


def task_drilldowns(profile: ProfileSet) -> Html:
    if profile.correlation is None:
        return Html("")
    activations = profile.correlation["activations"]
    functions, paths = function_paths(profile)
    groups: dict = defaultdict(list)
    for index, a in enumerate(activations):
        groups[a["participant"], a["task"]].append(index)
    rows = cpu_rows(profile)
    grouped_rows: dict = defaultdict(list)
    for row in rows:
        if row[2] in ("execution_context", "window_associated"):
            a = activations[row[3]]
            grouped_rows[(a["participant"], a["task"]), row[2]].append(row)
    blocks = []
    tasks = sorted(groups)
    for task_index, task in enumerate(tasks[:TASK_LIMIT]):
        refs = groups[task]
        zero = [activations[i] for i in refs if activations[i]["samples"] == 0]
        body = [tag("p", f"{len(refs)} Activations; {len(zero)} zero-sample Activations. "
                    "No sample is not evidence of no CPU use."),
                table(f"task-{task_index}-zero", "Zero-sample Activations", [
                    ("Index", True), ("Virtual time (ns)", True), ("Outcome", False)],
                    [[str(a["index"]), code(str(a["virtual_time_ns"])), a["outcome"]]
                     for a in zero[:INTERVAL_LIMIT]]),
                tag("p", f"{max(0, len(zero) - INTERVAL_LIMIT)} other zero-sample Activations not listed.")]
        for cls in ("execution_context", "window_associated"):
            selected = grouped_rows[task, cls]
            rank = cpu_ranking(paths, selected)
            id_ = f"task-{task_index}-{cls}"
            tree = build_tree((paths[r[0]], r[4], r[5]) for r in selected)
            body.append(tag("details", tag("summary", f"{cls}: {rank.samples} samples / "
                                           f"{rank.weight} ns sampled weight"),
                            tag("a", "Explore this class in the flame graph", href="#flame",
                                data_task_filter=str(task_index), data_class_filter=cls),
                            _rank_table(id_ + "-self", "Self hotspots", rank.self, functions,
                                        rank.samples, rank.weight, True),
                            _rank_table(id_ + "-inclusive", "Inclusive hotspots", rank.inclusive,
                                        functions, rank.samples, rank.weight, False),
                            tag("div", render_tree(tree, lambda k: code(_function_name(functions[k])),
                                lambda v: f"{v} ns", "samples",
                                TreeLimits(max_nodes=100, max_depth=32)), class_="tree-wrap")))
        blocks.append(tag("details", tag("summary", f"{task[0]} / {task[1]}"),
                          *body, id=f"task-detail-{task_index}"))
    hidden = set(tasks[TASK_LIMIT:])
    remainder = [r for r in rows if r[2] in ("execution_context", "window_associated")
                 and (activations[r[3]]["participant"], activations[r[3]]["task"]) in hidden]
    return section("task-drilldowns", "Per-Task drill-down", *blocks,
                   tag("p", f"{len(hidden)} other Tasks not expanded; "
                       f"{sum(len(groups[t]) for t in hidden)} Activations. " + "; ".join(
                           f"{cls}: {sum(r[4] for r in remainder if r[2] == cls)} samples / "
                           f"{sum(r[5] for r in remainder if r[2] == cls)} ns"
                           for cls in ("execution_context", "window_associated"))),
                   note="Classes are separate evidence, never one Task cost. For a partial "
                        "correlation these are only the retained Activations.")


def timeline_intervals(profile: ProfileSet) -> list[dict]:
    """References remain the original correlation indices, even after sorting."""
    correlation = profile.correlation
    assert correlation is not None
    result = []
    for kind, name in (("activation", "activations"), ("replay", "replays"), ("lifecycle", "phases")):
        for index, row in enumerate(correlation.get(name, [])):
            result.append({**row, "interval_kind": kind, "ref": index,
                           "lane": (f"{row['participant']} / {row['task']}" if kind == "activation"
                                    else f"{row['participant']} / Replay" if kind == "replay"
                                    else f"Lifecycle / {row['phase']}")})
    return result


def timeline(profile: ProfileSet) -> Html:
    if profile.correlation is None:
        return section("timeline", "Activation timeline", tag("p",
                       "Timeline unavailable: no validated Activation correlation. "
                       "No empty timeline is drawn."))
    intervals = timeline_intervals(profile)
    assert profile.samples is not None
    samples = profile.samples["samples"]
    classes = profile.correlation["samples"]
    origin = int(profile.report["capture"]["time_origin_ns"] or 0)
    points: dict = defaultdict(list)
    weights: dict = defaultdict(int)
    incomplete = []
    for sample, (cls, ref) in zip(samples, classes):
        if cls == "unattributed" and ref == "observations_incomplete":
            incomplete.append(origin + sample[0])
        else:
            points[cls, ref].append(origin + sample[0])
            weights[cls, ref] += sample[3]
    begin = min([int(a["begin_ns"]) for a in intervals] + incomplete, default=origin)
    end = max([int(a["end_ns"]) for a in intervals] + [t + 1 for t in incomplete], default=begin + 1)
    span = max(1, end - begin)
    shown = intervals[:INTERVAL_LIMIT]
    lanes = {name: index for index, name in enumerate(sorted({a["lane"] for a in shown}))}
    shapes = []
    tasks = sorted({(a["participant"], a["task"]) for a in profile.correlation["activations"]})
    task_ids = {t: i for i, t in enumerate(tasks)}
    for name, lane in lanes.items():
        shapes.append(tag("text", name[:40], tag("title", name), x="0", y=str(lane*38 + 18), class_="timeline-label"))
    rows = []
    for a in shown:
        kind, ref = a["interval_kind"], a["ref"]
        cls = "execution_context" if kind == "activation" else kind
        times = points[cls, ref if kind != "lifecycle" else a["phase"]]
        if kind == "activation":
            times = times + points["window_associated", ref]
        count = len(times)
        interval_weight = weights[cls, ref if kind != "lifecycle" else a["phase"]]
        class_text = f"{cls}: {len(times)} samples / {interval_weight} ns"
        if kind == "activation":
            own_count = len(points["execution_context", ref])
            window_count = len(points["window_associated", ref])
            interval_weight += weights["window_associated", ref]
            class_text = (f"execution_context: {own_count} samples / {weights['execution_context', ref]} ns; "
                          f"window_associated: {window_count} samples / {weights['window_associated', ref]} ns")
        b, e = int(a["begin_ns"]), int(a["end_ns"])
        x = 260 + 740 * (b - begin) / span
        w = 740 * (e - b) / span
        y = lanes[a["lane"]] * 38
        virtual = str(a.get("virtual_time_ns", "unavailable (lifecycle)"))
        title = (f"{a['lane']}: host [{b}, {e}) ns; Virtual time {virtual} ns; "
                 f"{count} samples / {interval_weight} ns; density {count}/{e-b} host ns. "
                 f"{class_text}. "
                 + ("No sample is not evidence of no CPU use." if not count else ""))
        bins = [0] * 16
        for t in times:
            bins[min(15, max(0, (t - b) * 16 // max(1, e - b)))] += 1
        marks = [tag("rect", tag("title", f"{value} samples in host subinterval {i+1}/16"),
                     x=str(x + w*i/16), y=str(y+28 - 16*value/max(bins)),
                     width=str(w/16), height=str(16*value/max(bins)), class_="density-mark")
                 for i, value in enumerate(bins) if value]
        shapes.append(tag("g", tag("title", title),
            tag("rect", x=f"{x:.4f}", y=str(y), width=f"{max(0.5, w):.4f}",
                height="28", class_="interval-rect" + (" no-samples" if not count else "")),
            *marks,
            tabindex="0", role="button", aria_label=title,
            data_interval_kind=kind, data_interval_ref=str(ref),
            data_interval_phase=a.get("phase"), data_name=a["lane"],
            class_="timeline-interval"))
        link = (tag("a", "Open Task", href=f"#task-detail-{task_ids[a['participant'], a['task']]}"
                    if task_ids[a["participant"], a["task"]] < TASK_LIMIT else "#tasks-table")
                if kind == "activation" else "")
        rows.append([a["lane"], code(f"[{b}, {e})"), code(virtual), str(count),
                     str(interval_weight), class_text, f"{count}/{e-b} host ns", link])
    warnings = []
    if profile.report["activation_correlation"]["status"] == "partial":
        warnings.append("Observation stream cut off or incomplete: gaps may contain unobserved "
                        "Activations; blank space is unknown, not idle CPU time. "
                        "The exact gap boundaries are not retained.")
    if incomplete:
        b, e = min(incomplete), max(incomplete) + 1
        warnings.append(f"observations_incomplete sample extent: [{b}, {e}) host ns; "
                        f"{len(incomplete)} samples (extent is not a proven gap boundary).")
        shapes.append(tag("rect", tag("title", warnings[-1]),
                          x=str(260 + 740*(b-begin)/span), y="0",
                          width=str(max(0.5, 740*(e-b)/span)),
                          height=str(max(1,len(lanes))*38), class_="incomplete-region"))
    axis_y = max(1, len(lanes)) * 38
    shapes.append(tag("line", x1="260", x2="1000", y1=str(axis_y), y2=str(axis_y), class_="axis"))
    for i in range(5):
        x = 260 + 740 * i / 4
        shapes += [tag("line", x1=str(x), x2=str(x), y1=str(axis_y), y2=str(axis_y+5), class_="axis"),
                   tag("text", f"+{span*i//4} ns", x=str(x), y=str(axis_y+20),
                       class_="timeline-label", text_anchor="start" if i == 0 else "end" if i == 4 else "middle")]
    hidden = intervals[INTERVAL_LIMIT:]
    return section("timeline", "Activation timeline",
                   tag("p", f"Host CLOCK_MONOTONIC axis: [{begin}, {end}) ns. "
                       "Intervals are half-open. Geometry uses exact integer differences "
                       "before scaling; full host and Virtual time values stay decimal strings."),
                   *(tag("p", text, class_="callout tone-warn") for text in warnings),
                   tag("svg", *shapes, viewBox=f"0 0 1000 {axis_y+30}",
                       class_="activation-timeline", id="timeline-svg", role="group",
                       aria_label="Activation timeline by Participant and Task"),
                   tag("p", "Select an interval to filter its samples. The class filter keeps "
                       "execution_context and window_associated distinguishable. Use Clear "
                       "filters to return. No sample is not evidence of no CPU use."),
                   tag("p", id="timeline-selection", class_="selection", aria_live="polite"),
                   table("timeline-table", "Timeline intervals (exact timestamps)", [
                       ("Lane", False), ("Host interval (ns)", False), ("Virtual time (ns)", True),
                       ("Samples", True), ("Weight (ns)", True), ("Classes", False),
                       ("Sample density", False), ("Task detail", False)], rows),
                   tag("p", f"{len(samples)} samples / "
                       f"{sum(s[3] for s in samples)} ns in the capture (all classes). "
                       f"{len(hidden)} other intervals not drawn; "
                       f"{sum(a.get('samples',0) for a in hidden)} samples in their counters. "
                       "All samples remain in the global tables and flame graph; "
                       "lifecycle and unattributed classes remain in the class table."))
