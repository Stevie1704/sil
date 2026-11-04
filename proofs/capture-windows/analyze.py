"""File-input/report-output analysis; validate.py is the single CLI entry point."""
from dataclasses import dataclass
import json
import re
import statistics

from model import CASES, MAX_INPUT_BYTES, CaptureWindow, Notice, WorkInterval, perf_sample_host_ns


def read(path):
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError(f"input exceeds proof budget: {path}")
    return json.loads(path.read_text())


def lines(path):
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError(f"input exceeds proof budget: {path}")
    return path.read_text(errors="replace").splitlines()


@dataclass
class WorkEvidence:
    intervals: list
    unclosed_entries: int
    reasons: list


def workload_intervals(directory):
    path = directory / "markers.json"
    if not path.exists():
        return WorkEvidence([], 0, ["workload_markers_missing"])
    markers = read(path)
    if markers.get("sil_capture_workload_markers") != 2:
        raise ValueError("unsupported workload marker version")
    intervals, opened = [], {}
    for host, task, index, virtual_ns, edge in markers["events"]:
        key = task, index
        if edge == 0:
            opened[key] = (host, virtual_ns)
        elif key in opened:
            begin, begin_virtual_ns = opened.pop(key)
            if virtual_ns != begin_virtual_ns or host < begin:
                raise ValueError("inconsistent workload interval")
            intervals.append(WorkInterval(task, index, virtual_ns, begin, host))
    return WorkEvidence(intervals, len(opened), ["workload_marker_loss"] if markers["lost"] else [])


def activation_evidence(directory, case):
    path = directory / "activations.jsonl"
    if not path.exists():
        return [], [] if case.runner == "ordinary" else ["activation_observations_incomplete"]
    rows = [json.loads(line) for line in lines(path)]
    work = [row for row in rows if row.get("kind") == "activation"]
    footer = next((row for row in reversed(rows) if row.get("kind") == "end"), None)
    incomplete = footer is None or any(footer.get(key) for key in ("dropped", "truncated", "write_failed"))
    return work, ["activation_observations_incomplete"] if incomplete else []


def capture_reasons(row, case, work, observation_reasons):
    reasons = list(observation_reasons)
    notifier = row.get("observer")
    if notifier and (notifier["dropped"] or notifier["sent"] != notifier["received"]):
        reasons.append("notification_loss")
    if case.notifications and not notifier:
        reasons.append("observer_footer_missing_after_sigkill" if case.outcome == "killed" else "observer_footer_missing")
    if row["stop_reason"]:
        reasons.append(row["stop_reason"])
    if any(row.get("exports", {}).values()):
        reasons.append("export_incomplete")
    metadata = row.get("metadata", {})
    for field, reason in (("lost_records", "perf_lost_records"), ("throttle_records", "perf_throttle_records")):
        if metadata.get(field):
            reasons.append(reason)
    if any(c["ack_ns"] is None for c in row["controls"]):
        reasons.append("control_unacknowledged")
    failed = any(r["outcome"] != "completed" for r in work)
    if failed:
        reasons.append("failed_or_interrupted_activation")
    if row["run_exit_code"] is None and row["exit_code"] and not failed:
        reasons.append("unclosed_run_work")
    if row["exit_code"] and row["run_exit_code"] == 0:
        reasons.append("collector_failed")
    if row["run_exit_code"] not in (None, 0):
        reasons.append("run_failed")
    return reasons


def perf_loss_sources(directory, row):
    sources = ["PERF_RECORD_LOST"] if row.get("metadata", {}).get("lost_records") else []
    path = directory / "stats.txt"
    if path.exists() and row.get("exports", {}).get("stats.txt", 0) == 0:
        if any(re.match(r"^\s*LOST_SAMPLES events:\s*[1-9][0-9]*\b", line) for line in lines(path)):
            # Stats may contain record counts and an aggregated counter. Both
            # establish loss; neither is silently substituted for the other.
            sources.append("perf_report_LOST_SAMPLES")
    return sources


