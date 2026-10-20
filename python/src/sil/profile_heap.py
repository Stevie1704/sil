"""Normalization of a Heaptrack capture (#317).

`sil profile --collector heap` keeps Heaptrack's interpreted data file as
the raw evidence. This module reads that file directly: Heaptrack's
interpreted format, file version 3 (Heaptrack 1.4 and 1.5; see
src/util/linewriter.h, src/interpret/heaptrack_interpret.cpp and
src/analyze/accumulatedtracedata.cpp in Heaptrack). One record per line, a
mode character first, numbers in hexadecimal:

    v <heaptrack version> <file version>
    X <command line>                     the traced process
    s <length> <string>                  string n is the n-th s record
    i <ip> <module> [<function> <file> <line>]...
                                         the first frame is the innermost;
                                         later ones are the functions it is
                                         inlined into
    t <ip> <parent trace>                trace n is the n-th t record; 0 ends
    a <size> <trace>                     allocation info n (0-based)
    + <info>  /  - <info>                one allocation / one free
    c <ms>, R <pages>, I ..., A, #       timestamps, RSS and other records

docs/profile-report.md defines every metric and its boundary. The analysis
never sums inclusive values into a total, and it does not judge ownership or
leaks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from sil.profile_report import (
    HEAP_METRICS, HOTSPOT_LIMIT, SOURCE_LINES_PER_ROW,
)

FILE_VERSION = 3
# Each metric's top rows are kept; outstanding_allocations follows bytes.
RANKED = ("allocated_bytes", "allocations", "temporary_allocations",
          "outstanding_bytes")
IGNORED_MODES = frozenset(b"cRIA#")

BuildId = Callable[[str], tuple]  # path -> (build ID or None, reason)


class HeapDecodeError(ValueError):
    """The data is not Heaptrack's interpreted format, file version 3."""


@dataclass(frozen=True)
class HeapAnalysis:
    record: dict  # the `capture.analysis` member, without allocations_file
    allocations_document: dict  # heap/allocations.json


@dataclass
class _Info:
    size: int
    trace: int
    allocations: int = 0
    frees: int = 0
    temporary: int = 0


@dataclass
class _Data:
    file_version: int | None = None
    heaptrack_version: str | None = None
    executable: str | None = None
    strings: list = None
    ips: list = None  # (address, module string, [(function, file, line)])
    traces: list = None
    infos: list = None
    frees: int = 0
    peak: int = 0
    undecodable: int = 0
    truncated: bool = False

    def __post_init__(self):
        self.strings, self.ips, self.traces, self.infos = [], [], [], []

    def string(self, index: int) -> str | None:
        return self.strings[index - 1] if 0 < index <= len(self.strings) \
            else None


def _version_text(value: int) -> str:
    return f"{value >> 16}.{(value >> 8) & 0xff}.{value & 0xff}"


def _records(data: bytes):
    """(mode, rest) per record; a length-prefixed string can hold any
    byte. The last record without its newline is cut: (None, None)."""
    position, end = 0, len(data)
    while position < end:
        mode = data[position]
        if mode == ord("\n"):  # heaptrack_interpret ends with an empty line
            position += 1
            continue
        if mode == ord("s") and data.startswith(b"s ", position):
            space = data.find(b" ", position + 2)
            try:
                length = int(data[position + 2:space], 16)
            except ValueError:
                length = -1
            start = space + 1
            if space >= 0 and length >= 0 and start + length >= end or \
                    space < 0 and data.find(b"\n", position) < 0:
                yield None, None  # the data ends inside the string
                return
            if space < 0 or length < 0 or data[start + length] != ord("\n"):
                # A lost string moves every later string index.
                raise HeapDecodeError(
                    f"malformed string record at byte {position}")
            yield "s", data[start:start + length]
            position = start + length + 1
            continue
        newline = data.find(b"\n", position)
        if newline < 0:
            yield None, None
            return
        yield chr(mode), data[position + 2:newline]
        position = newline + 1


def _numbers(rest: bytes) -> list[int]:
    return [int(token, 16) for token in rest.split()]


