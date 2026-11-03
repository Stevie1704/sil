#!/usr/bin/env python3
"""Interpret proof artifacts without treating perf acks as collection edges."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import statistics

MAX_INPUT = 256 << 20


def read(path):
    if path.stat().st_size > MAX_INPUT:
        raise ValueError(f"input exceeds proof budget: {path}")
    return json.loads(path.read_text())


def per_run(directory, row):
    notices = read(directory / "notices.json")
    marker_path = directory / "markers.json"
    markers = read(marker_path) if marker_path.exists() else None
    sample_path = directory / "sample-times.json"
    times = read(sample_path) if sample_path.exists() else []
    requested = row["requested"]
    reasons = []
    intervals, opened = [], {}
    if markers is not None:
        if markers["lost"]: reasons.append("workload_marker_loss")
        for host, task, index, edge in markers["events"]:
            key = task, index
            if edge == 0: opened[key] = host
            elif key in opened:
                intervals.append({"task": task, "index": index,
                                  "begin_ns": opened.pop(key), "end_ns": host})
    else:
        reasons.append("workload_markers_missing")
    comparison = requested or {"from_ns": 610_000_000, "to_ns": 611_000_000, "task": None}
    selected = [i for i in intervals
                if comparison["from_ns"] <= i["index"]*1_000_000 < comparison["to_ns"]
                and (comparison["task"] is None or i["task"] == comparison["task"])]
    observer = row.get("observer")
    if observer and (observer["dropped"] or observer["sent"] != observer["received"]):
        reasons.append("notification_loss")
    if not observer and row["mode"] not in ("ordinary", "ordinary-failure", "diagnostics", "whole-baseline", "unclosed"):
        reasons.append("observer_footer_missing")
    if row["mode"] == "unclosed": reasons.append("observer_footer_missing_after_sigkill")
    if row["stop_reason"]: reasons.append(row["stop_reason"])
    if any(code for code in row.get("exports", {}).values()): reasons.append("export_incomplete")
    metadata = row.get("metadata", {})
    if metadata.get("lost_records"): reasons.append("perf_lost_records")
    if metadata.get("throttle_records"): reasons.append("perf_throttle_records")
    if any(c["ack_ns"] is None for c in row["controls"]): reasons.append("control_unacknowledged")

    observations = directory / "activations.jsonl"
    work_rows, footer = [], None
    if observations.exists():
        if observations.stat().st_size > MAX_INPUT: raise ValueError("observations too large")
        for line in observations.read_text().splitlines():
            record = json.loads(line)
            if record.get("kind") == "activation": work_rows.append(record)
            if record.get("kind") == "end": footer = record
        if footer is None or footer.get("dropped") or footer.get("truncated") or footer.get("write_failed"):
            reasons.append("activation_observations_incomplete")
    failed = [r for r in work_rows if r["outcome"] != "completed"]
    # A workload marker exit proves only that window_work returned, not that
    # its encompassing Activation completed (e.g. deliberate api->fail).
    if failed: reasons.append("failed_or_interrupted_activation")
    if row["exit_code"] and not failed: reasons.append("unclosed_run_work")
    if requested is None:
        selection_state = "whole_run_or_unprofiled"
    elif requested["from_ns"] == requested["to_ns"]:
        selection_state = "empty"
    elif selected:
        selection_state = "reached"
    elif any(requested["from_ns"] <= n[1] < requested["to_ns"] for n in notices):
        selection_state = "reached_but_no_closed_workload_interval"
    else:
        selection_state = "unreached"
    begin = min((i["begin_ns"] for i in selected), default=None)
    end = max((i["end_ns"] for i in selected), default=None)
    inside = sum(any(i["begin_ns"] <= ns < i["end_ns"] for i in selected) for ns in times)
    hot_samples = 0
    symbols_path = directory / "symbols.txt"
    if symbols_path.exists():
        if symbols_path.stat().st_size > MAX_INPUT: raise ValueError("symbols too large")
        for line in symbols_path.read_text(errors="replace").splitlines():
            match = re.match(r"^\s*(\d+)\.(\d{9}):?\s", line)
            if match and "late_hot_function" in line:
                ns = int(match[1])*1_000_000_000 + int(match[2])
                hot_samples += any(i["begin_ns"] <= ns < i["end_ns"] for i in selected)
    commands = []
    for c in row["controls"]:
        prior = [n for n in notices if n[4] <= c["write_ns"]]
        notice = prior[-1] if prior else None
        commands.append({**c, "ack_latency_ns": c["ack_ns"]-c["write_ns"] if c["ack_ns"] else None,
                         "preceding_notice": notice,
                         "notice_to_write_ns": c["write_ns"]-notice[0] if notice else None})
    # Check setup metadata by its own host timestamp, before the first enable
    # request. Export text is retained so these counts are independently replayable.
    first_enable = next((c["write_ns"] for c in row["controls"] if c["command"] == "enable"), None)
    setup = {"exec_before_enable": 0, "workload_mmap_before_enable": 0,
             "exit_after_disable": 0}
    last_disable = next((c["ack_ns"] for c in reversed(row["controls"])
                         if c["command"] == "disable" and c["ack_ns"]), None)
    metadata_path = directory / "metadata.txt"
    if metadata_path.exists():
        if metadata_path.stat().st_size > MAX_INPUT: raise ValueError("metadata too large")
        for line in metadata_path.read_text(errors="replace").splitlines():
            match = re.search(r"\s(\d+)\.(\d{9}):", line)
            if not match: continue
            ns = int(match[1])*1_000_000_000 + int(match[2])
            if first_enable is not None and ns < first_enable:
                setup["exec_before_enable"] += "PERF_RECORD_COMM exec" in line
                setup["workload_mmap_before_enable"] += "PERF_RECORD_MMAP" in line and "workload.so" in line
            if last_disable is not None and ns >= last_disable:
                setup["exit_after_disable"] += "PERF_RECORD_EXIT" in line
    return {"selection_state": selection_state, "reasons": reasons,
            "intentional_disabled_regions": requested is not None,
            "effective_transition_times": "not_observed",
            "sample_times_are_not_continuous_coverage": True,
            "known_selected_work": selected, "unclosed_workload_entries": len(opened),
            "comparison_selection": comparison, "selected_hot_function_leaf_samples": hot_samples,
            "failed_activations": len(failed), "completed_activations": sum(r["outcome"] == "completed" for r in work_rows),
            "first_sample_minus_known_work_begin_ns": times[0]-begin if times and begin is not None else None,
            "last_sample_minus_known_work_end_ns": times[-1]-end if times and end is not None else None,
            "samples_inside_known_selected_work": inside,
            "samples_outside_known_selected_work": len(times)-inside,
            "control_observations": commands, "setup_metadata": setup}


def summarize(root):
    results = read(root / "results.json")
    if not results["runs"]:
        return {"availability": "unavailable", "comparisons": [], "runs": []}
    rows = []
    for row in results["runs"]:
        candidates = list(root.glob(f"{row['variant']}-{row['mode']}*"))
        # Match by raw result identity; timing makes repetitions distinct.
        directory = next((p for p in candidates if (p / "result.json").exists()
                          and read(p / "result.json")["elapsed_ns"] == row["elapsed_ns"]), None)
        if directory is None:
            raise ValueError("run artifact missing")
        rows.append({"directory": directory.name, **row, "analysis": per_run(directory, row)})
    comparisons = []
    for variant in ("native", "process"):
        group = [r for r in rows if r["variant"] == variant]
        def median(mode, key):
            values = [r[key] for r in group if r["mode"] == mode and r["stop_reason"] is None]
            return statistics.median(values) if values else None
        whole, window = median("whole-baseline", "perf_bytes"), median("window", "perf_bytes")
        ordinary = median("ordinary", "elapsed_ns")
        comparisons.append({"variant": variant, "whole_median_bytes": whole,
                            "window_median_bytes": window,
                            "window_to_whole_bytes": window/whole if whole else None,
                            "elapsed_median_ns": {mode: median(mode, "elapsed_ns") for mode in
                                ("ordinary", "diagnostics", "observer", "whole-baseline", "whole", "window")},
                            "ordinary_elapsed_median_ns": ordinary})
    return {"availability": "available", "comparisons": comparisons, "runs": rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    report = summarize(args.evidence)
    (args.evidence / "analysis.json").write_text(json.dumps(report, indent=2) + "\n")
