"""Reduce direct scheduler transitions; unknown edges never use CPU subtraction."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from evidence import read_json

STATES = ("running", "runnable", "sleeping", "unknown")
COVERAGE_CHECKS = (("clock", "host_monotonic", "clock_unverified"),
                   ("namespace_verified", True, "namespace_unverified"),
                   ("loss", False, "event_loss"), ("truncated", False, "truncated"))
STOP_REASONS = {"interrupted": "collector_interrupted", "size_guard": "collector_size_limit"}
PHASE_STATES = {"cpu": "running", "sleep": "sleeping", "wait": "sleeping", "contended": "runnable"}


def advance(life, at, state, *, invalidate=False):
    if at < life["last_ns"]:
        raise ValueError("events are not in host-time order")
    if at > life["last_ns"]:
        life["segments"].append([life["last_ns"], at,
                                 "unknown" if invalidate else life["state"]])
    life["last_ns"], life["state"] = at, state


class StateTimeline:
    def __init__(self, reasons):
        self.reasons = reasons
        self.lifetimes, self.live, self.cpu_current = [], {}, {}

    def consume(self, events):
        previous = -1
        handlers = {"fork": self.fork, "switch": self.switch, "wakeup": self.wakeup,
                    "exec": self.exec, "exit": self.exit}
        for event in events:
            at = event["time_ns"]
            if type(at) is not int or at < previous:
                raise ValueError("events are not in host-time order")
            previous = at
            handlers[event["event"]](event)

    def fork(self, event):
        tid, at = event["child_pid"], event["time_ns"]
        if tid in self.live:
            self.reasons.add("reused_tid_without_exit")
            self.retire(tid, at, invalidate=True)
        if not event.get("in_scope", True):
            return
        life = {"id": len(self.lifetimes), "tid": tid, "parent_tid": event["parent_pid"],
                "begin_ns": at, "end_ns": None, "exec_ns": [],
                "last_ns": at, "state": "unknown", "segments": []}
        self.lifetimes.append(life)
        self.live[tid] = life

    def retire(self, tid, at, *, invalidate=False):
        if tid in self.live:
            life = self.live.pop(tid)
            advance(life, at, "unknown", invalidate=invalidate)
            life["end_ns"] = at

    def switch(self, event):
        prev, nxt, cpu = event["prev_pid"], event["next_pid"], event["cpu"]
        expected = self.cpu_current.get(cpu)
        if expected is not None and expected != prev:
            if expected in self.live or prev in self.live:
                self.reasons.add("missing_switch")
            if expected in self.live:
                advance(self.live[expected], event["time_ns"], "unknown", invalidate=True)
        self.switch_out(prev, event["time_ns"], event["prev_state"])
        self.switch_in(nxt, event["time_ns"])
        self.cpu_current[cpu] = nxt

    def switch_out(self, tid, at, state):
        if tid not in self.live:
            return
        life = self.live[tid]
        after = ("runnable" if state in ("R", "R+") else
                 "sleeping" if state in ("S", "D", "I") else "unknown")
        invalid = life["state"] != "running"
        if invalid:
            self.reasons.add("missing_switch")
        advance(life, at, after, invalidate=invalid)

    def switch_in(self, tid, at):
        if tid not in self.live:
            return
        life = self.live[tid]
        invalid = life["state"] in ("sleeping", "running")
        if invalid:
            self.reasons.add("missing_wakeup" if life["state"] == "sleeping" else "missing_switch")
        advance(life, at, "running", invalidate=invalid)

    def wakeup(self, event):
        life = self.live.get(event["pid"])
        # A redundant wakeup need not dequeue an executing thread.
        if life and life["state"] != "running":
            advance(life, event["time_ns"], "runnable")

    def exec(self, event):
        if event["pid"] not in self.live and event["old_pid"] not in self.live:
            return
        if event["old_pid"] != event["pid"]:
            self.reasons.add("nonleader_exec_unqualified")
            for tid in (event["old_pid"], event["pid"]):
                self.retire(tid, event["time_ns"], invalidate=True)
        else:
            self.live[event["pid"]]["exec_ns"].append(event["time_ns"])

    def exit(self, event):
        self.retire(event["pid"], event["time_ns"])

    def window(self, source, unusable):
        window = {**source, "lifetime": None, "states_ns": None, "intervals": None}
        begin, end = window["begin_ns"], window["end_ns"]
        if end is None:
            return window
        if type(begin) is not int or type(end) is not int or begin > end:
            raise ValueError("invalid half-open host interval")
        matches = [life for life in self.lifetimes if life["tid"] == window["tid"]
                   and life["begin_ns"] <= begin
                   and (life["end_ns"] is None or end <= life["end_ns"])]
        segments = []
        if len(matches) == 1:
            window["lifetime"] = matches[0]["id"]
            if not unusable:
                segments = matches[0]["segments"]
        else:
            self.reasons.add("lifetime_unverified")
        window["intervals"] = partition(segments, begin, end)
        window["states_ns"] = {state: sum(b - a for a, b, label in window["intervals"]
                                         if label == state) for state in STATES}
        return window

    def identities(self):
        return [{k: v for k, v in life.items() if k not in ("last_ns", "state", "segments")}
                for life in self.lifetimes]


def partition(segments, begin, end):
    intervals, cursor = [], begin
    for a, b, state in segments:
        a, b = max(a, begin), min(b, end)
        if a >= b:
            continue
        if a < cursor:
            raise ValueError("overlapping state intervals")
        if cursor < a:
            intervals.append([cursor, a, "unknown"])
        intervals.append([a, b, state])
        cursor = b
    if cursor < end:
        intervals.append([cursor, end, "unknown"])
    return intervals


def analyze(document: dict) -> dict:
    reasons = {reason for key, expected, reason in COVERAGE_CHECKS if document.get(key) != expected}
    unusable = bool(reasons)
    stop_reason = document.get("collector_stop_reason")
    if stop_reason:
        reasons.add(STOP_REASONS.get(stop_reason, "collector_stopped"))
    timeline = StateTimeline(reasons)
    timeline.consume(document["events"])
    windows = [timeline.window(window, unusable) for window in document["windows"]]
    return {"decision": "defer", "scope": "host scheduling experiment only",
            "collector_stop_reason": stop_reason,
            "completed_steps": sum(w["outcome"] == "completed" for w in windows
                                   if w.get("kind", "step") == "step"),
            "reasons": sorted(reasons), "windows": windows, "lifetimes": timeline.identities(),
            "phase_evidence": phase_evidence(windows)}


def phase_evidence(windows):
    evidence = {}
    for phase, state in PHASE_STATES.items():
        matching = [w for w in windows if w.get("kind") == "fixture_phase" and w.get("phase") == phase]
        evidence[phase] = {"expected_state": state, "windows": len(matching),
                           "observed_windows": sum(w["states_ns"] is not None
                                                   and w["states_ns"][state] > 0 for w in matching)}
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = analyze(read_json(args.input))
    with args.output.open("x") as output:
        json.dump(result, output, indent=2)
