"""Bounded, full-resolution queries over the Recording read seam."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import operator
import re
import struct
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import NoReturn, Sequence

from mcap.exceptions import McapError

from sil._schema_types import FORMATS, INT_RANGES, SIZES
from sil.recording import read_channels, read_records


class QueryError(ValueError):
    """A query or its source cannot be accepted."""


_COMPARISONS = {"eq": operator.eq, "ne": operator.ne, "lt": operator.lt,
                "le": operator.le, "gt": operator.gt, "ge": operator.ge}


@dataclass(frozen=True)
class Predicate:
    """Compare one scalar with a typed numeric literal; no expressions.

    cross-up: previous <= literal and current > literal.
    cross-down: previous >= literal and current < literal.
    The first sample never crosses; non-finite values break adjacency.
    """

    field: str
    operator: str
    value: int | float


class _PredicateScan:
    def __init__(self, predicate: Predicate, field: _Field, channel: str):
        if field.count is not None:
            raise QueryError("predicates require a scalar field or explicit array element")
        if predicate.operator not in (*_COMPARISONS, "cross-up", "cross-down"):
            raise QueryError(f"unknown predicate operator {predicate.operator!r}")
        literal = predicate.value
        if type(literal) not in (int, float) or (
                isinstance(literal, float) and not math.isfinite(literal)):
            raise QueryError("predicate literal must be a finite number")
        if field.type in INT_RANGES:
            low, high = INT_RANGES[field.type]
            if type(literal) is not int or not low <= literal <= high:
                raise QueryError(f"predicate literal must be a {field.type} integer")
        self.predicate = predicate
        self.field = field
        self.channel = channel
        self.previous: dict | None = None
        self.nonfinite = 0
        self.last_t: int | None = None
        self.crossing = predicate.operator.startswith("cross-")

    def inspect(self, data: bytes, t: int, index: int) -> tuple[bool, dict | None]:
        if self.crossing and self.last_t is not None and t < self.last_t:
            raise QueryError("crossings require nondecreasing publication times")
        self.last_t = t
        value = self.field.scalar(data)
        if not math.isfinite(value):
            self.nonfinite += 1
            self.previous = None
            return False, None
        predicate = self.predicate
        if not self.crossing:
            return _COMPARISONS[predicate.operator](value, predicate.value), None
        previous = self.previous
        self.previous = _locator(self.channel, self.field.name, t, index, value)
        if previous is None:
            return False, None
        matched = (previous["value"] <= predicate.value < value
                   if predicate.operator == "cross-up" else
                   previous["value"] >= predicate.value > value)
        return matched, previous if matched else None


def render(report: object) -> str:
    """Canonical report bytes are UTF-8 of this text, including its newline."""
    return json.dumps(report, ensure_ascii=True, allow_nan=False,
                      separators=(",", ":")) + "\n"


def _json_value(value):
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else (
            "Infinity" if value > 0 else "-Infinity")
    return value


@dataclass(frozen=True)
class _Field:
    name: str
    type: str
    offset: int
    count: int | None

    def scalar(self, data: bytes) -> int | float:
        return struct.unpack_from("<" + FORMATS[self.type], data, self.offset)[0]

    def decode(self, data: bytes):
        if self.count is None:
            return self.scalar(data)
        if self.type == "u8":
            return memoryview(data)[self.offset:self.offset + self.count].hex()
        size = SIZES[self.type]
        return [_json_value(struct.unpack_from("<" + FORMATS[self.type], data,
                                              self.offset + i * size)[0])
                for i in range(self.count)]


def _fields(spec: dict, selected: Sequence[str]) -> tuple[list[_Field], int]:
    layout = {}
    offset = 0
    for field in spec["fields"]:
        name, type_ = field["name"], field["type"]
        count = field.get("count")
        if (not isinstance(name, str) or not name or name in layout or type_ not in FORMATS
                or (count is not None and (type(count) is not int or count < 1))):
            raise QueryError("invalid SiL Schema field")
        layout[name] = _Field(name, type_, offset, count)
        offset += SIZES[type_] * (1 if count is None else count)
    plans = []
    for selector in selected:
        if selector in layout:
            plans.append(layout[selector])
            continue
        match = re.fullmatch(r"(.+)\[(0|[1-9][0-9]*)\]", selector, re.DOTALL)
        if match is None or match[1] not in layout:
            raise QueryError(f"unknown or invalid field {selector!r}")
        field = layout[match[1]]
        if match[2] is not None:
            index = int(match[2])
            if field.count is None or index >= field.count:
                raise QueryError(f"array index out of bounds for {selector!r}")
            field = _Field(selector, field.type,
                           field.offset + index * SIZES[field.type], None)
        plans.append(field)
    return plans, offset


def _locator(channel: str, field: str, t: int, index: int, value) -> dict:
    return {"channel": channel, "field": field, "t_ns": t,
            "message_index": index, "value": value}


class _Summary:
    def __init__(self, channel: str, field: str):
        self.row = {"channel": channel, "field": field, "count": 0,
                    "finite_count": 0, "nonfinite_count": 0,
                    "min": None, "max": None}

    def add(self, value, t: int, index: int) -> None:
        self.row["count"] += 1
        if isinstance(value, float) and not math.isfinite(value):
            self.row["nonfinite_count"] += 1
            return
        self.row["finite_count"] += 1
        for key in ("min", "max"):
            old = self.row[key]
            better = old is None or (value < old["value"] if key == "min"
                                      else value > old["value"])
            earlier = old is not None and value == old["value"] and (
                t, index) < (old["t_ns"], old["message_index"])
            if better or earlier:
                self.row[key] = _locator(self.row["channel"], self.row["field"],
                                         t, index, value)


class _Results:
    """Retain a prefix within row and byte budgets; keep counting omissions."""

    def __init__(self, report: dict, max_results: int, max_bytes: int):
        self.report = report
        self.max_results = max_results
        # Reserve room for counters and the first omission's locator. Final
        # serialization is checked exactly, including the report and newline.
        names = report["query"]["fields"]
        longest = max(names, key=lambda name: len(render(name).encode()))
        locator_room = len(render({"channel": report["query"]["channel"],
                                   "field": longest}).encode())
        self.remaining = max_bytes - len(render(report).encode()) - 512 - locator_room
        if self.remaining < 0:
            raise QueryError("query and report metadata exceed max_bytes")
        self.max_bytes = max_bytes

    def omit(self, location: dict, reason: str) -> None:
        output = self.report["output"]
        output["omitted"] += 1
        output["complete"] = False
        if output["first_omitted"] is None:
            output["first_omitted"] = {**location, "reason": reason}

    def accepting(self, location: dict) -> bool:
        if not self.report["output"]["complete"]:
            self.omit(location, self.report["output"]["first_omitted"]["reason"])
            return False
        if len(self.report["results"]) >= self.max_results:
            self.omit(location, "max_results")
            return False
        return True

    def add(self, row: dict, location: dict) -> None:
        size = len(render(row).encode())
        if size > self.remaining:
            self.omit(location, "max_bytes")
            return
        self.remaining -= size
        self.report["results"].append(row)

    def finish(self) -> dict:
        report = self.report
        report["status"] = ("incomplete" if not report["scan"]["complete"] else
                            "complete" if report["output"]["complete"] else "limited")
        report["output"]["returned"] = len(report["results"])
        for _ in range(8):
            size = len(render(report).encode())
            if size > self.max_bytes:
                raise QueryError("query and report metadata exceed max_bytes")
            if report["output"]["serialized_bytes"] == size:
                return report
            report["output"]["serialized_bytes"] = size
        raise QueryError("cannot size report")


class RecordingQuery:
    """A source session. Hashes the file once; refuses later source changes.

    Message indices are zero-based stored-order positions within the selected
    Channel and half-open interval. They are locators together with the source
    digest and the report's query, including the interval.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        try:
            self._stamp = self.path.stat()
            digest = hashlib.sha256()
            with self.path.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    digest.update(block)
            self._check_source()
            self._source = {"sha256": digest.hexdigest(), "size_bytes": self._stamp.st_size}
        except OSError as e:
            raise QueryError(f"cannot read Recording: {e}") from e

    @property
    def source(self) -> dict:
        """The content identity of this session, without exposing mutable state."""
        return dict(self._source)

    def _check_source(self) -> None:
        current = self.path.stat()
        keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(current, key) != getattr(self._stamp, key) for key in keys):
            raise QueryError("Recording source changed; create a new source session")

    def query(self, channel: str, fields: Sequence[str], *,
              start_ns: int | None = None, end_ns: int | None = None,
              mode: str = "rows", where: Predicate | None = None,
              max_results: int = 100, max_bytes: int = 65536,
              max_messages: int | None = None) -> dict:
        """Scan at full resolution, returning a bounded report.

        Output limits retain a prefix but continue scanning. max_messages
        stops inspection and marks partial results incomplete. No pagination
        or continuation is supported. render() defines the byte budget.
        """
        try:
            self._check_source()
            if type(max_results) is not int or max_results < 1:
                raise QueryError("max_results must be a positive integer")
            if type(max_bytes) is not int or max_bytes < 1024:
                raise QueryError("max_bytes must be an integer of at least 1024")
            if max_messages is not None and (
                    type(max_messages) is not int or max_messages < 1):
                raise QueryError("max_messages must be a positive integer")
            if not fields or len(set(fields)) != len(fields):
                raise QueryError("select at least one field, without duplicates")
            for bound in (start_ns, end_ns):
                if bound is not None and (type(bound) is not int or bound < 0):
                    raise QueryError("time bounds must be non-negative integer nanoseconds")
            if start_ns is not None and end_ns is not None and end_ns < start_ns:
                raise QueryError("end_ns must be at least start_ns")
            declared = {c.name: c for c in read_channels(self.path)}
            if channel not in declared:
                raise QueryError(f"unknown Channel {channel!r}")
            plans, size = _fields(declared[channel].schema, fields)
            predicate_scan = None
            if where is not None:
                predicate_fields, _ = _fields(declared[channel].schema, [where.field])
                predicate_scan = _PredicateScan(where, predicate_fields[0], channel)
            if mode not in ("rows", "summary"):
                raise QueryError("mode must be rows or summary")
            if mode == "summary" and any(p.count is not None for p in plans):
                raise QueryError("summaries require scalar fields or explicit array elements")
            summaries = [_Summary(channel, p.name) for p in plans]
            query = {"channel": channel, "fields": list(fields),
                     "start_ns": start_ns, "end_ns": end_ns, "mode": mode,
                     "where": None if where is None else asdict(where)}
            report = {"sil_recording_query": 1, "source": dict(self.source),
                      "query": query, "status": "complete", "results": [],
                      "scan": {"complete": True, "messages": 0, "matches": 0,
                               "predicate_nonfinite": 0, "first_t_ns": None,
                               "last_t_ns": None, "last_message_index": None,
                               "lookahead_messages": 0},
                      "query_sha256": hashlib.sha256(render(query).encode()).hexdigest(),
                      "limits": {"max_results": max_results, "max_bytes": max_bytes,
                                 "max_messages": max_messages},
                      "output": {"complete": True, "returned": 0, "omitted": 0,
                                 "first_omitted": None, "serialized_bytes": 0},
                      "continuation": None,
                      "crossing_context": None if not predicate_scan or
                      not predicate_scan.crossing else {
                          "outside_window": False, "first_sample": "no_crossing",
                          "nonfinite": "breaks_adjacency"}}
            results = _Results(report, max_results, max_bytes)
            for index, (_, t, data) in enumerate(read_records(
                    self.path, channels=[channel], start_ns=start_ns, end_ns=end_ns)):
                if max_messages is not None and index >= max_messages:
                    report["scan"]["complete"] = False
                    report["scan"]["lookahead_messages"] = 1
                    break
                if len(data) != size:
                    raise QueryError(f"Message {index} on {channel!r} does not match its Schema")
                report["scan"]["messages"] += 1
                scan = report["scan"]
                scan["first_t_ns"] = t if scan["first_t_ns"] is None else min(t, scan["first_t_ns"])
                scan["last_t_ns"] = t if scan["last_t_ns"] is None else max(t, scan["last_t_ns"])
                scan["last_message_index"] = index
                matched, previous = (True, None) if predicate_scan is None else (
                    predicate_scan.inspect(data, t, index))
                if not matched:
                    continue
                report["scan"]["matches"] += 1
                if mode == "summary":
                    for summary, plan in zip(summaries, plans):
                        summary.add(plan.scalar(data), t, index)
                else:
                    location = {"channel": channel, "t_ns": t, "message_index": index}
                    if not results.accepting(location):
                        continue
                    # A lower bound prevents constructing an enormous array
                    # or hex string merely to discover that it cannot fit.
                    if sum(p.count * 2 for p in plans if p.count is not None) > results.remaining:
                        results.omit(location, "oversized_result")
                        continue
                    row = {**location,
                           "fields": {p.name: _json_value(p.decode(data)) for p in plans}}
                    if previous is not None:
                        row["previous"] = previous
                    results.add(row, location)
            if predicate_scan is not None:
                report["scan"]["predicate_nonfinite"] = predicate_scan.nonfinite
            if mode == "summary":
                for summary in summaries:
                    location = {"channel": channel, "field": summary.row["field"]}
                    if results.accepting(location):
                        results.add(summary.row, location)
            self._check_source()
            return results.finish()
        except QueryError:
            raise
        except (OSError, ValueError, KeyError, TypeError, struct.error, McapError) as e:
            raise QueryError(f"cannot query Recording: {e}") from e


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise QueryError(message)


