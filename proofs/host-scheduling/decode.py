"""Decode pinned perf text and bind independent markers to actual Process Steps."""
from __future__ import annotations

import json
from pathlib import Path
import re

from report import INPUT_BYTES, analyze

LINE = re.compile(r"\[(\d+)\]\s+(\d+)\.(\d{9}):\s+sched:(sched_\w+):\s+(.*)")


def lines(path):
    if path.stat().st_size > INPUT_BYTES:
        raise ValueError(f"input exceeds {INPUT_BYTES} bytes: {path}")
    return path.read_text().splitlines()


def parse_events(path):
    events, unknown = [], 0
    for line in lines(path):
        if not line.strip() or line.startswith("#"):
            continue
        match = LINE.search(line)
        if not match:
            unknown += 1
            continue
        cpu, seconds, fraction, name, payload = match.groups()
        row = {"cpu": int(cpu), "time_ns": int(seconds) * 10**9 + int(fraction)}
        fields = dict(re.findall(r"(\w+)=([^\s]+)", payload))
        if name == "sched_switch":
            switch = re.fullmatch(r".+:(\d+) \[\d+\] (\S+) ==> .+:(\d+) \[\d+\]", payload)
            if not switch:
                unknown += 1
                continue
            prev, state, nxt = switch.groups()
            row.update(event="switch", prev_pid=int(prev), next_pid=int(nxt), prev_state=state)
        elif name in ("sched_wakeup", "sched_wakeup_new"):
            wakeup = re.fullmatch(r".+:(\d+) \[\d+\] CPU:\d+", payload)
            if not wakeup:
                unknown += 1
                continue
            row.update(event="wakeup", pid=int(wakeup[1]))
        elif name == "sched_process_fork":
            row.update(event="fork", parent_pid=int(fields["pid"]),
                       child_pid=int(fields["child_pid"]))
        elif name == "sched_process_exec":
            row.update(event="exec", pid=int(fields["pid"]), old_pid=int(fields["old_pid"]))
        elif name == "sched_process_exit":
            row.update(event="exit", pid=int(fields["pid"]))
        else:
            unknown += 1
            continue
        events.append(row)
    return events, unknown


