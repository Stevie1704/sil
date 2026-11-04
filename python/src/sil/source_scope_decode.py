"""Offline decoding of a source-scope capture (#371).

docs/proposals/native-source-scopes.md, "Files", "Join and pairing" and
"Identity checks", is the contract. The inputs are saved evidence only: the
raw `scopes/events.bin`, the runner's `activation/observations.jsonl` and
the `sil_source_scope_map` v1 document that the preflight wrote. `decode`
is pure. It returns the `sil_source_scope_calls` v1 document, or raises
CaptureFailed before any output when an identity check fails.

Pairing is per thread with one stack. A paired call belongs to an
Activation of the selected participant when both its edges are on the
runner thread inside that Activation's half-open interval.
"""

from __future__ import annotations

import bisect
import json
import uuid
from collections import Counter
from dataclasses import dataclass, field
import struct

CALLS_KEY = "sil_source_scope_calls"
MAP_KEY = "sil_source_scope_map"
OBSERVATIONS_KEY = "sil_activation_observations"
HEADER = struct.Struct("<8sHHHH16sIIQQQQQ3QQQB40s95x")
RECORD = struct.Struct("<QIHBB")
MAGIC, VERSION = b"SILSCOPE", 1
CREATED, ACTIVE, FINALIZED, UNAVAILABLE = 0, 1, 2, 3
ENTRY, RETURN = 0, 1
MAX_RECORDS = 262144
MAX_SITES = 4096
MAX_ROWS = 1000
# The first that applies is an unavailable Activation's reason.
ACTIVATION_REASONS = ("observations_incomplete", "events_lost",
                      "not_completed", "unbalanced_scopes", "crossing_call")
FAILURE_REASONS = ("observer_unavailable", "raw_incomplete", "raw_invalid",
                   "capture_mismatch", "identity_mismatch",
                   "clock_unverified", "map_invalid", "normalized_limit")


class CaptureFailed(Exception):
    """An identity check failed: the capture is `failed` with `reason`.
    `header` is the raw header when it could be read, for the loss
    counters of `scopes.json`."""

    def __init__(self, reason: str, detail: str,
                 header: RawHeader | None = None):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.header = header


@dataclass(frozen=True)
class RawHeader:
    state: int
    capture_id: str
    pid: int
    capacity: int
    records: int
    dropped_full: int
    dropped_contended: int
    clock_errors: int
    bracket: tuple[int, int, int]
    pid_namespace: dict
    build_id: str

    @property
    def lost(self) -> int:
        return self.dropped_full + self.dropped_contended + self.clock_errors


def read_header(events: bytes) -> RawHeader:
    """The raw header; CaptureFailed `raw_invalid` when it is not a
    version-1 header of a file of its own capacity."""
    if len(events) < HEADER.size:
        raise CaptureFailed("raw_invalid", "the file is shorter than its header")
    (magic, version, header_bytes, record_bytes, state, capture, pid, flags,
     capacity, records, dropped_full, dropped_contended, clock_errors,
     bracket_0, bracket_1, bracket_2, device, inode, build_id_bytes,
     build_id) = HEADER.unpack_from(events)
    if (magic, version, header_bytes, record_bytes, flags) != \
            (MAGIC, VERSION, HEADER.size, RECORD.size, 0) or state > UNAVAILABLE:
        raise CaptureFailed("raw_invalid", "not a version-1 raw header")
    header = RawHeader(
        state, str(uuid.UUID(bytes=capture)), pid, capacity, records,
        dropped_full, dropped_contended, clock_errors,
        (bracket_0, bracket_1, bracket_2),
        {"device": device, "inode": inode},
        build_id[:build_id_bytes].hex())
    if not 1 <= capacity <= MAX_RECORDS or build_id_bytes > len(build_id) \
            or len(events) != HEADER.size + RECORD.size * capacity \
            or records > capacity:
        raise CaptureFailed("raw_invalid", "the raw sizes do not agree",
                            header)
    return header