def _parse(data: bytes) -> _Data:
    parsed = _Data()
    last_allocation = None  # the info of the record before, if a "+"
    live = 0
    for mode, rest in _records(data):
        if mode is None:
            parsed.truncated = True
            break
        if parsed.file_version is None:
            if mode != "v":
                raise HeapDecodeError("the data does not start with a "
                                      "version record")
            values = _numbers(rest)
            if len(values) < 2 or values[1] != FILE_VERSION:
                raise HeapDecodeError(
                    f"Heaptrack data file version {values[1:2] or '?'}; "
                    f"this version reads file version {FILE_VERSION}")
            parsed.file_version = values[1]
            parsed.heaptrack_version = _version_text(values[0])
            continue
        try:
            if mode == "s":
                parsed.strings.append(rest.decode(errors="replace"))
            elif mode == "X":
                parsed.executable = rest.decode(errors="replace")
            elif mode == "i":
                values = _numbers(rest)
                frames = [tuple(values[k:k + 3])
                          for k in range(2, len(values) - 2, 3)]
                parsed.ips.append((values[0], values[1], frames))
            elif mode == "t":
                ip, parent = _numbers(rest)
                parsed.traces.append((ip, parent))
            elif mode == "a":
                size, trace = _numbers(rest)
                parsed.infos.append(_Info(size, trace))
            elif mode in "+-":
                index, = _numbers(rest)
                info = parsed.infos[index]
                if mode == "+":
                    info.allocations += 1
                    live += info.size
                    parsed.peak = max(parsed.peak, live)
                    last_allocation = index
                    continue
                if info.frees >= info.allocations:
                    raise ValueError("a free without a live allocation")
                info.frees += 1
                parsed.frees += 1
                live -= info.size
                if last_allocation == index:
                    info.temporary += 1
                last_allocation = None
            elif ord(mode) not in IGNORED_MODES:
                parsed.undecodable += 1
        except (ValueError, IndexError):
            parsed.undecodable += 1
    if parsed.file_version is None:
        raise HeapDecodeError("the data has no version record")
    return parsed


class _Frames:
    """Frame and stack tables of allocations.json, and module identities."""

    def __init__(self, data: _Data, build_id: BuildId):
        self.data = data
        self.build_id = build_id
        self.frames: list[dict] = []
        self.keys: list[tuple] = []  # (module id, symbol) per frame
        self.sources: list = []
        self.by_ip: dict[int, list[int]] = {}
        self.stacks: list[list[int]] = []
        self.by_trace: dict[int, int] = {}
        self.modules: dict[str, dict] = {}

    def module(self, path: str | None) -> str | None:
        if path is None:
            return None
        if path not in self.modules:
            found, reason = self.build_id(path)
            unavailable = {} if found else {"build_id": reason}
            # The interpreter does not say which debug file it used.
            unavailable["debug"] = "not_reported_by_backend"
            self.modules[path] = {"path": path, "build_id": found,
                                  "debug": None, "unavailable": unavailable}
        return path

    def ip_frames(self, ip_index: int) -> list[int]:
        if ip_index not in self.by_ip:
            if not 0 < ip_index <= len(self.data.ips):
                raise HeapDecodeError(f"trace names unknown ip {ip_index}")
            address, module_string, frames = self.data.ips[ip_index - 1]
            module = self.module(self.data.string(module_string))
            indices = []
            for function, file, line in frames or [(0, 0, 0)]:
                source = self.data.string(file)
                self.frames.append({
                    "module": module, "symbol": self.data.string(function),
                    "address": f"0x{address:x}",
                    "source": {"file": source, "line": line}
                    if source and line else None})
                self.keys.append((module, self.data.string(function)))
                indices.append(len(self.frames) - 1)
            self.by_ip[ip_index] = indices
        return self.by_ip[ip_index]

    def stack(self, trace: int) -> int:
        if trace not in self.by_trace:
            frames, seen = [], set()
            index = trace
            while index:
                if index in seen or index > len(self.data.traces):
                    raise HeapDecodeError(f"trace {trace} does not end")
                seen.add(index)
                ip, index = self.data.traces[index - 1]
                frames += self.ip_frames(ip)
            self.stacks.append(frames)
            self.by_trace[trace] = len(self.stacks) - 1
        return self.by_trace[trace]


def _costs(info: _Info) -> dict:
    outstanding = info.allocations - info.frees
    return {"allocations": info.allocations,
            "allocated_bytes": info.allocations * info.size,
            "temporary_allocations": info.temporary,
            "outstanding_allocations": outstanding,
            "outstanding_bytes": outstanding * info.size}


def _add(target: dict, costs: dict) -> None:
    for key in HEAP_METRICS:
        target[key] = target.get(key, 0) + costs[key]


def _select(rows: dict, limit: int) -> list[dict]:
    """The union of each ranked metric's top `limit` rows."""
    def order(metric):
        return lambda item: (-item[1][metric], -item[1]["allocated_bytes"],
                             item[0][0] or "", item[0][1] or "")
    chosen = set()
    for metric in RANKED:
        ranked = sorted((item for item in rows.items() if item[1][metric]),
                        key=order(metric))
        chosen.update(key for key, _ in ranked[:limit])
    kept = sorted(((k, v) for k, v in rows.items() if k in chosen),
                  key=order("allocated_bytes"))
    return [value for _, value in kept]


