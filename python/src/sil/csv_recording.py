"""Convert a timestamped CSV file into a Recording a Replay participant accepts.

This is the edge where a source format meets the kernel's typed contract
(issue #179). A mapping document names the timestamp column and its unit, the
schemas of the output Channels, and which CSV column feeds each schema field
with which scale and offset. The output is an MCAP file shaped the way the
kernel's recorder writes one — each Channel's schema record carries the
canonical schema JSON — so a Manifest that declares the same schemas replays it
unchanged. Nothing about CSV reaches the kernel.

The conversion is exact or it is rejected:

* A timestamp cell is a plain decimal number. It is converted with rational
  arithmetic to integer nanoseconds after the mapping's origin; a result that
  is not a whole number of nanoseconds, is negative, or does not fit in u64 is
  rejected, as is a row whose timestamp descends below the previous row's.
* An integer field takes an integer cell, an integer scale and an integer
  offset, and must land inside the field type's range.
* A float field is `cell × scale + offset` in binary64, in that order, with
  each step skipped at its identity value; an f32 field is then rounded to the
  nearest f32. The mapping's scale and offset are rounded to binary64 first. A
  non-finite cell or result is rejected, and so is a nonzero value that
  underflows to zero.

Rows are emitted in file order and, within one row, in mapping order, so rows
sharing a timestamp keep their order in the Recording's publish order. A row
whose cells for one Channel are all empty carries no Message of that Channel;
a Channel with only some of its cells empty is rejected, because completing
it would invent a value. Nothing is interpolated. One column may feed several
fields, the timestamp column included: that reads one cell twice and is not
ambiguous.

A conversion receipt names the converter, the digests of the source, the
mapping and the Recording, each Channel's message count, and the time bounds.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
import struct
import sys
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import NamedTuple

from mcap.writer import LIBRARY_IDENTIFIER, CompressionType, Writer

from sil import build_info
from sil._schema_types import FORMATS, INT_RANGES
from sil.manifest import Manifest, ManifestError

MAPPING_VERSION = 1
RECEIPT_VERSION = 1
CONVERTER = "sil-csv"

_NS_PER_UNIT = {"s": 10**9, "ms": 10**6, "us": 10**3, "ns": 1}
_U64_MAX = 2**64 - 1

# ASCII digits only: `\d` would also accept other scripts' digits.
_DECIMAL = re.compile(r"[+-]?[0-9]+(\.[0-9]+)?")
_INTEGER = re.compile(r"[+-]?[0-9]+")
_FLOAT = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?")
_NON_FINITE = re.compile(r"[+-]?(nan|inf|infinity)", re.IGNORECASE)
_F32 = struct.Struct("<f")


class ConversionError(ValueError):
    """The mapping or the source cannot be converted without guessing."""


class _CellError(ValueError):
    """One cell's reason for rejection; the caller adds where it is."""


class _Message(NamedTuple):
    channel: int  # index into the mapping's Channels
    ns: int
    payload: bytes


def _exact(parse, cell: str):
    """`parse(cell)`, with Python's digit limit reported as a cell error."""
    try:
        return parse(cell)
    except ValueError:
        raise _CellError(f"{cell[:20]!r}... has too many digits") from None


@dataclass(frozen=True)
class _Clock:
    column: str
    unit: str
    origin: Fraction

    def ns(self, cell: str) -> int:
        if not _DECIMAL.fullmatch(cell):
            raise _CellError(f"{cell!r} is not a plain decimal timestamp")
        ns = (_exact(Fraction, cell) - self.origin) * _NS_PER_UNIT[self.unit]
        if ns.denominator != 1:
            raise _CellError(
                f"{cell} {self.unit} is not a whole number of nanoseconds "
                "after the origin"
            )
        if ns < 0:
            raise _CellError(f"{cell} {self.unit} is negative after the origin")
        if ns > _U64_MAX:
            raise _CellError(
                f"{cell} {self.unit} overflows u64 nanoseconds after the origin"
            )
        return int(ns)