def decode(events: bytes, observations: str, scope_map: dict, *,
           participant: str, capture_id: str,
           library_sha256: str | None) -> dict:
    """The `sil_source_scope_calls` v1 document of a capture.

    `capture_id` is the session's. `library_sha256` is the SHA-256 of the
    file at the map's library path after the Run, or None when it cannot be
    read."""
    scopes = _check_map(scope_map)
    header = read_header(events)
    records = _records(events, header, scopes)
    if header.state == ACTIVE:
        raise CaptureFailed("raw_incomplete", "the runner did not finalize",
                            header)
    if header.state == UNAVAILABLE:
        raise CaptureFailed("observer_unavailable",
                            "the observer could not activate", header)
    stream = _Stream.parse(observations)
    _check_identities(header, stream, scope_map, capture_id, library_sha256)
    return _Join(header, records, stream, participant).document()


def _check_map(scope_map: dict) -> set[int]:
    try:
        entries = scope_map["scopes"]
        ids = [entry["id"] for entry in entries]
        sites = sum(len(entry["sites"]) for entry in entries)
        valid = (scope_map[MAP_KEY] == 1 and len(set(ids)) == len(ids)
                 and all(type(i) is int and 1 <= i <= 0xFFFF for i in ids)
                 and sites <= MAX_SITES)
    except (KeyError, TypeError):
        valid = False
    if not valid:
        raise CaptureFailed("map_invalid",
                            "the scope map is not a valid version-1 map")
    return set(ids)


def _records(events: bytes, header: RawHeader,
             scopes: set[int]) -> list[tuple[int, int, int, int]]:
    """(monotonic_ns, tid, scope, edge) of each kept record."""
    if header.state == CREATED and header.records:
        raise CaptureFailed("raw_invalid", "a created file has records",
                            header)
    records, last = [], {}
    for index in range(header.records):
        ns, tid, scope, edge, reserved = RECORD.unpack_from(
            events, HEADER.size + RECORD.size * index)
        if edge not in (ENTRY, RETURN) or reserved or scope not in scopes \
                or ns < last.get(tid, 0):
            raise CaptureFailed("raw_invalid", f"record {index} is not valid",
                                header)
        last[tid] = ns
        records.append((ns, tid, scope, edge))
    return records


def _check_identities(header: RawHeader, stream: _Stream, scope_map: dict,
                      capture_id: str, library_sha256: str | None) -> None:
    observed = stream.header
    if header.capture_id != capture_id or observed is None:
        raise CaptureFailed("capture_mismatch",
                            "the raw file or the observation stream is not "
                            "of this capture", header)
    activated = header.state == FINALIZED
    if activated and (header.pid != observed.get("runner_pid")
                      or header.pid_namespace != observed.get("pid_namespace")):
        raise CaptureFailed("capture_mismatch",
                            "the observer ran in another process", header)
    if activated and header.build_id != scope_map["library"]["build_id"] \
            or library_sha256 != scope_map["library"]["sha256"]:
        raise CaptureFailed("identity_mismatch",
                            "the library that ran is not the mapped build",
                            header)
    if observed.get("clock") != "host_monotonic" or (
            activated and list(header.bracket) != sorted(header.bracket)):
        raise CaptureFailed("clock_unverified",
                            "the clocks are not proven the same", header)


def _host(row: dict, key: str) -> int:
    value = row[key]
    if type(value) is not str or not value.isdigit():
        raise ValueError(f"{key} is not a decimal string")
    return int(value)


@dataclass
class _Stream:
    """The kept records of the observation stream. A line that does not
    decode cuts the stream there, as a failed write does."""

    header: dict | None = None
    rows: list[dict] = field(default_factory=list)
    cut: bool = False
    coverage_end: int = 0  # the last kept time; meaningful when cut

    @classmethod
    def parse(cls, text: str) -> _Stream:
        stream, end = cls(), None
        lines = [line for line in text.splitlines() if line.strip()]
        for number, line in enumerate(lines):
            try:
                row = json.loads(line)
                if number == 0:
                    if not isinstance(row, dict) or row.get(OBSERVATIONS_KEY) != 1:
                        return stream
                    stream.header = row
                    continue
                if row.get("kind") == "end":
                    end = row
                    break
                times = [_host(row, key) for key in ("begin_ns", "end_ns", "at_ns")
                         if key in row]
                if row.get("kind") == "activation" and len(times) != 2:
                    raise ValueError("an activation without an interval")
            except (ValueError, TypeError, AttributeError, KeyError):
                stream.cut = True
                break
            stream.rows.append(row)
            stream.coverage_end = max([stream.coverage_end, *times])
        stream.cut = stream.cut or end is None or bool(
            end.get("truncated") or end.get("write_failed") or end.get("dropped"))
        return stream


