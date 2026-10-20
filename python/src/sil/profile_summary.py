"""The terminal summary of a saved profile (#315, #317, #318).

`sil profile` prints it after a capture, and `sil profile render` prints it
again from the saved `profile.json`. It reads the report only.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

from sil.diagnostics_format import format_bytes, format_duration_ns


def format_summary(report: dict, path: Path) -> str:
    """The summary of a valid `sil_profile_report` v1 document."""
    if report["capture"]["kind"] == "heap":
        return _heap_summary(report, path)
    return _cpu_summary(report, path)


def _clean(text) -> str:
    """Report text for a terminal: a control character (an ANSI escape, a
    newline) is written as its escape, so it cannot act on the terminal."""
    return "".join(f"\\x{ord(c):02x}" if unicodedata.category(c) == "Cc"
                   else c for c in str(text))


def _identity_lines(report: dict) -> list[str]:
    identity = report["identity"]

    def known(key: str) -> str:
        value = identity.get(key)
        return "unknown" if value is None else _clean(value)

    return [f"manifest: {_clean(identity['manifest_path'])} "
            f"({known('manifest_hash')})",
            f"sil: {known('sil_version')}, {known('source_revision')}"]


def _limitations_line(report: dict) -> str:
    return "limitations: " + ", ".join(_clean(item) for item in
                                       report["limitations"])


def _share(part: int, whole: int) -> str:
    return f"{100 * part / whole:5.1f}%" if whole else "    -"


def _cpu_summary(report: dict, path: Path) -> str:
    run = report["run"]
    capture = report["capture"]
    totals = capture["totals"]
    state = capture["status"] + (f" ({', '.join(capture['reasons'])})"
                                 if capture["reasons"] else "")
    exit_text = (f"exit code {run['exit_code']}" if run["exit_code"]
                 is not None else f"signal {run['signal']}"
                 if run["signal"] else "no exit code")
    sampling = capture["sampling"]
    call_graph = capture["call_graph"]
    method = (f"DWARF call graph ({call_graph['stack_bytes']} bytes)"
              if call_graph["method"] == "dwarf"
              else "frame-pointer call graph")
    lines = [
        f"profile: {_clean(path)}",
        f"run: {run['status']}, {exit_text}",
        f"capture: cpu, {state}",
        *_identity_lines(report),
        f"sampling: perf {sampling['event']} at {sampling['frequency_hz']} "
        f"Hz, {method}, the runner and all its descendants",
    ]
    lost = totals["lost_samples"]
    lines.append(
        f"samples: {totals['samples']}, weight "
        f"{format_duration_ns(totals['weight_ns'])} of sampled on-CPU time "
        "(an estimate, not a duration); lost "
        + ("unknown" if lost is None else str(lost)))
    if totals["samples"] == 0:
        lines.append("no sample: this is not evidence that no CPU time was "
                     "used")
    observed = capture["processes"]
    lines.append(f"processes: {len(observed)} observed, "
                 f"{sum(1 for p in observed if p['samples'])} with samples")
    names = capture["symbolization"]["function_names"]["samples_with_name"]
    source = capture["symbolization"]["source_lines"]
    with_line = source["samples_with_line"]
    lines.append(
        f"symbols: function names for {names} of {totals['samples']} "
        "samples; source lines "
        + (f"for {with_line} (leaf frames)" if with_line is not None else
           f"unavailable ({next(iter(source['unavailable'].values()))})"))
    missing = [m for m in capture["modules"] if m["material"] ==
               "unavailable" and not m["path"].startswith("[")]
    if missing:
        lines.append(
            f"modules: {len(missing)} of {len(capture['modules'])} have no "
            "file with their build ID, so their samples have no function "
            "name; name a copy with --modules-from (for example the FMU "
            "archive)")
    lines += _activation_lines(report["activation_correlation"])
    lines.append(_limitations_line(report))
    modules = {m["id"]: m["path"] for m in capture["modules"]}
    rows = capture["hotspots"]["self"][:10]
    if rows:
        lines.append("top self weight (share of all sample weight):")
        for row in rows:
            name = _clean(row["symbol"] or "[unknown function]")
            module = Path(modules.get(row["module"]) or "[unknown module]")
            lines.append(f"  {_share(row['self_weight_ns'], totals['weight_ns'])}"
                         f"  {name} ({_clean(module.name)})")
        rest = totals["weight_ns"] - sum(r["self_weight_ns"] for r in rows)
        if rest:
            lines.append(f"  {_share(rest, totals['weight_ns'])}  "
                         "all other functions")
    return "\n".join(lines) + "\n"


def _activation_lines(correlation: dict) -> list[str]:
    if correlation["kind"] != "activation":
        return []
    lines = [f"activations: {correlation['status']}"
             + (f" ({', '.join(correlation['reasons'])})"
                if correlation["reasons"] else "")]
    summary = correlation["summary"]
    if summary is None:
        lines.append("  no sample is related to an Activation: the join of "
                     "the two clocks or processes is not proven")
        return lines
    samples = summary["samples"]
    total = samples["total"]["samples"]
    lines.append(
        f"  of {total} samples: {samples['execution_context']['samples']} "
        "in an Activation's execution context (runner thread), "
        f"{samples['window_associated']['samples']} in a Process Step "
        "window (associated, not proven to be Step work), "
        f"{samples['lifecycle']['samples']} lifecycle, "
        f"{samples['replay']['samples']} Replay, "
        f"{samples['unattributed']['samples']} unattributed")
    silent = sum(t["without_samples"] for t in summary["tasks"])
    if silent:
        lines.append(f"  {silent} Activations have no sample: that is not "
                     "evidence that they used no CPU time")
    return lines


def _heap_summary(report: dict, path: Path) -> str:
    run = report["run"]
    capture = report["capture"]
    state = capture["status"] + (f" ({', '.join(capture['reasons'])})"
                                 if capture["reasons"] else "")
    exit_text = (f"exit code {run['exit_code']}" if run["exit_code"]
                 is not None else f"signal {run['signal']}"
                 if run["signal"] else "no exit code")
    scope = capture["scope"]
    if scope["kind"] == "runner":
        where = "the runner process (every Native participant)"
    elif scope["kind"] == "process_participant":
        where = f"Process participant '{scope['participant']}'"
    else:
        where = (f"unsupported '{scope['requested']}' "
                 f"({scope['reason']})")
    lines = [f"profile: {_clean(path)}", f"run: {run['status']}, {exit_text}",
             f"capture: heap, {state}", *_identity_lines(report),
             f"scope: {_clean(where)}, "
             f"{_clean(capture['backend']['version'] or 'Heaptrack')}",
             "activation correlation: unavailable (Heaptrack's timestamps "
             "have no validated mapping to host time)"]
    analysis = capture["analysis"]
    if analysis is None:
        lines.append(_limitations_line(report))
        return "\n".join(lines) + "\n"
    totals = analysis["totals"]
    lines += [
        f"allocations: {totals['allocations']} calls, "
        f"{format_bytes(totals['allocated_bytes'])} allocated; "
        f"{totals['temporary_allocations']} temporary",
        f"at capture end: {totals['outstanding_allocations']} outstanding, "
        f"{format_bytes(totals['outstanding_bytes'])} (not a leak verdict); "
        f"peak tracked heap {format_bytes(totals['peak_tracked_bytes'])} "
        "(not RSS)"]
    coverage = analysis["coverage"]
    unknown = coverage["allocations_unknown_module"] + \
        coverage["allocations_unknown_symbol"]
    if unknown:
        lines.append(f"symbols: {unknown} of {totals['allocations']} "
                     "allocations have no function name")
    lines.append(_limitations_line(report))
    modules = {m["id"]: m["path"] for m in analysis["modules"]}
    rows = analysis["hotspots"]["self"][:10]
    if rows:
        lines.append("top allocation sites (innermost frame, bytes "
                     "allocated):")
        for row in rows:
            name = _clean(row["symbol"] or "[unknown function]")
            module = Path(modules.get(row["module"]) or "[unknown module]")
            lines.append(f"  {format_bytes(row['allocated_bytes']):>10}  "
                         f"{row['allocations']:>8} calls  {name} "
                         f"({_clean(module.name)})")
        calls = totals["allocations"] - sum(r["allocations"] for r in rows)
        size = totals["allocated_bytes"] - sum(r["allocated_bytes"]
                                               for r in rows)
        if calls or size:
            lines.append(f"  {format_bytes(size):>10}  {calls:>8} calls  "
                         "all other sites")
    return "\n".join(lines) + "\n"