def requested_end(row, notices):
    if row["requested"] is None:
        return "not_applicable", None
    selection = CaptureWindow(**row["requested"])
    if selection.from_ns == selection.to_ns:
        return "empty", None
    boundary = next((n for n in notices if n.edge == 0 and n.virtual_ns >= selection.to_ns), None)
    if boundary is not None:
        return "observed", boundary._asdict()
    if row["run_exit_code"] not in (None, 0):
        return "not_reached_run_failed", None
    if row["run_exit_code"] is None and row["exit_code"]:
        return "not_reached_run_killed", None
    return "run_completed_without_end_notice", None


def selection_state(row, selection, notices, selected, end_state):
    if row["requested"] is None:
        return "whole_run_or_unprofiled"
    if selection.from_ns == selection.to_ns:
        return "empty"
    entered = selected or any(n.edge == 0 and selection.contains(n.virtual_ns, n.task) for n in notices)
    if entered and end_state in ("not_reached_run_failed", "not_reached_run_killed"):
        return "terminated_before_requested_end"
    if selected:
        return "reached"
    return "reached_but_no_closed_workload_interval" if entered else "unreached"


def disabled_regions(row, host_times):
    current, regions = None, []
    if row["requested"] is not None:
        current = {"intent": "initially_disabled", "begin_request_host_ns": row.get("collector_start_host_ns"),
                   "disable_ack_host_ns": None}
    for control in row["controls"]:
        if control["command"] == "disable":
            current = {"intent": row["stop_reason"] or "outside_selection",
                       "begin_request_host_ns": control["write_ns"], "disable_ack_host_ns": control["ack_ns"]}
        elif current is not None:
            current["end_request_host_ns"] = control["write_ns"]
            regions.append(current)
            current = None
    if current is not None:
        current["end_request_host_ns"] = row.get("collector_exit_observed_host_ns")
        regions.append(current)
    for region in regions:
        begin, end = region["begin_request_host_ns"], region["end_request_host_ns"]
        region["effective_boundaries"] = "unknown"
        region["observed_samples_in_request_interval"] = (
            sum(begin <= ns < end for ns in host_times) if begin is not None and end is not None else None)
    return regions


def control_observations(controls, notices):
    result = []
    for control in controls:
        if control["ack_ns"] is not None and control["ack_ns"] < control["write_ns"]:
            raise ValueError("acknowledgment precedes command")
        prior = [notice for notice in notices if notice.received_host_ns <= control["write_ns"]]
        notice = prior[-1] if prior else None
        result.append({**control,
            "ack_latency_ns": control["ack_ns"]-control["write_ns"] if control["ack_ns"] is not None else None,
            "preceding_notice": notice._asdict() if notice else None,
            "notice_to_write_ns": control["write_ns"]-notice.host_ns if notice else None})
    return result


def setup_metadata(directory, controls):
    result = {"exec_before_enable": 0, "workload_mmap_before_enable": 0, "exit_after_disable": 0}
    first_enable = next((c["write_ns"] for c in controls if c["command"] == "enable"), None)
    last_disable = next((c["ack_ns"] for c in reversed(controls) if c["command"] == "disable" and c["ack_ns"]), None)
    path = directory / "metadata.txt"
    if not path.exists():
        return result
    for line in lines(path):
        host_ns = perf_sample_host_ns(line)
        if host_ns is None:
            continue
        if first_enable is not None and host_ns < first_enable:
            result["exec_before_enable"] += "PERF_RECORD_COMM exec" in line
            result["workload_mmap_before_enable"] += "PERF_RECORD_MMAP" in line and "workload.so" in line
        if last_disable is not None and host_ns >= last_disable:
            result["exit_after_disable"] += "PERF_RECORD_EXIT" in line
    return result


