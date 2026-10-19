"""Normalization of a Linux perf CPU capture into the profile contract (#315).

The input is the text of two `perf script` passes over one `perf.data`
(docs/profile-report.md, "What the command does"):

- the records: `-F comm,pid,tid,time,period,ip,sym,dso --ns --no-inline
  --show-task-events --show-mmap-events --show-lost-events`;
- the leaf source lines: `-G -F ip,sym,srcline`, one pair of lines per
  sample, in the same order.

`normalize_cpu` is pure: facts about a module file (is it on the host or
retained, does a matching debug file exist) come from a callable. It returns
the members of a CPU capture and the `sil_profile_cpu_samples` document. It
never turns a sample into a duration or a call count: a weight is the
`cpu-clock` period that perf gives each sample.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Callable

from sil.profile_report import SOURCE_DEBUG_STATUSES

HOTSPOT_LIMIT = 50
SOURCE_LINES_PER_ROW = 5

# One record header: comm can contain spaces, so it is matched up to the
# rightmost "pid/tid time:" that follows it.
_HEADER = re.compile(
    r"^\s*(?P<comm>\S.*?)\s+(?P<pid>-?\d+)/(?P<tid>-?\d+)\s+"
    r"(?P<sec>\d+)\.(?P<nsec>\d{9}):\s?(?P<rest>.*)$")
_FRAME = re.compile(
    r"^\s*(?P<ip>[0-9a-f]+)\s+(?P<sym>.*?)\s+\((?P<dso>[^()]*)\)\s*$")
_PERIOD = re.compile(r"^\s*(?P<period>\d+)\s*(?P<frame>.*)$")
_TASK = re.compile(
    r"^PERF_RECORD_(?P<kind>FORK|EXIT)\((?P<pid>-?\d+):(?P<tid>-?\d+)\):"
    r"\((?P<ppid>-?\d+):(?P<ptid>-?\d+)\)")
_COMM = re.compile(
    r"^PERF_RECORD_COMM(?P<exec> exec)?: (?P<comm>.*):(?P<pid>-?\d+)/"
    r"(?P<tid>-?\d+)\s*$")
_MMAP = re.compile(
    r"^PERF_RECORD_MMAP2? (?P<pid>-?\d+)/(?P<tid>-?\d+): "
    r"\[(?P<start>0x[0-9a-f]+)\((?P<size>0x[0-9a-f]+)\) @ "
    r"(?P<pgoff>0x[0-9a-f]+|0)(?P<extra>[^\]]*)\]: (?P<prot>\S+) "
    r"(?P<path>.*)$")
_BUILD_ID = re.compile(r"<(?P<id>[0-9a-f]+)>")
_LOST = re.compile(r"^PERF_RECORD_LOST lost (?P<count>\d+)")
_SRCLINE = re.compile(r"^(?P<file>.*\S):(?P<line>\d+)$")
_IP_LINE = re.compile(r"^\s*(?P<ip>[0-9a-f]+)(\s|$)")


@dataclass(frozen=True)
class ModuleFacts:
    """What the command found about one module file after the Run."""

    material: str  # host_file, retained or unavailable
    debug_status: str  # one of profile_report.DEBUG_STATUSES
    debug_path: str | None


@dataclass(frozen=True)
class _Frame:
    ip: int
    symbol: str | None
    dso: str | None


@dataclass
class _Sample:
    time_ns: int
    process: "_Process"
    thread: "_Thread"
    weight_ns: int
    frames: tuple[_Frame, ...]
    source: tuple[str, int] | None = None


@dataclass
class _Thread:
    id: str
    process: "_Process"
    tid: int
    commands: list[str]
    samples: int = 0
    weight_ns: int = 0


@dataclass
class _Process:
    id: str
    pid: int
    parent: "_Process | None"
    commands: list[str]
    forked: bool
    start_ns: int
    end_ns: int
    execs: int = 0
    exited: bool = False
    samples: int = 0
    weight_ns: int = 0
    threads: list[_Thread] = field(default_factory=list)
    mappings: list[tuple[str, int, int, int]] = field(default_factory=list)


@dataclass
class _Capture:
    """Every record of the export, with lifetime identities."""

    origin_ns: int | None = None
    processes: list[_Process] = field(default_factory=list)
    threads: list[_Thread] = field(default_factory=list)
    samples: list[_Sample] = field(default_factory=list)
    build_ids: dict[str, str] = field(default_factory=dict)
    lost_samples: int = 0
    lost_records: int = 0
    _live_process: dict[int, _Process] = field(default_factory=dict)
    _live_thread: dict[int, _Thread] = field(default_factory=dict)

    def process(self, pid: int, comm: str, time_ns: int) -> _Process:
        process = self._live_process.get(pid)
        if process is None:
            process = self._new_process(pid, None, comm, time_ns,
                                        forked=False)
        process.end_ns = max(process.end_ns, time_ns)
        return process

    def thread(self, pid: int, tid: int, comm: str,
               time_ns: int) -> _Thread:
        process = self.process(pid, comm, time_ns)
        thread = self._live_thread.get(tid)
        if thread is None or thread.process is not process:
            thread = self._new_thread(process, tid, comm)
        return thread

    def _new_process(self, pid, parent, comm, time_ns, *, forked):
        process = _Process(f"p{len(self.processes) + 1}", pid, parent,
                           [comm], forked, time_ns, time_ns)
        self.processes.append(process)
        self._live_process[pid] = process
        return process

    def _new_thread(self, process: _Process, tid: int,
                    comm: str) -> _Thread:
        thread = _Thread(f"t{len(self.threads) + 1}", process, tid, [comm])
        self.threads.append(thread)
        process.threads.append(thread)
        self._live_thread[tid] = thread
        return thread

    def fork(self, pid, tid, ppid, comm, time_ns) -> None:
        if pid == ppid:  # a new thread of a running process
            process = self.process(pid, comm, time_ns)
            self._new_thread(process, tid, comm)
            return
        parent = self._live_process.get(ppid)
        # A fork is always a new lifetime, also when the kernel reused a PID.
        process = self._new_process(pid, parent, comm, time_ns, forked=True)
        self._new_thread(process, tid, comm)

    def exit(self, pid, tid, comm, time_ns) -> None:
        thread = self.thread(pid, tid, comm, time_ns)
        self._live_thread.pop(tid, None)
        if pid == tid:
            thread.process.exited = True
            self._live_process.pop(pid, None)

    def comm(self, pid, tid, header_comm, comm, exec_, time_ns) -> None:
        thread = self.thread(pid, tid, header_comm, time_ns)
        for owner in (thread, thread.process):
            if owner.commands[-1] != comm:
                owner.commands.append(comm)
        if exec_:
            thread.process.execs += 1


def _time_ns(match: re.Match) -> int:
    return int(match["sec"]) * 1_000_000_000 + int(match["nsec"])


def _frame(match: re.Match) -> _Frame:
    symbol = match["sym"]
    dso = match["dso"]
    return _Frame(int(match["ip"], 16),
                  None if symbol == "[unknown]" else symbol,
                  None if dso == "[unknown]" else dso)


def _parse(script: str) -> _Capture:
    capture = _Capture()
    sample: _Sample | None = None
    for line in script.splitlines():
        if not line.strip():
            continue
        header = _HEADER.match(line)
        if header is None:
            frame = _FRAME.match(line)
            if sample is not None and frame is not None:
                sample.frames += (_frame(frame),)
            continue
        sample = None
        time_ns = _time_ns(header)
        if capture.origin_ns is None:
            capture.origin_ns = time_ns
        sample = _record(capture, header, time_ns)
    return capture


def _record(capture: _Capture, header: re.Match,
            time_ns: int) -> _Sample | None:
    """Apply one record; return it when it is a sample."""
    comm, rest = header["comm"], header["rest"]
    pid, tid = int(header["pid"]), int(header["tid"])
    if rest.startswith("PERF_RECORD_"):
        if (task := _TASK.match(rest)) is not None:
            if task["kind"] == "FORK":
                capture.fork(int(task["pid"]), int(task["tid"]),
                             int(task["ppid"]), comm, time_ns)
            else:
                capture.exit(int(task["pid"]), int(task["tid"]), comm,
                             time_ns)
        elif (event := _COMM.match(rest)) is not None:
            capture.comm(int(event["pid"]), int(event["tid"]), comm,
                         event["comm"], event["exec"] is not None, time_ns)
        elif (event := _MMAP.match(rest)) is not None:
            _mmap(capture, event, comm, time_ns)
        elif (lost := _LOST.match(rest)) is not None:
            capture.lost_records += 1
            capture.lost_samples += int(lost["count"])
        return None
    period = _PERIOD.match(rest)
    if period is None:
        return None
    thread = capture.thread(pid, tid, comm, time_ns)
    frames: tuple[_Frame, ...] = ()
    if (inline := _FRAME.match(period["frame"])) is not None:
        frames = (_frame(inline),)
    sample = _Sample(time_ns, thread.process, thread, int(period["period"]),
                     frames)
    capture.samples.append(sample)
    return sample


def _mmap(capture: _Capture, event: re.Match, comm: str,
          time_ns: int) -> None:
    path = event["path"]
    build_id = _BUILD_ID.search(event["extra"])
    if build_id is not None:
        capture.build_ids.setdefault(path, build_id["id"])
    if "x" not in event["prot"]:
        return
    process = capture.process(int(event["pid"]), comm, time_ns)
    process.mappings.append((path, int(event["start"], 16),
                             int(event["size"], 16), int(event["pgoff"], 16)))


def _pair_srclines(text: str, samples: list[_Sample]) -> str | None:
    """Attach each leaf source line; return a problem, or None."""
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != 2 * len(samples):
        return "export_failed"
    pairs = []
    for sample, (ip_line, source_line) in zip(
            samples, zip(lines[0::2], lines[1::2])):
        ip = _IP_LINE.match(ip_line)
        leaf = sample.frames[0].ip if sample.frames else None
        if ip is None or int(ip["ip"], 16) != leaf:
            return "export_failed"
        source = _SRCLINE.match(source_line.strip())
        pairs.append(None if source is None or source["file"].startswith("??")
                     else (source["file"], int(source["line"])))
    for sample, source in zip(samples, pairs):
        sample.source = source
    return None


@dataclass
class _Row:
    self_samples: int = 0
    self_weight_ns: int = 0
    inclusive_samples: int = 0
    inclusive_weight_ns: int = 0
    lines: Counter = field(default_factory=Counter)


@dataclass(frozen=True)
class CpuAnalysis:
    """The normalized members of one CPU capture (docs/profile-report.md)."""

    time_origin_ns: str | None
    totals: dict
    symbolization: dict
    processes: list[dict]
    threads: list[dict]
    modules: list[dict]
    hotspots: dict
    samples_document: dict

    def module_id(self, path: str) -> str | None:
        return next((m["id"] for m in self.modules if m["path"] == path),
                    None)


def normalize_cpu(script: str, *, srclines: str | None,
                  facts: Callable[[str, str | None], ModuleFacts],
                  lost_sample_records: int,
                  srcline_problem: str | None = None,
                  hotspot_limit: int = HOTSPOT_LIMIT) -> CpuAnalysis:
    """The normalized capture of one `perf script` export.

    `srclines` is the second pass, or None with `srcline_problem` as the
    reason. `lost_sample_records` counts PERF_RECORD_LOST_SAMPLES records
    (`perf report --stats`); perf script prints no count for them, so any
    makes the lost-sample count unknown.
    """
    capture = _parse(script)
    if srclines is not None and capture.samples:
        srcline_problem = _pair_srclines(srclines, capture.samples)
    elif not capture.samples:
        srcline_problem = "no_samples"
    modules = _modules(capture, facts)
    by_path = {m["path"]: m for m in modules}
    for sample in capture.samples:
        sample.process.samples += 1
        sample.process.weight_ns += sample.weight_ns
        sample.thread.samples += 1
        sample.thread.weight_ns += sample.weight_ns
        leaf = sample.frames[0] if sample.frames else None
        module = by_path.get(leaf.dso) if leaf and leaf.dso else None
        if module is not None:
            module["samples_leaf"] += 1
            module["samples_unknown_symbol"] += leaf.symbol is None
        if sample.source and (module is None or module["debug"]["status"]
                              not in SOURCE_DEBUG_STATUSES):
            sample.source = None  # never a line from an unmatched file
    origin = capture.origin_ns
    return CpuAnalysis(
        time_origin_ns=None if origin is None else str(origin),
        totals=_totals(capture, lost_sample_records),
        symbolization=_symbolization(capture.samples, srcline_problem),
        processes=[_process_row(p, capture, origin)
                   for p in capture.processes],
        threads=[{"id": t.id, "process": t.process.id, "tid": t.tid,
                  "commands": t.commands, "samples": t.samples,
                  "weight_ns": t.weight_ns} for t in capture.threads],
        modules=modules,
        hotspots=_hotspots(capture.samples, by_path, hotspot_limit),
        samples_document=_samples_document(capture, by_path))


def _modules(capture: _Capture,
             facts: Callable[[str, str | None], ModuleFacts]) -> list[dict]:
    paths = {path for p in capture.processes for path, *_ in p.mappings}
    paths |= {f.dso for s in capture.samples for f in s.frames if f.dso}
    rows = []
    for index, path in enumerate(sorted(paths)):
        build_id = capture.build_ids.get(path)
        found = facts(path, build_id)
        rows.append({
            "id": f"m{index + 1}", "path": path, "build_id": build_id,
            "material": found.material,
            "debug": {"status": found.debug_status,
                      "path": found.debug_path},
            "mappings": [
                {"process": p.id, "start": hex(start), "size": hex(size),
                 "file_offset": hex(offset)}
                for p in capture.processes
                for mapped, start, size, offset in p.mappings
                if mapped == path],
            "samples_leaf": 0, "samples_unknown_symbol": 0,
            "unavailable": {} if build_id else {"build_id": "no_build_id"}})
    return rows


def _totals(capture: _Capture, lost_sample_records: int) -> dict:
    samples = capture.samples
    leaves = [s.frames[0] if s.frames else None for s in samples]
    unknown_count = lost_sample_records > 0
    return {
        "samples": len(samples),
        "weight_ns": sum(s.weight_ns for s in samples),
        "samples_unknown_module": sum(1 for f in leaves
                                      if f is None or f.dso is None),
        "samples_unknown_symbol": sum(1 for f in leaves
                                      if f is not None and f.dso is not None
                                      and f.symbol is None),
        "samples_with_caller": sum(1 for s in samples if len(s.frames) > 1),
        "lost_samples": None if unknown_count else capture.lost_samples,
        "lost_records": capture.lost_records + lost_sample_records,
        "unavailable": ({"lost_samples": "not_reported_by_backend"}
                        if unknown_count else {}),
    }


def _symbolization(samples: list[_Sample], problem: str | None) -> dict:
    named = sum(1 for s in samples if s.frames and s.frames[0].symbol)
    lines = sum(1 for s in samples if s.source)
    if problem is not None:
        source = {"status": "unavailable", "scope": "leaf_frames",
                  "samples_with_line": None,
                  "unavailable": {"samples_with_line": problem}}
    else:
        source = {"status": "available" if lines == len(samples)
                  else "partial" if lines else "unavailable",
                  "scope": "leaf_frames", "samples_with_line": lines,
                  "unavailable": {}}
        if lines == 0:
            source.update(samples_with_line=None, unavailable={
                "samples_with_line": "debug_file_not_found"})
    with_caller = sum(1 for s in samples if len(s.frames) > 1)
    return {"function_names": {"samples_with_name": named},
            "call_frame_unwinding": {"method": "dwarf",
                                     "samples_with_caller": with_caller},
            "source_lines": source,
            "inlined_frames": {"status": "not_requested"}}


def _offset(time_ns: int, origin: int | None) -> int | None:
    return None if origin is None else time_ns - origin


def _process_row(process: _Process, capture: _Capture,
                 origin: int | None) -> dict:
    if process.parent is not None:
        role = ("runner" if process.parent.parent is None
                and not process.parent.forked else "descendant")
    else:
        role = "launcher" if process is capture.processes[0] else "descendant"
    return {"id": process.id, "pid": process.pid,
            "parent": process.parent.id if process.parent else None,
            "role": role, "commands": process.commands,
            "execs": process.execs,
            "start_offset_ns": _offset(process.start_ns, origin),
            "end_offset_ns": _offset(process.end_ns, origin),
            "forked": process.forked, "exited": process.exited,
            "samples": process.samples, "weight_ns": process.weight_ns,
            "threads": len(process.threads)}


def _function(frame: _Frame, by_path: dict) -> tuple[str | None, str | None]:
    module = by_path.get(frame.dso) if frame.dso else None
    return (module["id"] if module else None, frame.symbol)


def _hotspots(samples: list[_Sample], by_path: dict, limit: int) -> dict:
    rows: dict[tuple, _Row] = defaultdict(_Row)
    for sample in samples:
        if not sample.frames:
            continue
        leaf = rows[_function(sample.frames[0], by_path)]
        leaf.self_samples += 1
        leaf.self_weight_ns += sample.weight_ns
        if sample.source:
            leaf.lines[sample.source] += 1
        # Once per sample, also when a function recurses.
        for key in {_function(f, by_path) for f in sample.frames}:
            rows[key].inclusive_samples += 1
            rows[key].inclusive_weight_ns += sample.weight_ns
    paths = {m["id"]: m["path"] for m in by_path.values()}

    def order(weight: str):
        def key(item):
            (module, symbol), row = item
            return (-getattr(row, weight), module is None,
                    paths.get(module, ""), symbol is None, symbol or "")
        return key

    leaves = sorted(((k, r) for k, r in rows.items() if r.self_samples),
                    key=order("self_weight_ns"))
    kept = leaves[:limit]
    return {
        "limit": limit,
        "self": [_hotspot_row(k, r) for k, r in kept],
        "self_remainder": {
            "samples": sum(r.self_samples for _, r in leaves[limit:]),
            "weight_ns": sum(r.self_weight_ns for _, r in leaves[limit:])},
        "inclusive": [_hotspot_row(k, r) for k, r in sorted(
            rows.items(), key=order("inclusive_weight_ns"))[:limit]],
    }


def _hotspot_row(key: tuple, row: _Row) -> dict:
    module, symbol = key
    lines = sorted(row.lines.items(), key=lambda item: (-item[1], item[0]))
    return {"module": module, "symbol": symbol,
            "self_samples": row.self_samples,
            "self_weight_ns": row.self_weight_ns,
            "inclusive_samples": row.inclusive_samples,
            "inclusive_weight_ns": row.inclusive_weight_ns,
            "source_lines": [{"file": f, "line": n, "samples": c}
                             for (f, n), c in lines[:SOURCE_LINES_PER_ROW]]}


def _module_offset(frame: _Frame, process: _Process) -> int | None:
    for path, start, size, offset in reversed(process.mappings):
        if path == frame.dso and start <= frame.ip < start + size:
            return frame.ip - start + offset
    return None


def _samples_document(capture: _Capture, by_path: dict) -> dict:
    frames: list[dict] = []
    frame_index: dict[tuple, int] = {}
    stacks: list[list[int]] = []
    stack_index: dict[tuple, int] = {}
    rows = []
    origin = capture.origin_ns
    for sample in capture.samples:
        indices = []
        for position, frame in enumerate(sample.frames):
            module = by_path.get(frame.dso) if frame.dso else None
            offset = _module_offset(frame, sample.process) if module else None
            source = sample.source if position == 0 else None
            record = {
                "module": module["id"] if module else None,
                "symbol": frame.symbol, "address": hex(frame.ip),
                "module_offset": None if offset is None else hex(offset),
                "source": ({"file": source[0], "line": source[1]}
                           if source else None)}
            key = (record["module"], record["module_offset"], record["symbol"],
                   record["address"] if offset is None else None,
                   source)
            if key not in frame_index:
                frame_index[key] = len(frames)
                frames.append(record)
            indices.append(frame_index[key])
        stack = tuple(indices)
        if stack not in stack_index:
            stack_index[stack] = len(stacks)
            stacks.append(list(stack))
        rows.append([sample.time_ns - origin, sample.process.id,
                     sample.thread.id, sample.weight_ns, stack_index[stack]])
    return {"sil_profile_cpu_samples": 1,
            "time_origin_ns": None if origin is None else str(origin),
            "frames": frames, "stacks": stacks, "samples": rows}
