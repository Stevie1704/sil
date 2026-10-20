"""The Activation join of `sil profile --activations` (#316).

docs/profile-report.md, "Activation correlation", is the contract. The
inputs are saved evidence only: the runner's observation stream
(`activation/observations.jsonl`), the CPU capture members of
`profile.json` and the normalized samples. `correlate` is pure.

The join needs a proven common clock and PID namespace. Then each sample is
in exactly one class, by the rules of spec #313:

- `execution_context`: a sample of the runner thread inside a Native
  activation or a Process Step; synchronous routing and Recording work is
  part of it;
- `window_associated`: a sample of a Process participant's process tree
  inside one of its Steps; not proven to be the Step's own work;
- `lifecycle`, `replay`: the runner thread in a lifecycle phase or in a
  Replay publication Slot, outside an Activation;
- `unattributed`, with a reason: everything else.

Identity comes from the runner thread and from verified process lifetimes,
never from a symbol name. Intervals are half open, [begin, end).
"""

from __future__ import annotations

import bisect
import json
from dataclasses import dataclass, field

OBSERVATIONS_KEY = "sil_activation_observations"
CORRELATION_KEY = "sil_profile_activation_correlation"
CLASSES = ("execution_context", "window_associated", "lifecycle", "replay",
           "unattributed")
PHASES = ("preflight", "setup", "scheduled_execution", "shutdown",
          "recording_finalization")
CORRELATION_REASONS = {
    "observations_missing", "observations_corrupt", "observations_truncated",
    "observations_write_failed", "observations_dropped",
    "observations_unterminated", "clock_mismatch", "runner_not_observed",
    "pid_namespace_mismatch", "pid_namespace_unverified", "no_samples_file",
    "process_unmapped",
}
UNATTRIBUTED_REASONS = (
    "process_outside_step", "runner_other_thread", "runner_outside_intervals",
    "unmapped_process", "launcher", "observations_incomplete")
# Reasons that leave no summary: the join is not proven.
_UNAVAILABLE = {"observations_missing", "clock_mismatch",
                "runner_not_observed", "pid_namespace_mismatch",
                "pid_namespace_unverified", "no_samples_file"}


@dataclass(frozen=True)
class Correlation:
    record: dict  # the `activation_correlation` member of profile.json
    document: dict | None  # activation/correlation.json, or None


@dataclass
class _Stream:
    header: dict | None = None
    end: dict | None = None
    rows: list[dict] = field(default_factory=list)
    undecodable: int = 0


def _parse(text: str) -> _Stream:
    parsed = _Stream()
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            parsed.undecodable += 1
            continue
        if not isinstance(row, dict):
            parsed.undecodable += 1
        elif parsed.header is None and OBSERVATIONS_KEY in row:
            parsed.header = row
        elif row.get("kind") == "end":
            parsed.end = row
        else:
            parsed.rows.append(row)
    return parsed


def _host(row: dict, key: str) -> int:
    value = row[key]
    if type(value) is not str or not value.isdigit():
        raise ValueError(f"{key} is not a decimal string")
    return int(value)


def _times(row: dict) -> list[int]:
    if "at_ns" in row:
        return [_host(row, "at_ns")]
    return [_host(row, "begin_ns"), _host(row, "end_ns")]


def _unavailable(reason: str, observations: str | None) -> Correlation:
    unavailable = {"clock": reason, "summary": reason}
    if observations is None:
        unavailable["observations"] = reason
    return Correlation(
        {"kind": "activation", "status": "unavailable", "reasons": [reason],
         "clock": None, "observations": observations, "summary": None,
         "unavailable": unavailable}, None)


def _failed(reason: str, observations: str) -> Correlation:
    return Correlation(
        {"kind": "activation", "status": "failed", "reasons": [reason],
         "clock": None, "observations": observations, "summary": None,
         "unavailable": {"clock": reason, "summary": reason}}, None)


