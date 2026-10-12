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
from contextlib import closing
from pathlib import Path
from typing import Generator, Iterable, NoReturn, Sequence

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
    The first selected Message never crosses; non-finite values break adjacency.
    """

    field: str
    operator: str
    value: int | float


@dataclass(frozen=True)
class _Match:
    current: dict
    previous: dict | None = None


class _PredicateScan:
    def __init__(self, predicate: Predicate, field: _Field, channel: str):
        if field.count is not None:
            raise QueryError("predicates require a scalar field or explicit array element")
        if not isinstance(predicate.operator, str) or predicate.operator not in (
                *_COMPARISONS, "cross-up", "cross-down"):
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

    def match(self, data: bytes, t: int, index: int) -> _Match | None:
        if self.crossing and self.last_t is not None and t < self.last_t:
            raise QueryError("crossings require nondecreasing publication times")
        self.last_t = t
        value = self.field.scalar(data)
        if not math.isfinite(value):
            self.nonfinite += 1
            self.previous = None
            return None
        predicate = self.predicate
        if not self.crossing:
            if _COMPARISONS[predicate.operator](value, predicate.value):
                return _Match(_locator(self.channel, self.field.name, t, index, value))
            return None
        previous = self.previous
        self.previous = _locator(self.channel, self.field.name, t, index, value)
        if previous is None:
            return None
        matched = (previous["value"] <= predicate.value < value
                   if predicate.operator == "cross-up" else
                   previous["value"] >= predicate.value > value)
        return _Match(self.previous, previous) if matched else None


def render(report: object) -> str:
    """Canonical report bytes are UTF-8 of this text, including its newline."""
    return json.dumps(report, ensure_ascii=True, allow_nan=False,
                      separators=(",", ":")) + "\n"


def _serialized_bytes(document: object) -> bytes:
    return render(document).encode("utf-8")


def _serialized_size(document: object) -> int:
    return len(_serialized_bytes(document))


def _size_report(report: dict, max_bytes: int) -> dict:
    for _ in range(8):
        size = _serialized_size(report)
        if size > max_bytes:
            raise QueryError("query and report metadata exceed max_bytes")
        if report["output"]["serialized_bytes"] == size:
            return report
        report["output"]["serialized_bytes"] = size
    raise QueryError("cannot size report")


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
    if not isinstance(spec, dict) or not isinstance(spec.get("fields"), list):
        raise QueryError("invalid SiL Schema fields")
    layout = {}
    offset = 0
    for field in spec["fields"]:
        if not isinstance(field, dict):
            raise QueryError("invalid SiL Schema field")
        name, type_ = field.get("name"), field.get("type")
        count = field.get("count")
        if (not isinstance(name, str) or not name or name in layout
                or not isinstance(type_, str) or type_ not in FORMATS
                or (count is not None and (type(count) is not int or count < 1))):
            raise QueryError("invalid SiL Schema field")
        layout[name] = _Field(name, type_, offset, count)
        offset += SIZES[type_] * (1 if count is None else count)
    selected_fields = []
    for selector in selected:
        if selector in layout:
            selected_fields.append(layout[selector])
            continue
        match = re.fullmatch(r"(.+)\[(0|[1-9][0-9]*)\]", selector, re.DOTALL)
        if match is None or match[1] not in layout:
            raise QueryError(f"unknown or invalid field {selector!r}")
        field = layout[match[1]]
        index = int(match[2])
        if field.count is None or index >= field.count:
            raise QueryError(f"array index out of bounds for {selector!r}")
        field = _Field(selector, field.type,
                       field.offset + index * SIZES[field.type], None)
        selected_fields.append(field)
    return selected_fields, offset


def _position(channel: str, t: int, index: int) -> dict:
    return {"channel": channel, "t_ns": t, "message_index": index}


def _locator(channel: str, field: str, t: int, index: int, value) -> dict:
    return {**_position(channel, t, index), "field": field, "value": value}


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

    def __init__(self, report: dict, limits: _Limits):
        self.report = report
        self.limits = limits
        self.remaining = limits.max_bytes - _metadata_size_bound(report)
        if self.remaining < 0:
            raise QueryError("query and report metadata exceed max_bytes")

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
        if len(self.report["results"]) >= self.limits.max_results:
            self.omit(location, "max_results")
            return False
        return True

    def add(self, row: dict, location: dict) -> None:
        size = _serialized_size(row)
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
        return _size_report(report, self.limits.max_bytes)


@dataclass(frozen=True)
class _Limits:
    max_results: int
    max_bytes: int
    max_messages: int | None

    def validate(self) -> None:
        if type(self.max_results) is not int or self.max_results < 1:
            raise QueryError("max_results must be a positive integer")
        if type(self.max_bytes) is not int or self.max_bytes < 1024:
            raise QueryError("max_bytes must be an integer of at least 1024")
        if self.max_messages is not None and (
                type(self.max_messages) is not int or self.max_messages < 1):
            raise QueryError("max_messages must be a positive integer")


@dataclass(frozen=True)
class _Request:
    channel: str
    fields: Sequence[str]
    start_ns: int | None
    end_ns: int | None
    mode: str
    where: Predicate | None

    def validate(self) -> None:
        if not isinstance(self.channel, str) or not self.channel:
            raise QueryError("select a Channel name")
        if (not isinstance(self.fields, Sequence) or isinstance(self.fields, str)
                or not self.fields or not all(isinstance(f, str) for f in self.fields)
                or len(set(self.fields)) != len(self.fields)):
            raise QueryError("select at least one field, without duplicates")
        for bound in (self.start_ns, self.end_ns):
            if bound is not None and (type(bound) is not int or bound < 0):
                raise QueryError("time bounds must be non-negative integer nanoseconds")
        if (self.start_ns is not None and self.end_ns is not None
                and self.end_ns < self.start_ns):
            raise QueryError("end_ns must be at least start_ns")
        if self.mode not in ("rows", "summary"):
            raise QueryError("mode must be rows or summary")
        if self.where is not None and (not isinstance(self.where, Predicate)
                                      or not isinstance(self.where.field, str)):
            raise QueryError("where must be a Predicate with a field name")

    def document(self) -> dict:
        return {"channel": self.channel, "fields": list(self.fields),
                "start_ns": self.start_ns, "end_ns": self.end_ns, "mode": self.mode,
                "where": None if self.where is None else asdict(self.where)}


@dataclass(frozen=True)
class _Selection:
    fields: list[_Field]
    size: int
    predicate: _PredicateScan | None


def _prepare_selection(path: Path, request: _Request) -> _Selection:
    # Convert external reader/Schema errors at the input boundary. Unexpected
    # errors in our scan and result logic must retain their original type.
    try:
        channels = read_channels(path)
    except (OSError, ValueError, KeyError, TypeError, struct.error, McapError) as e:
        raise QueryError(f"cannot read Recording Channels: {e}") from e
    declared = {c.name: c for c in channels}
    if request.channel not in declared:
        raise QueryError(f"unknown Channel {request.channel!r}")
    spec = declared[request.channel].schema
    fields, size = _fields(spec, request.fields)
    if request.mode == "summary" and any(f.count is not None for f in fields):
        raise QueryError("summaries require scalar fields or explicit array elements")
    predicate = None
    if request.where is not None:
        predicate_fields, _ = _fields(spec, [request.where.field])
        predicate = _PredicateScan(request.where, predicate_fields[0], request.channel)
    return _Selection(fields, size, predicate)


def _new_report(source: dict, request: _Request, limits: _Limits,
                predicate: _PredicateScan | None) -> dict:
    query = request.document()
    return {"sil_recording_query": 1, "source": source, "query": query,
            "status": "complete", "results": [],
            "scan": {"complete": True, "messages": 0, "matches": 0,
                     "predicate_nonfinite": 0, "first_t_ns": None,
                     "last_t_ns": None, "last_message_index": None,
                     "inspected_message_range": [0, 0], "first_message": None,
                     "last_message": None, "next_message": None,
                     "lookahead_messages": 0},
            "query_sha256": hashlib.sha256(_serialized_bytes(query)).hexdigest(),
            "limits": asdict(limits),
            "output": {"complete": True, "returned": 0, "omitted": 0,
                       "first_omitted": None, "serialized_bytes": 0},
            "continuation": None,
            "crossing_context": None if predicate is None or not predicate.crossing else {
                "outside_window": False, "first_message": "no_crossing",
                "nonfinite": "breaks_adjacency"}}


def _metadata_size_bound(report: dict) -> int:
    """Reserve metadata space even for unsorted scans and long Channel names."""
    query = report["query"]
    count = max(report["source"]["size_bytes"], len(query["fields"]))
    position = _position(query["channel"], 2**64 - 1, count)
    longest = max(query["fields"], key=_serialized_size)
    scan = {**report["scan"], "complete": False, "first_t_ns": 2**64 - 1,
            "last_t_ns": 2**64 - 1, "last_message_index": count,
            "inspected_message_range": [0, count], "first_message": position,
            "last_message": position, "next_message": position, "lookahead_messages": 1}
    for key in ("messages", "matches", "predicate_nonfinite"):
        scan[key] = count
    output = {"complete": False, "returned": count, "omitted": count,
              "serialized_bytes": report["limits"]["max_bytes"],
              "first_omitted": {**position, "field": longest, "reason": "oversized_result"}}
    return _serialized_size({**report, "status": "incomplete", "scan": scan,
                             "output": output})


def _query_records(path: Path, request: _Request) -> Generator[tuple[str, int, bytes], None, None]:
    records = read_records(path, channels=[request.channel],
                           start_ns=request.start_ns, end_ns=request.end_ns)
    try:
        while True:
            try:
                message = next(records)
            except StopIteration:
                return
            except (OSError, ValueError, KeyError, TypeError, struct.error, McapError) as e:
                raise QueryError(f"cannot read Recording Messages: {e}") from e
            yield message
    finally:
        close = getattr(records, "close", None)
        if close is not None:
            close()


class _QueryScan:
    def __init__(self, request: _Request, selection: _Selection, results: _Results):
        self.request = request
        self.selection = selection
        self.results = results
        self.scan = results.report["scan"]
        self.summaries = ([_Summary(request.channel, f.name) for f in selection.fields]
                          if request.mode == "summary" else [])

    def run(self, records: Iterable[tuple[str, int, bytes]]) -> None:
        limit = self.results.limits.max_messages
        for index, (_, t, data) in enumerate(records):
            if limit is not None and index >= limit:
                self.scan.update(complete=False, lookahead_messages=1,
                                 next_message=_position(self.request.channel, t, index))
                break
            self._inspect_message(data, t, index)
        if self.selection.predicate is not None:
            self.scan["predicate_nonfinite"] = self.selection.predicate.nonfinite
        for summary in self.summaries:
            location = {"channel": self.request.channel, "field": summary.row["field"]}
            if self.results.accepting(location):
                self.results.add(summary.row, location)

    def _inspect_message(self, data: bytes, t: int, index: int) -> None:
        channel = self.request.channel
        if len(data) != self.selection.size:
            raise QueryError(f"Message {index} on {channel!r} does not match its Schema")
        position = _position(channel, t, index)
        self._record_position(position)
        predicate = self.selection.predicate
        match = None if predicate is None else predicate.match(data, t, index)
        if predicate is not None and match is None:
            return
        self.scan["matches"] += 1
        if self.summaries:
            for summary, field in zip(self.summaries, self.selection.fields):
                summary.add(field.scalar(data), t, index)
        else:
            self._append_row(data, position, match)

    def _record_position(self, position: dict) -> None:
        t = position["t_ns"]
        self.scan["messages"] += 1
        self.scan["inspected_message_range"][1] = self.scan["messages"]
        if self.scan["first_message"] is None:
            self.scan["first_message"] = position
        self.scan["last_message"] = position
        self.scan["last_message_index"] = position["message_index"]
        first, last = self.scan["first_t_ns"], self.scan["last_t_ns"]
        self.scan["first_t_ns"] = t if first is None else min(t, first)
        self.scan["last_t_ns"] = t if last is None else max(t, last)

    def _append_row(self, data: bytes, position: dict, match: _Match | None) -> None:
        if not self.results.accepting(position):
            return
        # Reject an enormous array before allocating its decoded representation.
        if sum(f.count * 2 for f in self.selection.fields if f.count is not None
               ) > self.results.remaining:
            self.results.omit(position, "oversized_result")
            return
        row = {**position, "fields": {f.name: _json_value(f.decode(data))
                                      for f in self.selection.fields}}
        if match is not None:
            row["match"] = match.current
            if match.previous is not None:
                row["previous"] = match.previous
        self.results.add(row, position)


class RecordingQuery:
    """Queries one Recording. Hashes the file once; refuses later source changes.

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
        """The content identity of this Recording, without exposing mutable state."""
        return dict(self._source)

    def _check_source(self) -> None:
        current = self.path.stat()
        keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(current, key) != getattr(self._stamp, key) for key in keys):
            raise QueryError("Recording source changed; create a new RecordingQuery object")

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
        request = _Request(channel, fields, start_ns, end_ns, mode, where)
        limits = _Limits(max_results, max_bytes, max_messages)
        request.validate()
        limits.validate()
        try:
            self._check_source()
            selection = _prepare_selection(self.path, request)
            report = _new_report(self.source, request, limits, selection.predicate)
            results = _Results(report, limits)
            scan = _QueryScan(request, selection, results)
            with closing(_query_records(self.path, request)) as records:
                scan.run(records)
            self._check_source()
            return results.finish()
        except OSError as e:
            raise QueryError(f"cannot query Recording: {e}") from e


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise QueryError(message)


def _refused(arguments: list[str], source: RecordingQuery | None,
             error: QueryError, max_bytes: int) -> dict:
    report = {"sil_recording_query": 1, "status": "refused",
              "source": None if source is None else source.source,
              "request_sha256": hashlib.sha256(_serialized_bytes(arguments)).hexdigest(),
              "arguments": None, "scan": None, "results": [],
              "error": str(error)[:64],
              "output": {"complete": False, "serialized_bytes": 0}}
    if _serialized_size(arguments) + _serialized_size(report) + 16 <= max_bytes:
        report["arguments"] = arguments
    return _size_report(report, max_bytes)


def _parser() -> _Parser:
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
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