def selected_work_summary(directory, selected, host_times):
    contains = lambda ns: any(interval.contains(ns) for interval in selected)
    inside = sum(contains(ns) for ns in host_times)
    hot_samples = 0
    path = directory / "symbols.txt"
    if path.exists():
        for line in lines(path):
            if "late_hot_function" in line and (host_ns := perf_sample_host_ns(line)) is not None:
                hot_samples += contains(host_ns)
    begin = min((i.begin_ns for i in selected), default=None)
    end = max((i.end_ns for i in selected), default=None)
    return {"known_selected_work": [i.json() for i in selected],
            "known_interval_sample_counts": [{"task": i.task, "index": i.index,
                "samples": sum(i.contains(ns) for ns in host_times)} for i in selected],
            "selected_hot_function_leaf_samples": hot_samples,
            "samples_inside_known_selected_work": inside,
            "samples_outside_known_selected_work": len(host_times)-inside,
            "first_perf_sample_host_ns_minus_work_begin_ns": min(host_times)-begin if host_times and begin is not None else None,
            "last_perf_sample_host_ns_minus_work_end_ns": max(host_times)-end if host_times and end is not None else None}


def per_run(directory, row):
    case = CASES[row["mode"]]
    notices = [Notice(*raw) for raw in read(directory / "notices.json")]
    path = directory / "perf-sample-host-times.json"
    host_times = read(path) if path.exists() else []
    work = workload_intervals(directory)
    selection = CaptureWindow(**row["requested"]) if row["requested"] else CaptureWindow()
    selected = [i for i in work.intervals if selection.contains(i.virtual_ns, i.task)]
    excluded = next((i for i in work.intervals if i.virtual_ns >= selection.to_ns), None)
    activations, observation_reasons = activation_evidence(directory, case)
    reasons = capture_reasons(row, case, activations, [*work.reasons, *observation_reasons])
    loss_sources = perf_loss_sources(directory, row)
    if loss_sources:
        reasons.append("perf_sample_loss")
    end_state, end_notice = requested_end(row, notices)
    return {"selection_state": selection_state(row, selection, notices, selected, end_state),
            "requested_end_state": end_state, "requested_end_notice": end_notice,
            "disabled_regions": disabled_regions(row, host_times), "reasons": reasons,
            "first_excluded_work": excluded.json() if excluded else None,
            "perf_loss_sources": loss_sources,
            "effective_transition_times": "not_observed",
            "perf_sample_host_times_do_not_establish_continuous_coverage": True,
            "unclosed_workload_entries": work.unclosed_entries,
            "comparison_selection": selection.json(),
            "failed_activations": sum(r["outcome"] != "completed" for r in activations),
            "completed_activations": sum(r["outcome"] == "completed" for r in activations),
            "control_observations": control_observations(row["controls"], notices),
            "setup_metadata": setup_metadata(directory, row["controls"]),
            **selected_work_summary(directory, selected, host_times)}


def comparisons(rows):
    result = []
    for variant in ("native", "process"):
        group = [row for row in rows if row["variant"] == variant]
        def median(mode, key):
            values = [row[key] for row in group if row["mode"] == mode and row["stop_reason"] is None]
            return statistics.median(values) if values else None
        whole, bounded = median("whole-baseline", "perf_bytes"), median("window", "perf_bytes")
        result.append({"variant": variant, "whole_median_bytes": whole, "window_median_bytes": bounded,
                       "window_to_whole_bytes": bounded/whole if whole else None,
                       "elapsed_median_ns": {name: median(name, "elapsed_ns")
                           for name, case in CASES.items() if case.repeats > 1}})
    return result


def summarize(root):
    results = read(root / "results.json")
    if not results["runs"]:
        return {"availability": "unavailable", "comparisons": [], "runs": []}
    rows = []
    for row in results["runs"]:
        candidates = root.glob(f"{row['variant']}-{row['mode']}*")
        directory = next((p for p in candidates if (p / "result.json").exists()
                          and read(p / "result.json")["elapsed_ns"] == row["elapsed_ns"]), None)
        if directory is None:
            raise ValueError("run artifact missing")
        rows.append({"directory": directory.name, **row, "analysis": per_run(directory, row)})
    return {"availability": "available", "comparisons": comparisons(rows), "runs": rows}