def correlate(observations: str | None, capture: dict, samples: dict | None,
              *, pid_namespace: dict | None,
              observations_path: str = "activation/observations.jsonl",
              correlation_path: str = "activation/correlation.json"
              ) -> Correlation:
    """The `activation_correlation` member and, when the join is proven,
    the per-Activation and per-sample document."""
    if observations is None:
        return _unavailable("observations_missing", None)
    stream = _parse(observations)
    header = stream.header
    if header is None or header.get(OBSERVATIONS_KEY) != 1:
        return _failed("observations_corrupt", observations_path)
    if stream.undecodable and not (stream.end or {}).get("write_failed") \
            and stream.end is not None:
        return _failed("observations_corrupt", observations_path)
    try:
        times = [t for row in stream.rows for t in _times(row)]
    except (KeyError, ValueError):
        return _failed("observations_corrupt", observations_path)

    problem = _join_problem(header, capture, samples, times, pid_namespace)
    if problem is not None:
        return _unavailable(problem, observations_path)
    join = _Join(stream, capture, samples)
    summary, document = join.run()
    reasons = _stream_reasons(stream)
    if any(p["mapping"] != "verified" for p in summary["processes"]):
        reasons.append("process_unmapped")
    summary["correlation_file"] = correlation_path
    return Correlation(
        {"kind": "activation",
         "status": "partial" if reasons else "complete",
         "reasons": reasons, "clock": "host_monotonic",
         "observations": observations_path, "summary": summary,
         "unavailable": {}}, document)


def _stream_reasons(stream: _Stream) -> list[str]:
    end = stream.end
    if end is None:
        return ["observations_unterminated"]
    if end.get("write_failed"):
        return ["observations_write_failed"]
    if end.get("truncated"):
        return ["observations_truncated"]
    if end.get("dropped"):
        return ["observations_dropped"]
    return []


def _runner(capture: dict) -> dict | None:
    return next((p for p in capture["processes"] if p["role"] == "runner"),
                None)


def _join_problem(header: dict, capture: dict, samples: dict | None,
                  times: list[int], pid_namespace: dict | None) -> str | None:
    if header.get("clock") != "host_monotonic" or \
            capture["sampling"]["clock"] != "host_monotonic":
        return "clock_mismatch"
    if header.get("pid_namespace") is None or pid_namespace is None:
        return "pid_namespace_unverified"
    if header["pid_namespace"] != pid_namespace:
        return "pid_namespace_mismatch"
    runner = _runner(capture)
    if runner is None or runner["pid"] != header.get("runner_pid") or \
            capture["time_origin_ns"] is None and capture["totals"]["samples"]:
        return "runner_not_observed"
    if capture["totals"]["samples"] and samples is None:
        return "no_samples_file"
    if capture["time_origin_ns"] is not None and times:
        origin = int(capture["time_origin_ns"])
        # Every observation lies in the runner's lifetime as perf saw it:
        # the two clocks are the same clock.
        if runner["start_offset_ns"] is not None and \
                min(times) < origin + runner["start_offset_ns"]:
            return "clock_mismatch"
        if runner["exited"] and runner["end_offset_ns"] is not None and \
                max(times) > origin + runner["end_offset_ns"]:
            return "clock_mismatch"
    return None


class _Intervals:
    """Disjoint half-open intervals, searchable by time."""

    def __init__(self, items: list[tuple[int, int, object]]):
        self._items = sorted(items, key=lambda item: item[0])
        self._begins = [item[0] for item in self._items]

    def find(self, time_ns: int):
        index = bisect.bisect_right(self._begins, time_ns) - 1
        if index >= 0:
            begin, end, value = self._items[index]
            if begin <= time_ns < end:
                return value
        return None


def _counter() -> dict:
    return {"samples": 0, "weight_ns": 0}


def _add(counter: dict, weight: int) -> None:
    counter["samples"] += 1
    counter["weight_ns"] += weight


