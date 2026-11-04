"""Bind retained scheduler events and fixture markers to actual Process Steps."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

from evidence import PidNamespace, read_json, read_text
from report import analyze
from scope import spawn_lifetime_verified, scope_lifetimes

LINE = re.compile(r"\[(\d+)\]\s+(\d+)\.(\d{9}):\s+sched:(sched_\w+):\s+(.*)")


def parse_events(path):
    events, unknown = [], 0
    for line in read_text(path).splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        match = LINE.search(line)
        event = decode_event(match) if match else None
        if event is None:
            unknown += 1
        else:
            events.append(event)
    return events, unknown


def decode_event(match):
    cpu, seconds, fraction, name, payload = match.groups()
    row = {"cpu": int(cpu), "time_ns": int(seconds) * 10**9 + int(fraction)}
    fields = dict(re.findall(r"(\w+)=([^\s]+)", payload))
    if name == "sched_switch":
        return decode_switch(row, fields, payload)
    if name in ("sched_wakeup", "sched_wakeup_new"):
        return decode_wakeup(row, fields, payload)
    if name == "sched_process_fork":
        return {**row, "event": "fork", "parent_pid": int(fields["pid"]),
                "child_pid": int(fields["child_pid"])}
    if name == "sched_process_exec":
        return {**row, "event": "exec", "pid": int(fields["pid"]), "old_pid": int(fields["old_pid"])}
    if name == "sched_process_exit":
        return {**row, "event": "exit", "pid": int(fields["pid"])}
    return None


def decode_switch(row, fields, payload):
    if {"prev_pid", "next_pid", "prev_state"} <= fields.keys():
        prev, nxt, state = fields["prev_pid"], fields["next_pid"], fields["prev_state"]
    else:
        match = re.fullmatch(r".+:(\d+) \[\d+\] (\S+) ==> .+:(\d+) \[\d+\]", payload)
        if not match:
            return None
        prev, state, nxt = match.groups()
    return {**row, "event": "switch", "prev_pid": int(prev), "next_pid": int(nxt), "prev_state": state}


def decode_wakeup(row, fields, payload):
    if "pid" in fields:
        pid = fields["pid"]
    else:
        match = re.fullmatch(r".+:(\d+) \[\d+\] CPU:\d+", payload)
        if not match:
            return None
        pid = match[1]
    return {**row, "event": "wakeup", "pid": int(pid)}


def json_rows(path):
    return [json.loads(line) for line in read_text(path).splitlines()]


def loss_sources(out):
    warnings, stats = read_text(out / "events-errors.txt"), read_text(out / "stats.txt")
    sources = []
    if re.search(r"LOST(?:_SAMPLES)?(?: events)?\s*:\s*[1-9]\d*", stats):
        sources.append("perf_statistics")
    if re.search(r"lost\s+[1-9]\d*|[1-9]\d*\s+(?:lost|events lost)", warnings, re.I):
        sources.append("perf_script_warning")
    if "PERF_RECORD_LOST" in stats:
        sources.append("perf_lost_record")
    return sources


def event_clock_verified(out, header):
    attributes = "\n".join(line for line in read_text(out / "attributes.txt").splitlines()
                           if line.startswith("sched:"))
    return (header.get("clock") == "host_monotonic"
            and len(re.findall(r"\bclockid\s*:\s*1\b", attributes)) == 6
            and len(re.findall(r"use_clockid\s*:\s*1\b", attributes)) == 6)


class CaptureBinding:
    def __init__(self, out, result, namespace):
        self.result = result
        self.markers = json_rows(out / "markers.jsonl")
        observation_file = out / "activations.jsonl"
        self.observations = json_rows(observation_file) if observation_file.exists() else []
        headers = [row for row in self.observations if "sil_activation_observations" in row]
        self.header = headers[0] if len(headers) == 1 else {}
        self.namespace_ok = (self.header.get("pid_namespace") == namespace.as_record()
            and all(PidNamespace.from_pair(m["namespace"]) == namespace for m in self.markers)
            and result["initial_pid_namespace"])
        self.clock_ok = event_clock_verified(out, self.header)
        events, self.unparsed = parse_events(out / "events.txt")
        self.events = scope_lifetimes(events, self.header.get("runner_pid"), self.observations)
        self.loss = loss_sources(out)
        self.starts = {(m["participant"], m["index"], m["phase"], m["tid"]): m
                       for m in self.markers if m["edge"] == "begin"}

    def step_markers(self, participant, index, edge):
        return [m for m in self.markers if m["phase"] == "step" and m["edge"] == edge
                and m["participant"] == participant and m["index"] == index]

    def bind_steps(self):
        steps = []
        for row in self.observations:
            if row.get("kind") == "activation" and row.get("task_kind") == "process_step":
                markers = self.step_markers(row["participant"], row["index"], "begin")
                if len(markers) == 1:
                    steps.append(self.bind_step(row, markers[0]))
        return steps

    def bind_step(self, row, marker):
        ends = self.step_markers(marker["participant"], marker["index"], "end")
        begin, end = int(row["begin_ns"]), int(row["end_ns"])
        # Begin is a point in [begin,end); the end marker is an exclusive boundary.
        if not begin <= marker["host_ns"] < end or marker["virtual_ns"] != row["virtual_time_ns"]:
            self.clock_ok = False
        if ends and not marker["host_ns"] <= ends[0]["host_ns"] <= end:
            self.clock_ok = False
        if not self.spawn_verified(marker):
            self.namespace_ok = False
        return {"kind": "step", "participant": row["participant"], "index": row["index"],
                "virtual_ns": row["virtual_time_ns"], "tid": marker["tid"],
                "begin_ns": begin, "end_ns": end, "outcome": row["outcome"]}

    def spawn_verified(self, marker):
        spawns = [r for r in self.observations if r.get("kind") == "spawn"
                  and r["participant"] == marker["participant"] and r["pid"] == marker["pid"]]
        return len(spawns) == 1 and spawn_lifetime_verified(self.events, marker, self.header.get("runner_pid"),
                                                           int(spawns[0]["at_ns"]))

    def fixture_windows(self, steps):
        windows = []
        for marker in self.markers:
            if marker["edge"] != "end" or marker["phase"] in ("step", "lifetime"):
                continue
            start = self.starts[(marker["participant"], marker["index"], marker["phase"], marker["tid"])]
            window = {"kind": "fixture_phase", "phase": marker["phase"],
                      "participant": marker["participant"], "index": marker["index"],
                      "virtual_ns": marker["virtual_ns"], "tid": marker["tid"],
                      "begin_ns": start["host_ns"], "end_ns": marker["host_ns"], "outcome": "fixture_closed"}
            windows.append(window)
            if marker["phase"] in ("background", "wait_child", "competitor"):
                windows.extend(associated_thread_windows(window, steps))
        return windows

    def absent_intervals(self, steps):
        absent = []
        for marker in self.markers:
            if marker["phase"] != "step" or marker["edge"] != "begin":
                continue
            if any(w["participant"] == marker["participant"] and w["index"] == marker["index"] for w in steps):
                continue
            closed = bool(self.step_markers(marker["participant"], marker["index"], "end"))
            unclosed = self.result["run"]["exit_code"] == -9 and not closed
            absent.append({"kind": "step", "participant": marker["participant"],
                "index": marker["index"], "virtual_ns": marker["virtual_ns"], "tid": marker["tid"],
                "begin_ns": marker["host_ns"], "end_ns": None,
                "outcome": "unclosed" if unclosed else "interval_absent",
                "interval_source": "fixture_work_only; runner interval absent"})
        return absent

    def document(self):
        steps = self.bind_steps()
        windows = steps + self.fixture_windows(steps) + self.absent_intervals(steps)
        return {"clock": "host_monotonic" if self.clock_ok else "unknown",
                "namespace_verified": self.namespace_ok, "events": self.events, "windows": windows,
                "collector_stop_reason": self.result.get("collector_stop_reason"), "loss": bool(self.loss),
                "truncated": self.unparsed > 0 or any(self.result["exports"][key]["exit_code"] != 0
                                                      for key in ("events", "attributes", "stats"))}


def associated_thread_windows(window, steps):
    associated = []
    for step in steps:
        begin, end = max(step["begin_ns"], window["begin_ns"]), min(step["end_ns"], window["end_ns"])
        if begin < end and step["participant"] == window["participant"]:
            associated.append({**step, "kind": "worker_window_association", "phase": window["phase"],
                               "tid": window["tid"], "begin_ns": begin, "end_ns": end})
    return associated


def bind_capture(out: Path, result: dict, namespace: PidNamespace):
    binding = CaptureBinding(out, result, namespace)
    document = binding.document()
    analysis = analyze(document)
    analysis.update(loss_sources=binding.loss, unparsed_lines=binding.unparsed,
                    clock_verified=binding.clock_ok, namespace_verified=binding.namespace_ok,
                    activation_footer=next((r for r in binding.observations if r.get("kind") == "end"), None))
    return document, analysis


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    identity = read_json(args.capture.parent / "identity.json")
    document, analysis = bind_capture(args.capture, read_json(args.capture / "result.json"),
                                      PidNamespace.from_pair(identity["pid_namespace"]))
    with args.output.open("x") as stream:
        json.dump({"input": document, "analysis": analysis}, stream, indent=2)
