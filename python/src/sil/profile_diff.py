"""Compare two portable profile sets offline; no capture or significance test."""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path

from sil.diagnostics_html import page
from sil.diagnostics_html_markup import code, join, section, table, tag
from sil.diagnostics_report import new_path_problem, write_new_file
from sil.diagnostics_run import GivenOnce, command_error
from sil.profile_analysis import cpu_ranking, cpu_rows, function_paths
from sil.profile_set import ProfileSet, ProfileSetError, load_profile_set

PROG = "sil profile diff"
ROW_LIMIT = 100
TASK_LIMIT = 50
NOISE = ("One Run per set supplies no significance verdict; sampling noise "
         "can explain small changes. Sample weights are estimates, never "
         "function durations or call counts.")


def identities(profile: ProfileSet) -> dict:
    report = profile.report
    capture = report["capture"]
    return {"Manifest hash": report["identity"]["manifest_hash"],
            "SiL version": report["identity"]["sil_version"],
            "Source revision": report["identity"]["source_revision"],
            "Host": report["host"],
            "Tool version": capture["backend"]["version"],
            "Sampling settings": capture.get("sampling"),
            "Call graph": capture.get("call_graph"),
            "Scope": capture["scope"]}


def _name(function: tuple) -> str:
    module, symbol = function
    return f"{symbol or '[unknown function]'} ({module or '[unknown module]'})"


def _share(weight: int, total: int) -> str:
    return f"{100 * weight / total:.2f}%" if total else "unavailable (zero weight)"


def _cpu_comparison(base: ProfileSet, new: ProfileSet, base_rows: list,
                    new_rows: list) -> tuple[list, str]:
    ranked = []
    for profile, rows in ((base, base_rows), (new, new_rows)):
        functions, paths = function_paths(profile)
        rank = cpu_ranking(paths, rows)
        ranked.append((rank, {functions[k]: v[1] for k, v in rank.self.items()},
                       {functions[k]: v[1] for k, v in rank.inclusive.items()}))
    (b, bs, bi), (n, ns, ni) = ranked
    keys = bs.keys() | bi.keys() | ns.keys() | ni.keys()
    def change(key, before, after):
        return 100 * (after.get(key, 0) / n.weight -
                      before.get(key, 0) / b.weight) if b.weight and n.weight else 0
    keys = sorted(keys, key=lambda k: (-max(abs(change(k, bs, ns)),
                                           abs(change(k, bi, ni))), _name(k)))
    rows = []
    for key in keys[:ROW_LIMIT]:
        values = [_name(key)]
        for before, after in ((bs, ns), (bi, ni)):
            values += [_share(before.get(key, 0), b.weight),
                       _share(after.get(key, 0), n.weight),
                       f"{change(key, before, after):+.2f} pp"
                       if b.weight and n.weight else "unavailable"]
        rows.append(values)
    hidden = keys[ROW_LIMIT:]
    note = (f"Denominators: BASE {b.samples} samples / {b.weight} ns; "
            f"NEW {n.samples} samples / {n.weight} ns. "
            f"{len(hidden)} other functions not listed; self remainder: "
            f"BASE {sum(bs.get(k, 0) for k in hidden)} ns, "
            f"NEW {sum(ns.get(k, 0) for k in hidden)} ns. "
            "Inclusive shares overlap and do not add up.")
    return rows, note


CPU_HEADERS = [("Function and module", False), ("Self share BASE", True),
               ("Self share NEW", True), ("Self change (pp)", True),
               ("Inclusive share BASE", True), ("Inclusive share NEW", True),
               ("Inclusive change (pp)", True)]


def _heap_sites(profile: ProfileSet) -> dict:
    assert profile.allocations is not None
    functions, paths = function_paths(profile)
    sites: dict = {}
    for stack, size, allocations, temporary, outstanding in profile.allocations["allocations"]:
        key = functions[paths[stack][-1]]
        values = sites.setdefault(key, [0, 0, 0, 0, 0])
        for index, value in enumerate((allocations, size * allocations,
                                       temporary, outstanding, size * outstanding)):
            values[index] += value
    return sites


HEAP_METRICS = ("Allocations", "Allocated bytes", "Temporary allocations",
                "Outstanding allocations", "Outstanding bytes")