def _refused(arguments: list[str], source: RecordingQuery | None,
             error: QueryError, max_bytes: int) -> dict:
    report = {"sil_recording_query": 1, "status": "refused",
              "source": None if source is None else source.source,
              "request_sha256": hashlib.sha256(render(arguments).encode()).hexdigest(),
              "arguments": None, "scan": None, "results": [],
              "error": str(error)[:64],
              "output": {"complete": False, "serialized_bytes": 0}}
    if len(render(arguments).encode()) + len(render(report).encode()) + 16 <= max_bytes:
        report["arguments"] = arguments
    while True:
        size = len(render(report).encode())
        if report["output"]["serialized_bytes"] == size:
            return report
        report["output"]["serialized_bytes"] = size


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(prog="sil recording query", allow_abbrev=False,
                     description="Full-resolution field queries with a bounded JSON report.")
    parser.add_argument("recording", type=Path)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--field", action="append", required=True)
    parser.add_argument("--from", dest="start_ns", type=int)
    parser.add_argument("--to", dest="end_ns", type=int)
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--where", nargs=3, metavar=("FIELD", "OP", "JSON_NUMBER"))
    parser.add_argument("--max-results", type=int, default=100)
    parser.add_argument("--max-bytes", type=int, default=65536)
    parser.add_argument("--max-messages", type=int)
    arguments = sys.argv[1:] if argv is None else argv
    source = None
    max_bytes = 65536
    try:
        args = parser.parse_args(arguments)
        if args.max_bytes >= 1024:
            max_bytes = args.max_bytes
        where = None
        if args.where:
            try:
                literal = json.loads(args.where[2])
            except ValueError as e:
                raise QueryError("predicate literal must be a JSON number") from e
            where = Predicate(args.where[0], args.where[1], literal)
        source = RecordingQuery(args.recording)
        report = source.query(
            args.channel, args.field, start_ns=args.start_ns, end_ns=args.end_ns,
            mode="summary" if args.summary else "rows", where=where,
            max_results=args.max_results, max_bytes=args.max_bytes,
            max_messages=args.max_messages)
    except QueryError as e:
        report = _refused(arguments, source, e, max_bytes)
        sys.stdout.write(render(report))
        sys.stderr.write(f"sil recording query: error: {str(e)[:256]}\n")
        return 2
    sys.stdout.write(render(report))
    return {"complete": 0, "incomplete": 3, "limited": 4}[report["status"]]


if __name__ == "__main__":
    sys.exit(main())