class _Intervals:
    """Disjoint half-open intervals, searchable by time."""

    def __init__(self, items: list[tuple[int, int, int]]):
        self._items = sorted(items)
        self._begins = [item[0] for item in self._items]

    def find(self, time_ns: int) -> int | None:
        index = bisect.bisect_right(self._begins, time_ns) - 1
        if index >= 0:
            begin, end, value = self._items[index]
            if begin <= time_ns < end:
                return value
        return None


@dataclass
class _Call:
    tid: int
    scope: int
    entry: tuple[int, int] | None  # (record index, monotonic_ns)
    ret: tuple[int, int] | None

    @property
    def order(self) -> int:
        return (self.entry or self.ret)[0]

    def row(self, reason: str) -> dict:
        return {"scope": self.scope, "tid": self.tid,
                "entry_ns": str(self.entry[1]) if self.entry else None,
                "return_ns": str(self.ret[1]) if self.ret else None,
                "reason": reason}


def _pair(records: list) -> tuple[list[_Call], list[_Call]]:
    """Paired calls and unpaired edges, by the per-thread stack rule."""
    stacks: dict[int, list[_Call]] = {}
    paired, unpaired = [], []
    for index, (ns, tid, scope, edge) in enumerate(records):
        stack = stacks.setdefault(tid, [])
        if edge == ENTRY:
            stack.append(_Call(tid, scope, (index, ns), None))
            continue
        depth = next((d for d in range(len(stack) - 1, -1, -1)
                      if stack[d].scope == scope), None)
        if depth is None:
            unpaired.append(_Call(tid, scope, None, (index, ns)))
            continue
        unpaired += stack[depth + 1:]
        call = stack[depth]
        del stack[depth:]
        call.ret = (index, ns)
        paired.append(call)
    for stack in stacks.values():
        unpaired += stack
    return paired, unpaired