def compare(base: ProfileSet, new: ProfileSet) -> tuple[str, str]:
    kind = base.report["capture"]["kind"]
    if new.report["capture"]["kind"] != kind:
        raise ProfileSetError("collectors mismatch: CPU and heap cannot be compared")
    for profile in (base, new):
        if (profile.samples if kind == "cpu" else profile.allocations) is None:
            raise ProfileSetError("comparison needs normalized evidence in both sets")
    title = f"{kind.upper() if kind == 'cpu' else 'Heap'} profile diff"
    b_id, n_id = identities(base), identities(new)
    changed = [key for key in b_id if b_id[key] != n_id[key]]
    warning = ("WARNING: identities differ: " + ", ".join(changed)
               if changed else "Identities match (unknown values remain unknown).")
    text = [title, warning]
    identity_rows = [[key, json.dumps(value, sort_keys=True),
                      json.dumps(n_id[key], sort_keys=True)] for key, value in b_id.items()]
    text += [f"{key}: BASE {before}; NEW {after}"
             for key, before, after in identity_rows]
    for label, profile in (("BASE", base), ("NEW", new)):
        report = profile.report
        text.append(f"{label}: Run {report['run']['status']}; capture "
                    f"{report['capture']['status']} "
                    f"{report['capture']['reasons']}")
    content = [tag("h1", title), tag("p", warning, class_="callout tone-warn"
                                   if changed else "note"),
               tag("p", NOISE if kind == "cpu" else
                   "One Run per set supplies no significance verdict. Outstanding "
                   "allocations at capture end are not a leak verdict."),
               tag("p", "\n".join(text[-2:])),
               table("identities", "Both artifact identities", [
                   ("Identity", False), ("BASE", False), ("NEW", False)], identity_rows)]
    if kind == "cpu":
        br, nr = cpu_rows(base), cpu_rows(new)
        rows, note = _cpu_comparison(base, new, br, nr)
        content.append(section("functions", "CPU shares", tag("p", note),
                               table("diff-functions", "Functions", CPU_HEADERS, rows)))
        text += [NOISE, note, "Self share BASE → NEW; Inclusive share BASE → NEW"]
        text += [" | ".join(row) for row in rows]
        complete = all(p.correlation is not None and
                       p.report["activation_correlation"]["status"] == "complete"
                       for p in (base, new))
        if complete:
            assert base.correlation is not None and new.correlation is not None
            tasks = sorted({(a["participant"], a["task"])
                            for c in (base.correlation, new.correlation) for a in c["activations"]})
            for index, task in enumerate(tasks[:TASK_LIMIT]):
                for cls in ("execution_context", "window_associated"):
                    groups = []
                    for p, evidence in ((base, br), (new, nr)):
                        assert p.correlation is not None
                        refs = {i for i, a in enumerate(p.correlation["activations"])
                                if (a["participant"], a["task"]) == task}
                        groups.append([r for r in evidence if r[2] == cls and r[3] in refs])
                    rows, note = _cpu_comparison(base, new, *groups)
                    label = f"{task[0]} / {task[1]} · {cls}"
                    content.append(section(f"task-{index}-{cls}", label,
                                           tag("p", note), table(
                                               f"diff-task-{index}-{cls}", label,
                                               CPU_HEADERS, rows)))
                    text += [label, note, *(" | ".join(r) for r in rows)]
            remaining = tasks[TASK_LIMIT:]
            note = f"{len(remaining)} other Tasks not listed; global shares include their samples."
        else:
            note = "Per-Task diff unavailable: both sets need complete Activation correlation."
        content.append(tag("p", note))
        text.append(note)
    else:
        bs, ns = _heap_sites(base), _heap_sites(new)
        keys = sorted(bs.keys() | ns.keys(), key=lambda k: (
            -abs(ns.get(k, [0]*5)[1] - bs.get(k, [0]*5)[1]), _name(k)))
        headers = [("Site and module", False)] + [
            (f"{metric} {suffix}", True) for metric in HEAP_METRICS
            for suffix in ("BASE", "NEW", "change")]
        rows = []
        for key in keys[:ROW_LIMIT]:
            b, n = bs.get(key, [0]*5), ns.get(key, [0]*5)
            rows.append([_name(key), *(str(v) for before, after in zip(b, n)
                                       for v in (before, after, after - before))])
        footer = []
        for label, selected in (("Other sites (not listed)", keys[ROW_LIMIT:]),
                                ("All sites", keys)):
            b = [sum(bs.get(k, [0]*5)[i] for k in selected) for i in range(5)]
            n = [sum(ns.get(k, [0]*5)[i] for k in selected) for i in range(5)]
            footer.append([label, *(str(v) for before, after in zip(b, n)
                                    for v in (before, after, after - before))])
        content.append(table("heap-diff", "Heap site changes", headers, rows, footer=footer))
        text += [" | ".join(h[0] for h in headers),
                 *(" | ".join(r) for r in rows + footer)]
    body = join(content)
    html = page(title, body, json.dumps({"base": base.report, "new": new.report}),
                styles=("report.css", "profile.css"), scripts=("report.js",),
                download_name="profile-diff-inputs.json")
    # No artifact name may control the terminal.
    terminal = "\n".join("".join(f"\\x{ord(c):02x}" if
                                  unicodedata.category(c) == "Cc" else c
                                  for c in line) for line in text) + "\n"
    return terminal, html


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=PROG, allow_abbrev=False,
                                     description=__doc__)
    parser.add_argument("base", type=Path)
    parser.add_argument("new", type=Path)
    parser.add_argument("--html", type=Path, action=GivenOnce)
    args = parser.parse_args(argv)
    if args.html is not None:
        problem = new_path_problem("HTML report", args.html)
        if problem:
            return command_error(PROG, problem, 2)
    try:
        terminal, html = compare(load_profile_set(args.base), load_profile_set(args.new))
    except ProfileSetError as error:
        return command_error(PROG, str(error), 2)
    sys.stdout.write(terminal)
    if args.html is not None:
        try:
            write_new_file(args.html, html)
        except OSError as error:
            return command_error(PROG, f"cannot write HTML report: {error}", 1)
        print(f"HTML report: {args.html}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
