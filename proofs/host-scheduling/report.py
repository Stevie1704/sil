"""Offline #362 experiment reducer. No supported scheduler-state schema.

Only intervals closed by direct, consistent transitions are counted. Unknown
edges are never filled from sampled CPU, phase names or elapsed-time subtraction.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

INPUT_BYTES = 256 << 20
STATES = ("running", "runnable", "sleeping", "unknown")


def read(path: Path):
    if path.stat().st_size > INPUT_BYTES:
        raise ValueError(f"input exceeds {INPUT_BYTES} bytes: {path}")
    return json.loads(path.read_text())


def analyze(document: dict) -> dict:
    reasons = set()
    for key, expected, reason in (("clock", "host_monotonic", "clock_unverified"),
            ("namespace_verified", True, "namespace_unverified"),
            ("loss", False, "event_loss"), ("truncated", False, "truncated")):
        if document.get(key) != expected:
            reasons.add(reason)
    unusable = bool(reasons)
    lives, live, cpu_current = [], {}, {}

    def change(life, at, state, *, invalidate=False):
        if at < life["last_ns"]:
            raise ValueError("events are not in host-time order")
        if at > life["last_ns"]:
            life["segments"].append([life["last_ns"], at,
                                     "unknown" if invalidate else life["state"]])
        life["last_ns"], life["state"] = at, state

    previous = -1
    for e in document["events"]:
        at, kind = e["time_ns"], e["event"]
        if type(at) is not int or at < previous:
            raise ValueError("events are not in host-time order")
        previous = at
        if kind == "fork":
            tid = e["child_pid"]
            if tid in live:
                reasons.add("reused_tid_without_exit")
                change(live[tid], at, "unknown", invalidate=True)
                live[tid]["end_ns"] = at
            life = {"id": len(lives), "tid": tid, "parent_tid": e["parent_pid"],
                    "begin_ns": at, "end_ns": None, "exec_ns": [],
                    "last_ns": at, "state": "unknown", "segments": []}
            lives.append(life)
            live[tid] = life
        elif kind == "switch":
            prev, nxt, cpu = e["prev_pid"], e["next_pid"], e["cpu"]
            expected = cpu_current.get(cpu)
            if expected is not None and expected != prev:
                if expected in live or prev in live:
                    reasons.add("missing_switch")
                if expected in live:
                    change(live[expected], at, "unknown", invalidate=True)
            if prev in live:
                life = live[prev]
                state = e["prev_state"]
                after = ("runnable" if state in ("R", "R+") else
                         "sleeping" if state in ("S", "D", "I") else "unknown")
                invalid = life["state"] != "running"
                if invalid:
                    reasons.add("missing_switch")
                change(life, at, after, invalidate=invalid)
            if nxt in live:
                life = live[nxt]
                invalid = life["state"] in ("sleeping", "running")
                if invalid:
                    reasons.add("missing_wakeup" if life["state"] == "sleeping"
                                else "missing_switch")
                change(life, at, "running", invalidate=invalid)
            cpu_current[cpu] = nxt
        elif kind == "wakeup" and e["pid"] in live:
            life = live[e["pid"]]
            # A redundant wakeup need not dequeue a currently executing Task.
            if life["state"] != "running":
                change(life, at, "runnable")
        elif kind == "exec":
            if e["old_pid"] != e["pid"]:
                reasons.add("nonleader_exec_unqualified")
                for tid in (e["old_pid"], e["pid"]):
                    if tid in live:
                        life = live.pop(tid)
                        change(life, at, "unknown", invalidate=True)
                        life["end_ns"] = at
            elif e["pid"] in live:
                live[e["pid"]]["exec_ns"].append(at)
        elif kind == "exit" and e["pid"] in live:
            life = live.pop(e["pid"])
            change(life, at, "unknown")
            life["end_ns"] = at

    windows = []
    for window in document["windows"]:
        w = dict(window)
        begin, end = w["begin_ns"], w["end_ns"]
        w["lifetime"] = None
        w["states_ns"] = None
        if end is not None:
            if type(begin) is not int or type(end) is not int or begin > end:
                raise ValueError("invalid half-open host interval")
            totals = dict.fromkeys(STATES, 0)
            totals["unknown"] = end - begin
            candidates = [life for life in lives if life["tid"] == w["tid"]
                          and life["begin_ns"] <= begin
                          and (life["end_ns"] is None or end <= life["end_ns"])]
            if len(candidates) == 1:
                life = candidates[0]
                w["lifetime"] = life["id"]
                if not unusable:
                    for a, b, state in life["segments"]:
                        count = max(0, min(end, b) - max(begin, a))
                        if state != "unknown":
                            totals[state] += count
                            totals["unknown"] -= count
            else:
                reasons.add("lifetime_unverified")
            w["states_ns"] = totals
        windows.append(w)
    return {"decision": "defer", "scope": "host scheduling experiment only",
            "completed_steps": sum(w["outcome"] == "completed" for w in windows
                                   if w.get("kind", "step") == "step"),
            "reasons": sorted(reasons), "windows": windows,
            "lifetimes": [{k: v for k, v in life.items()
                           if k not in ("last_ns", "state", "segments")}
                          for life in lives]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--perf-text", type=Path, help="decode retained perf script output before reduction")
    args = parser.parse_args()
    document = read(args.input)
    if args.perf_text:
        from decode import parse_events
        document["events"], unparsed = parse_events(args.perf_text)
        document["truncated"] = document["truncated"] or unparsed > 0
    result = analyze(document)
    with args.output.open("x") as output:
        output.write(json.dumps(result, indent=2) + "\n")
