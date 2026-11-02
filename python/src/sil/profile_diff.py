"""Compare two portable profile sets offline; no capture or significance test."""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path

from sil.diagnostics_format import provenance_mode
from sil.diagnostics_html import page
from sil.diagnostics_html_markup import code, join, section, table, tag
from sil.diagnostics_report import new_path_problem, write_new_file
from sil.diagnostics_run import GivenOnce, command_error
from sil.profile_analysis import cpu_ranking, cpu_rows, function_paths, profile_tasks
from sil.profile_benchmark import Benchmark, benchmark_context, load_benchmark
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
            "Scope": capture["scope"],
            "Provenance mode": provenance_mode(report["configuration"])}


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
    (base_rank, base_self, base_inclusive), (new_rank, new_self, new_inclusive) = ranked
    keys = base_self.keys() | base_inclusive.keys() | new_self.keys() | new_inclusive.keys()
    def change(key, before, after):
        return 100 * (after.get(key, 0) / new_rank.weight -
                      before.get(key, 0) / base_rank.weight) if base_rank.weight and new_rank.weight else 0
    def share_change(k):
        return max(abs(change(k, base_self, new_self)),
                   abs(change(k, base_inclusive, new_inclusive)))
    def weight_change(k):
        return max(abs(new_self.get(k, 0) - base_self.get(k, 0)),
                   abs(new_inclusive.get(k, 0) - base_inclusive.get(k, 0)))
    # Unchanged shares can hide large absolute growth, so both rankings
    # contribute rows.
    by_share = sorted(keys, key=lambda k: (-share_change(k), _name(k)))
    by_weight = sorted(keys, key=lambda k: (-weight_change(k), _name(k)))
    listed = dict.fromkeys(by_share[:ROW_LIMIT // 2] + by_weight[:ROW_LIMIT // 2])
    for key in by_share:
        if len(listed) >= ROW_LIMIT:
            break
        listed.setdefault(key)
    shown = sorted(listed, key=lambda k: (-share_change(k), -weight_change(k), _name(k)))
    hidden = [k for k in by_share if k not in listed]
    rows = []
    for key in shown:
        values = [_name(key)]
        for before, after in ((base_self, new_self), (base_inclusive, new_inclusive)):
            values += [_share(before.get(key, 0), base_rank.weight),
                       _share(after.get(key, 0), new_rank.weight),
                       f"{change(key, before, after):+.2f} pp"
                       if base_rank.weight and new_rank.weight else "unavailable"]
            values += [str(before.get(key, 0)), str(after.get(key, 0)),
                       f"{after.get(key, 0) - before.get(key, 0):+d}"]
        rows.append(values)
    note = (f"Denominators: BASE {base_rank.samples} samples / {base_rank.weight} ns; "
            f"NEW {new_rank.samples} samples / {new_rank.weight} ns. "
            f"Listed: the largest share changes and the largest absolute estimated weight changes. "
            f"{len(hidden)} other functions not listed; self remainder: "
            f"BASE {sum(base_self.get(k, 0) for k in hidden)} ns, "
            f"NEW {sum(new_self.get(k, 0) for k in hidden)} ns. "
            "Inclusive shares overlap and do not add up.")
    return rows, note


CPU_HEADERS = [("Function and module", False)] + [
    (f"{scope} {metric}", True) for scope in ("Self", "Inclusive")
    for metric in ("share BASE", "share NEW", "change (pp)",
                   "estimated weight BASE (ns)", "estimated weight NEW (ns)",
                   "estimated weight change (ns)")]


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


def compare(base: ProfileSet, new: ProfileSet,
            benchmarks: tuple[Benchmark, Benchmark] | None = None) -> tuple[str, str]:
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
        content.append(section("functions", "CPU shares and estimated weights", tag("p", note),
                               table("diff-functions", "Functions", CPU_HEADERS, rows)))
        text += [NOISE, note, " | ".join(h[0] for h in CPU_HEADERS)]
        text += [" | ".join(row) for row in rows]
        complete = all(p.correlation is not None and
                       p.report["activation_correlation"]["status"] == "complete"
                       for p in (base, new))
        if complete:
            assert base.correlation is not None and new.correlation is not None
            tasks = sorted(set(profile_tasks(base)) | set(profile_tasks(new)))
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
            hidden = set(remaining)
            for label, p, evidence in (("BASE", base, br), ("NEW", new, nr)):
                assert p.correlation is not None
                refs = {i for i, a in enumerate(p.correlation["activations"])
                        if (a["participant"], a["task"]) in hidden}
                for cls in ("execution_context", "window_associated"):
                    omitted = [r for r in evidence if r[2] == cls and r[3] in refs]
                    note += (f" {label} {cls}: {sum(r[4] for r in omitted)} samples / "
                             f"{sum(r[5] for r in omitted)} ns in unexpanded Tasks.")
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
    inputs = {"base": base.report, "new": new.report}
    if benchmarks is not None:
        benchmark_text, benchmark_html = benchmark_context(*benchmarks, (base, new))
        text += benchmark_text
        content.append(benchmark_html)
        inputs["benchmarks"] = {label: vars(evidence) for label, evidence
                                in zip(("base", "new"), benchmarks)}
    body = join(content)
    html = page(title, body, json.dumps(inputs),
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
    parser.add_argument("--base-benchmark", type=Path, action=GivenOnce,
                        help="separate BASE repeated benchmark; requires --new-benchmark")
    parser.add_argument("--new-benchmark", type=Path, action=GivenOnce,
                        help="separate NEW repeated benchmark; requires --base-benchmark")
    args = parser.parse_args(argv)
    if (args.base_benchmark is None) != (args.new_benchmark is None):
        return command_error(PROG, "--base-benchmark and --new-benchmark must be supplied together", 2)
    if args.html is not None:
        problem = new_path_problem("HTML report", args.html)
        if problem:
            return command_error(PROG, problem, 2)
    try:
        benchmarks = ((load_benchmark(args.base_benchmark), load_benchmark(args.new_benchmark))
                      if args.base_benchmark is not None else None)
        terminal, html = compare(load_profile_set(args.base), load_profile_set(args.new), benchmarks)
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