@dataclass(frozen=True)
class _Field:
    name: str
    type: str
    column: str
    scale: int | float
    offset: int | float

    def value(self, cell: str) -> int | float:
        if self.type in INT_RANGES:
            return self._integer(cell)
        return self._float(cell)

    def _integer(self, cell: str) -> int:
        if not _INTEGER.fullmatch(cell):
            raise _CellError(f"{cell!r} is not an integer for {self.type} "
                             f"field {self.name!r}")
        value = _exact(int, cell) * self.scale + self.offset
        low, high = INT_RANGES[self.type]
        if not low <= value <= high:
            raise _CellError(f"{value} is outside the {self.type} range "
                             f"[{low}, {high}] of field {self.name!r}")
        return value

    def _float(self, cell: str) -> float:
        if _NON_FINITE.fullmatch(cell):
            raise _CellError(f"{cell!r} is not a finite number")
        if not _FLOAT.fullmatch(cell):
            raise _CellError(f"{cell!r} is not a decimal number")
        value = self._nonzero(float(cell), _exact(Decimal, cell) != 0, cell)
        if self.scale != 1:
            value = self._nonzero(value * self.scale, value != 0, cell)
        if self.offset != 0:
            value += self.offset
        if not math.isfinite(value):
            raise _CellError(f"{cell!r} does not convert to a finite number "
                             f"for field {self.name!r}")
        if self.type == "f32":
            try:
                narrowed = _F32.unpack(_F32.pack(value))[0]
            except OverflowError:
                raise _CellError(f"{value!r} is outside the f32 range of field "
                                 f"{self.name!r}") from None
            value = self._nonzero(narrowed, value != 0, cell)
        return value

    def _nonzero(self, value: float, was_nonzero: bool, cell: str) -> float:
        if value == 0 and was_nonzero:
            raise _CellError(f"{cell!r} underflows to zero in {self.type} "
                             f"field {self.name!r}")
        return value


@dataclass(frozen=True)
class _Channel:
    name: str
    schema: str
    schema_json: bytes
    fields: tuple[_Field, ...]
    layout: struct.Struct


@dataclass(frozen=True)
class _Mapping:
    clock: _Clock
    channels: tuple[_Channel, ...]

    def columns(self) -> list[str]:
        columns = [self.clock.column]
        for channel in self.channels:
            columns.extend(f.column for f in channel.fields)
        return list(dict.fromkeys(columns))


def convert(mapping: str | Path, source: str | Path, out: str | Path) -> dict:
    """Write the Recording for `source` under `mapping` to `out`; return the
    conversion receipt. Nothing is written when the conversion is rejected."""
    mapping, source, out = Path(mapping), Path(source), Path(out)
    if out.suffix != ".mcap":
        raise ConversionError(
            f"output {str(out)!r} must name an .mcap Recording"
        )
    mapping_bytes = _read(mapping, "mapping")
    source_bytes = _read(source, "source")
    plan = _parse_mapping(mapping_bytes, mapping)
    rows, messages = _read_rows(plan, source_bytes, source)
    if not messages:
        raise ConversionError(f"{source}: the CSV carries no Messages")
    digests = {"source_sha256": _sha256(source_bytes),
               "mapping_sha256": _sha256(mapping_bytes)}
    recording = _recording(plan, messages, digests)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(recording)
    except OSError as e:
        raise ConversionError(f"cannot write Recording {str(out)!r}: {e}") from e
    return _receipt(plan, messages, {
        "source": {"file": source.name, "sha256": digests["source_sha256"],
                   "rows": rows},
        "mapping": {"file": mapping.name, "sha256": digests["mapping_sha256"]},
        "recording": {"file": out.name, "sha256": _sha256(recording)},
    })