class _Join:
    def __init__(self, header: RawHeader, records: list, stream: _Stream,
                 participant: str):
        self.header = header
        self.records = records
        self.stream = stream
        self.participant = participant
        self.runner_tid = stream.header.get("runner_tid")
        self.rows = [r for r in stream.rows if r.get("kind") == "activation"]
        self.selected = [i for i, r in enumerate(self.rows)
                         if r["participant"] == participant
                         and r.get("task_kind") == "native_task"]
        self.intervals = _Intervals([
            (_host(r, "begin_ns"), _host(r, "end_ns"), i)
            for i, r in enumerate(self.rows) if r.get("tid") == self.runner_tid])
        self.calls: dict[int, list[_Call]] = {i: [] for i in self.selected}
        self.flags: dict[int, set[str]] = {i: set() for i in self.selected}
        self.unattributed: list[tuple[_Call, str]] = []

    def document(self) -> dict:
        paired, unpaired = _pair(self.records)
        for call in paired:
            self._join(call)
        for call in unpaired:
            self._unpaired(call)
        problems = [reason for reason, found in (
            ("events_lost", self.header.lost),
            ("observations_incomplete", self.stream.cut),
            ("unbalanced_scopes", unpaired)) if found]
        activations = [self._activation(i) for i in self.selected]
        exact = [a for a in activations if a["status"] == "exact"]
        rows = [call.row(reason) for call, reason in
                sorted(self.unattributed, key=lambda item: item[0].order)]
        edges = Counter(edge for *_, edge in self.records)
        return {
            CALLS_KEY: 1,
            "capture_id": self.header.capture_id,
            "participant": self.participant,
            "status": "partial" if problems else "complete",
            "problems": problems,
            "totals": {
                "entries": edges[ENTRY], "returns": edges[RETURN],
                "paired_calls": len(paired),
                "attributed_calls": sum(len(a["calls"]) for a in exact),
                "withheld_calls": sum(a["withheld_calls"] for a in activations),
                "unattributed": len(rows)},
            "activations": activations,
            "unattributed": {
                "reasons": dict(Counter(row["reason"] for row in rows)),
                "rows": rows[:MAX_ROWS],
                "rows_remainder": max(0, len(rows) - MAX_ROWS)},
        }

    def _in_selected(self, ns: int) -> int | None:
        found = self.intervals.find(ns)
        return found if found in self.calls else None

    def _join(self, call: _Call) -> None:
        entry_ns, return_ns = call.entry[1], call.ret[1]
        if self.stream.cut and return_ns >= self.stream.coverage_end:
            return self._unattribute(call, "observations_incomplete")
        if call.tid != self.runner_tid:
            return self._unattribute(call, "worker_thread")
        at_entry = self.intervals.find(entry_ns)
        at_return = self.intervals.find(return_ns)
        if at_entry is not None and at_entry == at_return:
            if at_entry in self.calls:
                self.calls[at_entry].append(call)
                return None
            return self._unattribute(call, "other_participant")
        if at_entry is None and at_return is None:
            return self._unattribute(call, "observations_incomplete"
                                     if self.stream.cut else
                                     "outside_activations")
        # In a cut stream, an endpoint outside every kept interval can be
        # in a dropped one: the Activation is then not proven complete.
        reason = "observations_incomplete" if self.stream.cut and None in (
            at_entry, at_return) else "crossing_call"
        for index in (at_entry, at_return):
            if index in self.calls:
                self.flags[index].add(reason)
        return self._unattribute(call, "observations_incomplete"
                                 if reason == "observations_incomplete"
                                 else "crossing_interval")

    def _unpaired(self, call: _Call) -> None:
        edge = call.entry or call.ret
        if call.tid == self.runner_tid:
            index = self._in_selected(edge[1])
            if index is not None:
                self.flags[index].add("unbalanced_scopes")
        self._unattribute(call, "missing_return" if call.entry
                          else "missing_entry")

    def _unattribute(self, call: _Call, reason: str) -> None:
        self.unattributed.append((call, reason))

    def _activation(self, index: int) -> dict:
        row = self.rows[index]
        flags = set(self.flags[index])
        if self.header.lost:
            flags.add("events_lost")
        if row["outcome"] != "completed":
            flags.add("not_completed")
        reason = next((r for r in ACTIVATION_REASONS if r in flags), None)
        calls = sorted(self.calls[index], key=lambda call: call.order)
        record = {key: row[key] for key in (
            "participant", "task", "index", "virtual_time_ns", "begin_ns",
            "end_ns", "outcome")}
        if reason is not None:
            return {**record, "status": "unavailable", "reason": reason,
                    "withheld_calls": len(calls), "calls": [], "counts": None}
        counts = Counter(call.scope for call in calls)
        return {**record, "status": "exact", "reason": None,
                "withheld_calls": 0, "calls": _nest(calls),
                "counts": {str(scope): counts[scope] for scope in sorted(counts)}}


def _nest(calls: list[_Call]) -> list[dict]:
    """The calls of one thread in entry order, with depth and parent."""
    rows: list[dict] = []
    open_calls: list[tuple[int, int]] = []  # (return record index, row)
    for call in calls:
        while open_calls and open_calls[-1][0] < call.entry[0]:
            open_calls.pop()
        rows.append({"scope": call.scope, "depth": len(open_calls),
                     "parent": open_calls[-1][1] if open_calls else None,
                     "entry_ns": str(call.entry[1]),
                     "return_ns": str(call.ret[1]),
                     "elapsed_ns": str(call.ret[1] - call.entry[1])})
        open_calls.append((call.ret[0], len(rows) - 1))
    return rows