class _Join:
    def __init__(self, stream: _Stream, capture: dict, samples: dict | None):
        self.stream = stream
        self.capture = capture
        self.samples = samples["samples"] if samples else []
        self.origin = int(capture["time_origin_ns"] or 0)
        self.runner = _runner(capture)
        self.runner_tid = stream.header.get("runner_tid")
        self.activations: list[dict] = []
        self.replays: list[dict] = []
        self.phases: list[tuple[str, int, int]] = []
        for row in stream.rows:
            if row.get("kind") == "activation":
                self.activations.append(row)
            elif row.get("kind") == "replay":
                self.replays.append(row)
            elif row.get("kind") == "phase":
                self.phases.append((row["phase"], _host(row, "begin_ns"),
                                    _host(row, "end_ns")))
        times = [t for row in stream.rows for t in _times(row)]
        complete = not _stream_reasons(stream)
        # Past the last kept record, a dropped record can hold any sample.
        self.coverage_end = None if complete else max(times, default=0)

    def run(self) -> tuple[dict, dict]:
        mapping = self._map_processes()
        owner = self._owners(mapping)
        tasks = self._tasks()
        activation_rows = [self._activation_row(a) for a in self.activations]
        on_runner = _Intervals([
            (_host(a, "begin_ns"), _host(a, "end_ns"), ("activation", i))
            for i, a in enumerate(self.activations)
            if a.get("tid") == self.runner_tid]
            + [(_host(r, "begin_ns"), _host(r, "end_ns"), ("replay", i))
               for i, r in enumerate(self.replays)])
        phases = _Intervals([(b, e, name) for name, b, e in self.phases])
        steps: dict[str, _Intervals] = {}
        for i, a in enumerate(self.activations):
            if a.get("task_kind") == "process_step":
                steps.setdefault(a["participant"], []).append(
                    (_host(a, "begin_ns"), _host(a, "end_ns"), i))
        windows = {name: _Intervals(items) for name, items in steps.items()}
        threads = {t["id"]: t for t in self.capture["threads"]}
        roles = {p["id"]: p["role"] for p in self.capture["processes"]}

        classes = {name: _counter() for name in CLASSES}
        unattributed = {reason: _counter() for reason in UNATTRIBUTED_REASONS}
        lifecycle = {name: _counter() for name, _, _ in self.phases}
        replay_counts = [_counter() for _ in self.replays]
        contexts = []
        for offset, process, thread, weight, _stack in self.samples:
            time_ns = self.origin + offset
            context = self._classify(
                time_ns, process, threads.get(thread), roles, owner,
                on_runner, phases, windows)
            kind, ref = context
            _add(classes[kind], weight)
            if kind == "unattributed":
                _add(unattributed[ref], weight)
            elif kind == "lifecycle":
                _add(lifecycle[ref], weight)
            elif kind == "replay":
                _add(replay_counts[ref], weight)
            else:
                row = activation_rows[ref]
                row["samples"] += 1
                row["weight_ns"] += weight
                task = tasks[(row["participant"], row["task"])]
                _add(task[kind], weight)
            contexts.append([kind, ref])

        for row in activation_rows:
            task = tasks[(row["participant"], row["task"])]
            task["with_samples" if row["samples"] else "without_samples"] += 1
        classes["unattributed"]["reasons"] = [
            {"reason": reason, **counts}
            for reason, counts in unattributed.items() if counts["samples"]]
        total = {"samples": len(self.samples),
                 "weight_ns": sum(row[3] for row in self.samples)}
        end = self.stream.end or {}
        summary = {
            "interval": "half_open",
            "stream": {
                "records": len(self.stream.rows),
                "dropped": end.get("dropped"),
                "truncated": bool(end.get("truncated")),
                "write_failed": bool(end.get("write_failed")),
                "terminated": self.stream.end is not None,
                "limit_bytes": self.stream.header.get("limit_bytes"),
                "undecodable_lines": self.stream.undecodable,
                "unavailable": {} if self.stream.end is not None else {
                    "dropped": "observations_unterminated"}},
            "intervals": {
                "activations": sum(1 for a in self.activations
                                   if a.get("task_kind") == "native_task"),
                "process_steps": sum(1 for a in self.activations
                                     if a.get("task_kind") == "process_step"),
                "replay": len(self.replays),
                "phases": len(self.phases)},
            "processes": mapping,
            "samples": {"total": total, **classes},
            "tasks": list(tasks.values()),
            "lifecycle": [{"phase": name, **counts}
                          for name, counts in lifecycle.items()],
        }
        document = {
            CORRELATION_KEY: 1,
            "activations": activation_rows,
            "replays": [{"participant": r["participant"], "index": r["index"],
                         "virtual_time_ns": r["virtual_time_ns"],
                         "begin_ns": r["begin_ns"], "end_ns": r["end_ns"],
                         "outcome": r["outcome"], **counts}
                        for r, counts in zip(self.replays, replay_counts)],
            "phases": [{"phase": name, "begin_ns": str(b), "end_ns": str(e)}
                       for name, b, e in self.phases],
            "samples": contexts,
        }
        return summary, document

    def _classify(self, time_ns, process, thread, roles, owner, on_runner,
                  phases, windows) -> tuple[str, object]:
        role = roles.get(process)
        if role == "launcher":
            return "unattributed", "launcher"
        runner_thread = (role == "runner" and thread is not None
                         and thread["tid"] == self.runner_tid)
        if role == "runner" and not runner_thread:
            return "unattributed", "runner_other_thread"
        if self.coverage_end is not None and time_ns >= self.coverage_end:
            return "unattributed", "observations_incomplete"
        if runner_thread:
            found = on_runner.find(time_ns)
            if found is not None:
                kind, index = found
                return ("execution_context" if kind == "activation"
                        else "replay"), index
            name = phases.find(time_ns)
            if name is not None:
                return "lifecycle", name
            if self.coverage_end is not None:
                # A record is written at its end: a dropped interval can
                # begin before the last kept record.
                return "unattributed", "observations_incomplete"
            return "unattributed", "runner_outside_intervals"
        participant = owner.get(process)
        if participant is None:
            return "unattributed", "unmapped_process"
        window = windows.get(participant)
        index = window.find(time_ns) if window else None
        if index is None:
            return "unattributed", "process_outside_step"
        return "window_associated", index

    def _map_processes(self) -> list[dict]:
        """Each spawn record to the one perf process the runner forked with
        that PID within the participant's spawn-to-reap lifetime."""
        reaped = {(r["participant"], r["pid"]): _host(r, "at_ns")
                  for r in self.stream.rows if r.get("kind") == "reap"}
        runner_id = self.runner["id"]
        rows = []
        for spawn in (r for r in self.stream.rows if r.get("kind") == "spawn"):
            start = _host(spawn, "at_ns")
            stop = reaped.get((spawn["participant"], spawn["pid"]))
            candidates = [
                p for p in self.capture["processes"]
                if p["pid"] == spawn["pid"] and p["parent"] == runner_id
                and p["forked"] and p["start_offset_ns"] is not None
                and start <= self.origin + p["start_offset_ns"]
                and (stop is None
                     or self.origin + p["start_offset_ns"] <= stop)]
            mapping = ("verified" if len(candidates) == 1 else
                       "not_observed" if not candidates else "ambiguous")
            rows.append({"participant": spawn["participant"],
                         "pid": spawn["pid"],
                         "process": candidates[0]["id"]
                         if len(candidates) == 1 else None,
                         "mapping": mapping})
        return rows

    def _owners(self, mapping: list[dict]) -> dict[str, str]:
        """Each perf process in a verified participant's process tree."""
        roots = {row["process"]: row["participant"] for row in mapping
                 if row["process"] is not None}
        parents = {p["id"]: p["parent"] for p in self.capture["processes"]}
        owners = {}
        for process in parents:
            node, seen = process, set()
            while node is not None and node not in roots and node not in seen:
                seen.add(node)
                node = parents.get(node)
            if node in roots:
                owners[process] = roots[node]
        return owners

    def _tasks(self) -> dict[tuple, dict]:
        tasks: dict[tuple, dict] = {}
        for a in self.activations:
            key = (a["participant"], a["task"])
            task = tasks.setdefault(key, {
                "participant": a["participant"], "task": a["task"],
                "kind": a["task_kind"], "activations": 0, "completed": 0,
                "failed": 0, "interrupted": 0, "with_samples": 0,
                "without_samples": 0,
                "execution_context": _counter(),
                "window_associated": _counter()})
            task["activations"] += 1
            task[a["outcome"]] += 1
        return tasks

    @staticmethod
    def _activation_row(a: dict) -> dict:
        return {"participant": a["participant"], "task": a["task"],
                "kind": a["task_kind"], "index": a["index"],
                "virtual_time_ns": a["virtual_time_ns"],
                "begin_ns": a["begin_ns"], "end_ns": a["end_ns"],
                "outcome": a["outcome"], "samples": 0, "weight_ns": 0}