def _read(path: Path, role: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as e:
        raise ConversionError(f"cannot read {role} {str(path)!r}: {e}") from e


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# Mapping -----------------------------------------------------------------------


def _parse_mapping(data: bytes, path: Path) -> _Mapping:
    try:
        doc = json.loads(
            data.decode("utf-8"),
            parse_float=Decimal,
            parse_constant=_non_finite_literal,
            object_pairs_hook=_unique_keys,
        )
    except ConversionError as e:
        raise ConversionError(f"mapping {str(path)!r}: {e}") from None
    except ValueError as e:  # also a number past Python's digit limit
        raise ConversionError(f"mapping {str(path)!r} is not valid JSON: {e}") from e
    try:
        return _mapping(doc)
    except ConversionError as e:
        raise ConversionError(f"mapping {str(path)!r}: {e}") from None


def _non_finite_literal(literal: str):
    raise ConversionError(f"{literal} is not a finite number")


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    doc: dict = {}
    for key, value in pairs:
        if key in doc:
            raise ConversionError(f"duplicate key {key!r}")
        doc[key] = value
    return doc


def _object(value, context: str, required: set[str],
            optional: frozenset[str] = frozenset()) -> dict:
    if not isinstance(value, dict):
        raise ConversionError(f"{context} must be an object")
    unknown = sorted(set(value) - required - optional)
    if unknown:
        raise ConversionError(f"{context} has unknown key(s) "
                              + ", ".join(map(repr, unknown)))
    missing = sorted(required - set(value))
    if missing:
        raise ConversionError(f"{context} is missing key(s) "
                              + ", ".join(map(repr, missing)))
    return value


def _string(value, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConversionError(f"{context} must be a non-empty string")
    return value


def _number(value, context: str) -> int | Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ConversionError(f"{context} must be a number, got {value!r}")
    return value


def _mapping(doc) -> _Mapping:
    doc = _object(doc, "the document",
                  {"sil_csv_mapping", "timestamp", "schemas", "channels"})
    version = doc["sil_csv_mapping"]
    if isinstance(version, bool) or version != MAPPING_VERSION:
        raise ConversionError(
            f"'sil_csv_mapping' must be {MAPPING_VERSION}, got {version!r}"
        )
    clock = _clock(doc["timestamp"])
    schemas = _schemas(doc["schemas"])
    entries = doc["channels"]
    if not isinstance(entries, list) or not entries:
        raise ConversionError("'channels' must be a non-empty array")
    channels: list[_Channel] = []
    for index, entry in enumerate(entries):
        channel = _channel(entry, f"channels[{index}]", schemas)
        if any(c.name == channel.name for c in channels):
            raise ConversionError(f"channel {channel.name!r} is mapped twice")
        channels.append(channel)
    return _Mapping(clock, tuple(channels))


def _clock(value) -> _Clock:
    value = _object(value, "'timestamp'", {"column", "unit"},
                    frozenset({"origin"}))
    unit = value["unit"]
    if unit not in _NS_PER_UNIT:
        raise ConversionError(
            f"'timestamp' unit must be one of {', '.join(_NS_PER_UNIT)}, "
            f"got {unit!r}"
        )
    origin = _number(value.get("origin", 0), "'timestamp' origin")
    return _Clock(_string(value["column"], "'timestamp' column"), unit,
                  Fraction(origin))


def _schemas(value) -> dict[str, dict]:
    if not isinstance(value, dict):
        raise ConversionError("'schemas' must be an object")
    try:
        # The builder's own schema checks, so the two cannot disagree on what
        # a valid schema is.
        Manifest(duration_ns=1).add_schemas(value)
    except ManifestError as e:
        raise ConversionError(str(e)) from None
    for name, schema in value.items():
        for field in schema["fields"]:
            if "count" in field:
                raise ConversionError(
                    f"schema {name!r} field {field['name']!r}: only scalar "
                    "fields are supported"
                )
    return value


def _channel(entry, context: str, schemas: dict[str, dict]) -> _Channel:
    entry = _object(entry, context, {"channel", "schema", "fields"})
    name = _string(entry["channel"], f"{context} 'channel'")
    context = f"channel {name!r}"
    schema_name = _string(entry["schema"], f"{context} 'schema'")
    if schema_name not in schemas:
        raise ConversionError(f"{context} names undeclared schema {schema_name!r}")
    declared = schemas[schema_name]["fields"]
    specs = entry["fields"]
    if not isinstance(specs, dict):
        raise ConversionError(f"{context} 'fields' must be an object")
    names = [f["name"] for f in declared]
    extra = sorted(specs.keys() - set(names))
    if extra:
        raise ConversionError(
            f"{context}: " + ", ".join(map(repr, extra))
            + f" is not a field of schema {schema_name!r}"
        )
    missing = [name for name in names if name not in specs]
    if missing:
        raise ConversionError(
            f"{context}: schema field(s) " + ", ".join(map(repr, missing))
            + " not mapped to a column"
        )
    fields = tuple(
        _field(f["name"], f["type"], specs[f["name"]], f"{context} field {f['name']!r}")
        for f in declared
    )
    return _Channel(
        name=name,
        schema=schema_name,
        # The kernel's canonical form of a schema: sorted keys, compact,
        # UTF-8. Its replayer compares these bytes with the Manifest's.
        schema_json=json.dumps(schemas[schema_name], sort_keys=True,
                               separators=(",", ":"),
                               ensure_ascii=False).encode(),
        fields=fields,
        layout=struct.Struct("<" + "".join(FORMATS[f.type] for f in fields)),
    )


def _field(name: str, field_type: str, spec, context: str) -> _Field:
    spec = _object(spec, context, {"column"}, frozenset({"scale", "offset"}))
    column = _string(spec["column"], f"{context} 'column'")
    factors = {}
    for key, identity in (("scale", 1), ("offset", 0)):
        number = _number(spec.get(key, identity), f"{context} {key}")
        if field_type in INT_RANGES:
            if not isinstance(number, int):
                raise ConversionError(
                    f"{context}: {key} must be an integer for a {field_type} "
                    f"field, got {number}"
                )
            factors[key] = number
        else:
            try:
                factors[key] = float(number)
            except OverflowError:
                factors[key] = math.inf
            if not math.isfinite(factors[key]):
                raise ConversionError(f"{context}: {key} {number} is not a "
                                      "finite binary64 number")
    return _Field(name, field_type, column, factors["scale"], factors["offset"])


# Source rows -------------------------------------------------------------------


def _read_rows(plan: _Mapping, data: bytes, path: Path
               ) -> tuple[int, list[_Message]]:
    """The row count and the Messages in emit order."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ConversionError(f"{path}: source is not UTF-8 text: {e}") from e
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = next(reader, None)
        if header is None:
            raise ConversionError(f"{path}: the CSV has no header row")
        index = _column_index(header, plan, path)
        messages: list[_Message] = []
        previous_ns: int | None = None
        rows = 0
        for rows, cells in enumerate(reader, 1):
            where = f"{path}: row {rows} (line {reader.line_num})"
            if len(cells) != len(header):
                raise ConversionError(
                    f"{where}: expected {len(header)} cells, got {len(cells)}"
                )
            previous_ns = _row_ns(plan.clock, index, cells, where, previous_ns)
            messages.extend(_row_messages(plan, index, cells, where, previous_ns))
    except csv.Error as e:
        raise ConversionError(
            f"{path}: line {reader.line_num}: malformed CSV: {e}"
        ) from e
    return rows, messages


def _column_index(header: list[str], plan: _Mapping, path: Path) -> dict[str, int]:
    index: dict[str, int] = {}
    for position, name in enumerate(header):
        if name in index:
            raise ConversionError(
                f"{path}: column {name!r} appears more than once in the header"
            )
        index[name] = position
    missing = [c for c in plan.columns() if c not in index]
    if missing:
        raise ConversionError(
            f"{path}: missing required column(s) " + ", ".join(map(repr, missing))
        )
    return index


def _row_ns(clock: _Clock, index: dict[str, int], cells: list[str], where: str,
            previous_ns: int | None) -> int:
    try:
        ns = clock.ns(cells[index[clock.column]])
    except _CellError as e:
        raise ConversionError(f"{where} column {clock.column!r}: {e}") from None
    if previous_ns is not None and ns < previous_ns:
        raise ConversionError(
            f"{where} column {clock.column!r}: timestamp {ns} ns descends "
            f"below the previous row's {previous_ns} ns"
        )
    return ns


def _row_messages(plan: _Mapping, index: dict[str, int], cells: list[str],
                  where: str, ns: int) -> list[_Message]:
    messages = []
    for channel_index, channel in enumerate(plan.channels):
        payload = _payload(channel, index, cells, where)
        if payload is not None:
            messages.append(_Message(channel_index, ns, payload))
    return messages


def _payload(channel: _Channel, index: dict[str, int], cells: list[str],
             where: str) -> bytes | None:
    """The Channel's payload from this row, or None when it has no Message."""
    raw = [cells[index[f.column]] for f in channel.fields]
    empty = [f.column for f, cell in zip(channel.fields, raw) if cell == ""]
    if len(empty) == len(raw):
        return None
    if empty:
        raise ConversionError(
            f"{where}: channel {channel.name!r} has empty cell(s) in column(s) "
            + ", ".join(map(repr, empty))
            + " but not in its others; a partial Message is not completed"
        )
    values = []
    for field, cell in zip(channel.fields, raw):
        try:
            values.append(field.value(cell))
        except _CellError as e:
            raise ConversionError(f"{where} column {field.column!r}: {e}") from None
    return channel.layout.pack(*values)


# Output ------------------------------------------------------------------------


def _recording(plan: _Mapping, messages: list[_Message],
               digests: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    writer = Writer(buffer, compression=CompressionType.NONE)
    writer.start(profile="sil", library=f"{CONVERTER}/{build_info.__version__}")
    writer.add_metadata("sil_csv", digests)
    schema_ids: dict[str, int] = {}
    channel_ids = []
    for channel in plan.channels:
        if channel.schema not in schema_ids:
            schema_ids[channel.schema] = writer.register_schema(
                channel.schema, "sil_pod", channel.schema_json)
        channel_ids.append(writer.register_channel(
            channel.name, "sil_pod", schema_ids[channel.schema]))
    for message in messages:
        writer.add_message(channel_ids[message.channel], log_time=message.ns,
                           data=message.payload, publish_time=message.ns)
    writer.finish()
    return buffer.getvalue()


def _receipt(plan: _Mapping, messages: list[_Message],
             files: dict) -> dict:
    channels = {c.name: {"schema": c.schema, "messages": 0, "first_ns": None,
                         "last_ns": None} for c in plan.channels}
    for message in messages:
        entry = channels[plan.channels[message.channel].name]
        entry["messages"] += 1
        if entry["first_ns"] is None:
            entry["first_ns"] = message.ns
        entry["last_ns"] = message.ns
    return {
        "sil_csv_receipt": RECEIPT_VERSION,
        "converter": {"name": CONVERTER, "version": build_info.__version__,
                      "revision": build_info.SOURCE_REVISION,
                      "mcap": LIBRARY_IDENTIFIER},
        **files,
        "channels": channels,
        "time_bounds": {"first_ns": messages[0].ns, "last_ns": messages[-1].ns},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=CONVERTER,
        description="Convert a timestamped CSV file into a replayable "
                    "Recording under a mapping document.",
        allow_abbrev=False,
    )
    parser.add_argument("mapping", type=Path, help="the mapping document (JSON)")
    parser.add_argument("source", type=Path, help="the CSV file to convert")
    parser.add_argument("-o", "--output", type=Path, required=True,
                        help="the .mcap Recording to write")
    parser.add_argument("--receipt", type=Path, default=None,
                        help="write the conversion receipt here instead of "
                             "to standard output")
    args = parser.parse_args(argv)
    try:
        receipt = convert(args.mapping, args.source, args.output)
    except ConversionError as e:
        sys.stderr.write(f"{CONVERTER}: error: {e}\n")
        return 2
    text = json.dumps(receipt, indent=2) + "\n"
    if args.receipt is None:
        sys.stdout.write(text)
        return 0
    try:
        args.receipt.write_text(text)
    except OSError as e:
        sys.stderr.write(f"{CONVERTER}: error: cannot write receipt "
                         f"{str(args.receipt)!r}: {e}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