def prepare(out: Path, result: dict, namespace: list[int]):
    markers = [json.loads(line) for line in lines(out / "markers.jsonl")]
    observations = ([json.loads(line) for line in lines(out / "activations.jsonl")]
                    if (out / "activations.jsonl").exists() else [])
    headers = [row for row in observations if "sil_activation_observations" in row]
    header = headers[0] if len(headers) == 1 else {}
    expected_ns = {"device": namespace[0], "inode": namespace[1]}
    namespace_ok = (header.get("pid_namespace") == expected_ns
                    and all(m["namespace"] == namespace for m in markers)
                    and result["initial_pid_namespace"])
    attributes = "\n".join(line for line in lines(out / "attributes.txt") if line.startswith("sched:"))
    # Verify the retained event attributes, not just the requested CLI option.
    clock_ok = (header.get("clock") == "host_monotonic"
                and len(re.findall(r"\bclockid\s*:\s*1\b", attributes)) == 6
                and len(re.findall(r"use_clockid\s*:\s*1\b", attributes)) == 6)
    events, unparsed = parse_events(out / "events.txt")
    descendants = {header.get("runner_pid")}
    for event in events:
        if event["event"] == "fork" and event["parent_pid"] in descendants:
            descendants.add(event["child_pid"])
    events = [event for event in events if event["event"] != "fork"
              or event["child_pid"] in descendants]
    warnings = "\n".join(lines(out / "events-errors.txt"))
    stats = "\n".join(lines(out / "stats.txt"))
    loss_sources = []
    if re.search(r"LOST(?:_SAMPLES)?(?: events)?\s*:\s*[1-9]\d*", stats):
        loss_sources.append("perf_statistics")
    if re.search(r"lost\s+[1-9]\d*|[1-9]\d*\s+(?:lost|events lost)", warnings, re.I):
        loss_sources.append("perf_script_warning")
    if "PERF_RECORD_LOST" in stats:
        loss_sources.append("perf_lost_record")

    starts = {(m["participant"], m["index"], m["phase"], m["tid"]): m
              for m in markers if m["edge"] == "begin"}
    windows = []
    for row in observations:
        if row.get("kind") != "activation" or row.get("task_kind") != "process_step":
            continue
        marker = next((m for m in markers if m["phase"] == "step" and m["edge"] == "begin"
                       and m["participant"] == row["participant"]
                       and m["index"] == row["index"]), None)
        if marker:
            ends = [m for m in markers if m["phase"] == "step" and m["edge"] == "end"
                    and m["participant"] == marker["participant"] and m["index"] == marker["index"]]
            # Independent direct-syscall markers must sit inside actual runner
            # intervals on the same clock. Mere CLOCK_MONOTONIC labels are insufficient.
            if not (int(row["begin_ns"]) <= marker["host_ns"] <= int(row["end_ns"])):
                clock_ok = False
            if ends and not (marker["host_ns"] <= ends[0]["host_ns"] <= int(row["end_ns"])):
                clock_ok = False
            spawns = [r for r in observations if r.get("kind") == "spawn"
                      and r["participant"] == marker["participant"] and r["pid"] == marker["pid"]]
            if len(spawns) != 1 or not any(e["event"] == "fork" and e["child_pid"] == marker["tid"]
                    and e["parent_pid"] == header.get("runner_pid")
                    and int(spawns[0]["at_ns"]) <= e["time_ns"] <= marker["host_ns"] for e in events):
                namespace_ok = False
            windows.append({"kind": "step", "participant": row["participant"],
                "index": row["index"], "virtual_ns": row["virtual_time_ns"],
                "tid": marker["tid"], "begin_ns": int(row["begin_ns"]),
                "end_ns": int(row["end_ns"]), "outcome": row["outcome"]})
    steps = list(windows)
    for marker in markers:
        if marker["edge"] != "end" or marker["phase"] in ("step", "lifetime"):
            continue
        start = starts[(marker["participant"], marker["index"], marker["phase"], marker["tid"])]
        windows.append({"kind": "fixture_phase", "phase": marker["phase"],
            "participant": marker["participant"], "index": marker["index"],
            "virtual_ns": marker["virtual_ns"], "tid": marker["tid"],
            "begin_ns": start["host_ns"], "end_ns": marker["host_ns"], "outcome": "fixture_closed"})
        if marker["phase"] in ("background", "wait_child", "competitor"):
            for step in steps:
                begin = max(step["begin_ns"], start["host_ns"])
                end = min(step["end_ns"], marker["host_ns"])
                if begin < end and step["participant"] == marker["participant"]:
                    windows.append({**step, "kind": "worker_window_association", "phase": marker["phase"],
                                    "tid": marker["tid"], "begin_ns": begin, "end_ns": end})
    for marker in markers:
        if marker["phase"] == "step" and marker["edge"] == "begin" and not any(
                w["kind"] == "step" and w["participant"] == marker["participant"]
                and w["index"] == marker["index"] for w in windows):
            windows.append({"kind": "step", "participant": marker["participant"],
                "index": marker["index"], "virtual_ns": marker["virtual_ns"],
                "tid": marker["tid"], "begin_ns": marker["host_ns"],
                "end_ns": None, "outcome": "unclosed" if result["run"]["exit_code"] == -9 else "interval_absent",
                "interval_source": "fixture_begin_only; runner interval absent"})
    document = {"clock": "host_monotonic" if clock_ok else "unknown",
        "namespace_verified": namespace_ok, "events": events, "windows": windows,
        "loss": bool(loss_sources), "truncated": (unparsed > 0 or any(
            result["exports"][key]["exit_code"] != 0 for key in ("events", "attributes", "stats")))}
    analysis = analyze(document)
    analysis.update(loss_sources=loss_sources, unparsed_lines=unparsed,
                    clock_verified=clock_ok, namespace_verified=namespace_ok,
                    activation_footer=next((r for r in observations if r.get("kind") == "end"), None))
    return document, analysis