def _row(key: tuple, module_ids: dict) -> dict:
    return {"module": module_ids.get(key[0]), "symbol": key[1],
            **{metric: 0 for metric in HEAP_METRICS}, "source_lines": {}}


def _finish_rows(rows: list[dict]) -> list[dict]:
    for row in rows:
        lines = sorted(row["source_lines"].items(),
                       key=lambda item: (-item[1], item[0]))
        row["source_lines"] = [{"file": file, "line": line,
                                "allocations": count}
                               for (file, line), count in
                               lines[:SOURCE_LINES_PER_ROW]]
    return rows


def normalize_heap(data: bytes, *, build_id: BuildId,
                   limit: int = HOTSPOT_LIMIT) -> HeapAnalysis:
    """The analysis of one interpreted Heaptrack data file. Raises
    HeapDecodeError when it is not file version 3 or its stacks do not
    resolve."""
    parsed = _parse(data)
    tables = _Frames(parsed, build_id)
    allocations = []
    stacks = []
    for info in parsed.infos:
        stack = tables.stack(info.trace)
        stacks.append(stack)
        allocations.append([stack, info.size, info.allocations,
                            info.temporary, info.allocations - info.frees])
    module_ids = {path: f"m{n}" for n, path in
                  enumerate(sorted(tables.modules), start=1)}
    totals = {metric: 0 for metric in HEAP_METRICS}
    self_rows: dict[tuple, dict] = {}
    inclusive_rows: dict[tuple, dict] = {}
    leaf_counts = {path: [0, 0] for path in tables.modules}
    unknown_module = unknown_symbol = with_caller = 0
    for info, stack in zip(parsed.infos, stacks):
        if not info.allocations:
            continue
        costs = _costs(info)
        _add(totals, costs)
        frames = tables.stacks[stack]
        if len(frames) > 1:
            with_caller += info.allocations
        leaf = tables.keys[frames[0]] if frames else (None, None)
        if leaf[0] is None:
            unknown_module += info.allocations
        else:
            leaf_counts[leaf[0]][0] += info.allocations
            if leaf[1] is None:
                unknown_symbol += info.allocations
                leaf_counts[leaf[0]][1] += info.allocations
        row = self_rows.setdefault(leaf, _row(leaf, module_ids))
        _add(row, costs)
        source = tables.frames[frames[0]]["source"] if frames else None
        if source:
            place = (source["file"], source["line"])
            row["source_lines"][place] = row["source_lines"].get(
                place, 0) + info.allocations
        for key in {tables.keys[frame] for frame in frames}:
            _add(inclusive_rows.setdefault(key, _row(key, module_ids)), costs)
    totals_record = {**{k: totals[k] for k in HEAP_METRICS[:2]},
                     "frees": parsed.frees,
                     **{k: totals[k] for k in HEAP_METRICS[2:]},
                     "peak_tracked_bytes": parsed.peak}
    kept = _finish_rows(_select(self_rows, limit))
    remainder = {metric: totals[metric] - sum(r[metric] for r in kept)
                 for metric in HEAP_METRICS}
    for frame in tables.frames:
        frame["module"] = module_ids.get(frame["module"])
    modules = [{"id": module_ids[path], **tables.modules[path],
                "allocations_leaf": leaf_counts[path][0],
                "allocations_unknown_symbol": leaf_counts[path][1]}
               for path in sorted(tables.modules)]
    record = {
        "file_version": parsed.file_version,
        "heaptrack_version": parsed.heaptrack_version,
        "traced_executable": parsed.executable,
        "totals": totals_record,
        "coverage": {"allocations_unknown_module": unknown_module,
                     "allocations_unknown_symbol": unknown_symbol,
                     "allocations_with_caller": with_caller,
                     "undecodable_records": parsed.undecodable,
                     "truncated": parsed.truncated},
        "modules": modules,
        "hotspots": {"limit": limit, "self": kept,
                     "self_remainder": remainder,
                     "inclusive": _finish_rows(_select(inclusive_rows,
                                                       limit))},
        "unavailable": {} if parsed.executable is not None else {
            "traced_executable": "not_reported_by_backend"},
    }
    document = {"sil_profile_heap_allocations": 1, "frames": tables.frames,
                "stacks": tables.stacks, "allocations": allocations}
    return HeapAnalysis(record, document)
