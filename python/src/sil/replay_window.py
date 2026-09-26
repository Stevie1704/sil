"""Select a replay window from a Recording and rebase it to Virtual time zero.

A Run always starts at Virtual time zero, and a stateful vECU cannot be
seeked into the middle of a recording (issue #185). This preparation step
takes a source Recording and a window document and writes a new Recording
that starts where the window starts. The vECU is then warmed up by actual
execution over the first part of the window, and only the rest is evaluated.

The window document declares four instants in the source Recording's own
time, in integer nanoseconds:

    source_origin_ns <= replay_start_ns <= evaluation_start_ns < end_ns

Virtual time = source time - `source_origin_ns`. The Messages in
[replay_start_ns, end_ns) are selected; [replay_start_ns,
evaluation_start_ns) is the warm-up and [evaluation_start_ns, end_ns) is the
evaluation interval. The window is a selection and a rebase, nothing more:

* Messages keep their stored order, so Messages at one timestamp keep their
  Publish order. Payload bytes are copied unchanged, except the integer
  source-time fields that `source_time_fields` names: those are rebased like
  the log time, and only those.
* Each selected Channel must cover the window in the source: a Channel
  that starts after the replay start is missing history, and one that ends
  before the window's last instant (or, with `max_gap_ns`, more than that
  before the end) is insufficient coverage. A selected Channel with no
  Message in the window is rejected.
* A gap is kept as it is; the receipt reports each Channel's longest one, and
  `max_gap_ns`, when declared, rejects a longer one.
* A Channel in `hold_initial` without a Message at the replay start gets its
  latest earlier Message, published at the replay start ahead of the window's
  own Messages. Nothing else is held, and nothing is interpolated.

The receipt names the preparer, the digests of the source, the window
document and the output, both intervals in source and Virtual time, each
Channel's coverage per interval, the Duration the replaying Manifest needs,
and the evaluation window to use in a comparison contract. The output carries
the source and window digests as metadata, so a changed window is a
different Recording and a different Manifest hash.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from mcap.exceptions import McapError
from mcap.reader import make_reader
from mcap.writer import LIBRARY_IDENTIFIER, CompressionType, Writer

from sil import build_info
from sil._schema_types import FORMATS, INT_RANGES, SIZES

WINDOW_VERSION = 1
RECEIPT_VERSION = 1
PREPARER = "sil-window"

_U64_MAX = INT_RANGES["u64"][1]
_TIME_FIELD_TYPES = {name: INT_RANGES[name] for name in ("u64", "i64")}
_INSTANTS = ("source_origin_ns", "replay_start_ns", "evaluation_start_ns",
             "end_ns")
_REQUIRED = {"sil_replay_window", *_INSTANTS, "channels"}
_OPTIONAL = frozenset({"hold_initial", "source_time_fields", "max_gap_ns"})


class WindowError(ValueError):
    """The window document or the source cannot give the declared window."""


@dataclass(frozen=True)
class _Window:
    origin: int
    replay_start: int
    evaluation_start: int
    end: int
    channels: tuple[str, ...]
    hold_initial: tuple[str, ...]
    source_time_fields: dict[str, tuple[str, ...]]
    max_gap: int | None

    def virtual(self, source_ns: int) -> int:
        return source_ns - self.origin


class _Message(NamedTuple):
    channel: str
    ns: int
    data: bytes


@dataclass(frozen=True)
class _SourceChannel:
    schema_name: str
    schema_encoding: str
    schema_data: bytes
    message_encoding: str


def prepare(window: str | Path, source: str | Path, out: str | Path) -> dict:
    """Write the Recording `window` selects from `source` to `out`; return the
    receipt. Nothing is written when the window is rejected."""
    window, source, out = Path(window), Path(source), Path(out)
    if out.suffix != ".mcap":
        raise WindowError(f"output {str(out)!r} must name an .mcap Recording")
    window_bytes = _read(window, "window document")
    source_bytes = _read(source, "source Recording")
    plan = _parse_window(window_bytes, window)
    channels, messages = _read_source(source_bytes, source, plan)
    _check_source_time_fields(plan, channels)
    selected, held = _select(plan, messages)
    digests = {"source_sha256": _sha256(source_bytes),
               "window_sha256": _sha256(window_bytes)}
    recording = _recording(plan, channels, _rebased(plan, channels, selected),
                           digests)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(recording)
    except OSError as e:
        raise WindowError(f"cannot write Recording {str(out)!r}: {e}") from e
    return _receipt(plan, channels, messages, selected, held, {
        "source": {"file": source.name, "sha256": digests["source_sha256"]},
        "window": {"file": window.name, "sha256": digests["window_sha256"]},
        "recording": {"file": out.name, "sha256": _sha256(recording)},
    })


def _read(path: Path, role: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as e:
        raise WindowError(f"cannot read {role} {str(path)!r}: {e}") from e


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# Window document ---------------------------------------------------------------


def _parse_window(data: bytes, path: Path) -> _Window:
    try:
        doc = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_keys)
        return _window(doc)
    except WindowError as e:
        raise WindowError(f"window {str(path)!r}: {e}") from None
    except ValueError as e:
        raise WindowError(f"window {str(path)!r} is not valid JSON: {e}") from e


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    doc: dict = {}
    for key, value in pairs:
        if key in doc:
            raise WindowError(f"duplicate key {key!r}")
        doc[key] = value
    return doc


def _window(doc) -> _Window:
    if not isinstance(doc, dict):
        raise WindowError("the document must be an object")
    unknown = sorted(set(doc) - _REQUIRED - _OPTIONAL)
    if unknown:
        raise WindowError("unknown key(s) " + ", ".join(map(repr, unknown)))
    missing = sorted(_REQUIRED - set(doc))
    if missing:
        raise WindowError("missing key(s) " + ", ".join(map(repr, missing)))
    version = doc["sil_replay_window"]
    if isinstance(version, bool) or version != WINDOW_VERSION:
        raise WindowError(
            f"'sil_replay_window' must be {WINDOW_VERSION}, got {version!r}")
    origin, replay_start, evaluation_start, end = (
        _instant(doc[key], key) for key in _INSTANTS)
    if replay_start < origin:
        raise WindowError(f"replay_start_ns {replay_start} is before "
                          f"source_origin_ns {origin}")
    if evaluation_start < replay_start:
        raise WindowError(f"evaluation_start_ns {evaluation_start} is before "
                          f"replay_start_ns {replay_start}")
    if end <= evaluation_start:
        raise WindowError(f"end_ns {end} must be after evaluation_start_ns "
                          f"{evaluation_start}: the evaluation interval "
                          "[evaluation_start_ns, end_ns) is empty")
    channels = _names(doc["channels"], "'channels'")
    hold_initial = (_names(doc["hold_initial"], "'hold_initial'")
                    if "hold_initial" in doc else ())
    source_time_fields = _source_time_fields(doc.get("source_time_fields", {}))
    for key, names in (("hold_initial", hold_initial),
                       ("source_time_fields", source_time_fields)):
        for name in names:
            if name not in channels:
                raise WindowError(
                    f"{key} channel {name!r} is not a selected channel")
    max_gap = doc.get("max_gap_ns")
    if "max_gap_ns" in doc and (isinstance(max_gap, bool)
                                or not isinstance(max_gap, int)
                                or not 1 <= max_gap <= _U64_MAX):
        raise WindowError("'max_gap_ns' must be an integer from 1 to 2^64 - 1, "
                          f"got {max_gap!r}")
    return _Window(origin, replay_start, evaluation_start, end, channels,
                   hold_initial, source_time_fields, max_gap)


def _instant(value, key: str) -> int:
    if (isinstance(value, bool) or not isinstance(value, int)
            or not 0 <= value <= _U64_MAX):
        raise WindowError(
            f"{key!r} must be an integer from 0 to 2^64 - 1, got {value!r}")
    return value


def _names(value, context: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise WindowError(f"{context} must be a non-empty array")
    names: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise WindowError(f"{context} must hold non-empty strings, "
                              f"got {item!r}")
        if item in names:
            raise WindowError(f"{context}: channel {item!r} is selected twice")
        names.append(item)
    return tuple(names)


def _source_time_fields(value) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        raise WindowError("'source_time_fields' must be an object")
    fields = {}
    for channel, names in value.items():
        context = f"source_time_fields channel {channel!r}"
        if not isinstance(names, list) or not names:
            raise WindowError(f"{context} must be a non-empty array")
        for name in names:
            if not isinstance(name, str) or not name:
                raise WindowError(f"{context} must hold field names")
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise WindowError(f"{context} names field {duplicates[0]!r} twice")
        fields[channel] = tuple(names)
    return fields


# Source Recording --------------------------------------------------------------


def _read_source(data: bytes, path: Path, plan: _Window
                 ) -> tuple[dict[str, _SourceChannel], list[_Message]]:
    """The selected Channels and their Messages, in stored order."""
    try:
        reader = make_reader(io.BytesIO(data))
        summary = reader.get_summary()
        if summary is None:
            raise WindowError(f"source {str(path)!r} has no summary section")
        channels = {}
        for channel in summary.channels.values():
            schema = summary.schemas[channel.schema_id]
            channels[channel.topic] = _SourceChannel(
                schema.name, schema.encoding, schema.data,
                channel.message_encoding)
        for name in plan.channels:
            if name not in channels:
                raise WindowError(f"channel {name!r} is not in the source "
                                  f"Recording {str(path)!r}")
        wanted = set(plan.channels)
        # Stored order is Publish order; log-time order would reorder it.
        messages = [
            _Message(channel.topic, message.log_time, message.data)
            for _, channel, message in reader.iter_messages(
                log_time_order=False)
            if channel.topic in wanted
        ]
    except (McapError, ValueError, KeyError) as e:
        if isinstance(e, WindowError):
            raise
        raise WindowError(
            f"source {str(path)!r} is not a readable Recording: {e}") from e
    return {name: channels[name] for name in plan.channels}, messages


def _check_source_time_fields(plan: _Window,
                              channels: dict[str, _SourceChannel]) -> None:
    for channel, names in plan.source_time_fields.items():
        schema = channels[channel]
        declared = {f["name"]: f for f in _fields(schema)}
        for name in names:
            context = f"source_time_fields channel {channel!r} field {name!r}"
            field = declared.get(name)
            if field is None:
                raise WindowError(f"{context} is not a field of schema "
                                  f"{schema.schema_name!r}")
            if field["type"] not in _TIME_FIELD_TYPES or "count" in field:
                raise WindowError(
                    f"{context} must be a u64 or i64 field of nanoseconds on "
                    f"the source time axis, got {field['type']}")


def _fields(schema: _SourceChannel) -> list[dict]:
    try:
        return json.loads(schema.schema_data)["fields"]
    except (ValueError, KeyError, TypeError):
        raise WindowError(f"schema {schema.schema_name!r} in the source is not "
                          "a SiL schema declaration") from None


# Selection ---------------------------------------------------------------------


def _select(plan: _Window, messages: list[_Message]
            ) -> tuple[list[_Message], dict[str, int]]:
    """The window's Messages in publish order, held values first, and the
    source time of each held Message by Channel."""
    for channel in plan.channels:
        _check_span(plan, channel, _times(messages, channel))
    inside = [m for m in messages if plan.replay_start <= m.ns < plan.end]
    for channel in plan.channels:
        # Checked before holding: a held value does not fill a selection.
        if not _times(inside, channel):
            raise WindowError(
                f"channel {channel!r} has no Message in the window "
                f"[{plan.replay_start}, {plan.end}) ns; an empty selection is "
                "not replayed")
    held_messages, held = _held(plan, messages, inside)
    selected = held_messages + inside
    for channel in plan.channels:
        start, stop = _largest_gap(plan, _times(selected, channel))
        if plan.max_gap is not None and stop - start > plan.max_gap:
            raise WindowError(
                f"channel {channel!r} has no Message from {start} ns to "
                f"{stop} ns, longer than max_gap_ns {plan.max_gap}")
    return selected, held


def _times(messages: list[_Message], channel: str) -> list[int]:
    return [m.ns for m in messages if m.channel == channel]


def _check_span(plan: _Window, channel: str, times: list[int]) -> None:
    """The Channel's own Messages must cover the window: one at or before
    the replay start, and one close enough to the end. Close enough is
    `max_gap_ns` when declared, and the window's last instant otherwise."""
    if not times:
        raise WindowError(f"channel {channel!r} has no Messages in the source")
    first, last = min(times), max(times)
    if plan.replay_start < first:
        raise WindowError(
            f"missing history: channel {channel!r} starts at {first} ns, "
            f"after replay_start_ns {plan.replay_start}")
    if plan.max_gap is None and last < plan.end - 1:
        raise WindowError(
            f"insufficient coverage: channel {channel!r} ends at {last} ns, "
            f"before the window's last instant {plan.end - 1} ns "
            f"(end_ns {plan.end} is exclusive)")
    if plan.max_gap is not None and plan.end - last > plan.max_gap:
        raise WindowError(
            f"insufficient coverage: channel {channel!r} ends at {last} ns, "
            f"more than max_gap_ns {plan.max_gap} before end_ns {plan.end}")


def _held(plan: _Window, messages: list[_Message], inside: list[_Message]
          ) -> tuple[list[_Message], dict[str, int]]:
    held_messages, held = [], {}
    for channel in plan.hold_initial:
        if any(m.channel == channel and m.ns == plan.replay_start
               for m in inside):
            continue
        # The span check guarantees an earlier Message. Take the latest
        # time; among equal times, the last one published.
        before = [m for m in messages
                  if m.channel == channel and m.ns < plan.replay_start]
        latest = max(reversed(before), key=lambda m: m.ns)
        held[channel] = latest.ns
        held_messages.append(latest._replace(ns=plan.replay_start))
    return held_messages, held


def _largest_gap(plan: _Window, times: list[int]) -> tuple[int, int]:
    """The earliest longest interval of the window without a Message."""
    instants = [plan.replay_start, *sorted(times), plan.end]
    return max(zip(instants, instants[1:]), key=lambda gap: gap[1] - gap[0])


# Rebasing ----------------------------------------------------------------------


def _rebased(plan: _Window, channels: dict[str, _SourceChannel],
             selected: list[_Message]) -> list[_Message]:
    patches = {channel: _time_field_patches(channels[channel], names)
               for channel, names in plan.source_time_fields.items()}
    out = []
    for message in selected:
        data = message.data
        for name, offset, fmt, (low, high) in patches.get(message.channel, ()):
            (value,) = fmt.unpack_from(data, offset)
            rebased = plan.virtual(value)
            where = f"channel {message.channel!r} field {name!r}"
            if rebased < low:
                raise WindowError(f"{where}: {value} ns is before the source "
                                  f"origin {plan.origin} ns")
            if rebased > high:
                raise WindowError(f"{where}: {value} ns rebased to {rebased} "
                                  "ns does not fit its type")
            data = data[:offset] + fmt.pack(rebased) + data[offset + fmt.size:]
        out.append(_Message(message.channel, plan.virtual(message.ns), data))
    return out


def _time_field_patches(schema: _SourceChannel, names: tuple[str, ...]):
    """(name, byte offset, struct, range) of each named source-time field."""
    patches, offset = [], 0
    for field in _fields(schema):
        if field["name"] in names:
            patches.append((field["name"], offset,
                            struct.Struct("<" + FORMATS[field["type"]]),
                            _TIME_FIELD_TYPES[field["type"]]))
        offset += SIZES[field["type"]] * field.get("count", 1)
    return patches


# Output ------------------------------------------------------------------------


def _recording(plan: _Window, channels: dict[str, _SourceChannel],
               messages: list[_Message], digests: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    writer = Writer(buffer, compression=CompressionType.NONE)
    writer.start(profile="sil", library=f"{PREPARER}/{build_info.__version__}")
    writer.add_metadata("sil_window", digests)
    schema_ids: dict[tuple[str, bytes], int] = {}
    channel_ids = {}
    for name in plan.channels:
        source = channels[name]
        key = (source.schema_name, source.schema_data)
        if key not in schema_ids:
            schema_ids[key] = writer.register_schema(
                source.schema_name, source.schema_encoding, source.schema_data)
        channel_ids[name] = writer.register_channel(
            name, source.message_encoding, schema_ids[key])
    for message in messages:
        writer.add_message(channel_ids[message.channel], log_time=message.ns,
                           data=message.data, publish_time=message.ns)
    writer.finish()
    return buffer.getvalue()


def _receipt(plan: _Window, channels: dict[str, _SourceChannel],
             source_messages: list[_Message], selected: list[_Message],
             held: dict[str, int], files: dict) -> dict:
    def interval(start: int, end: int) -> dict:
        return {"source": {"start_ns": start, "end_ns": end},
                "virtual": {"start_ns": plan.virtual(start),
                            "end_ns": plan.virtual(end)}}

    def coverage(times: list[int]) -> dict:
        return {"messages": len(times),
                "first_ns": plan.virtual(min(times)) if times else None,
                "last_ns": plan.virtual(max(times)) if times else None}

    report = {}
    for name in plan.channels:
        times = _times(selected, name)
        source = _times(source_messages, name)
        start, stop = _largest_gap(plan, times)
        report[name] = {
            "schema": channels[name].schema_name,
            "source_span": {"first_ns": min(source), "last_ns": max(source)},
            "held": {"source_ns": held[name]} if name in held else None,
            "largest_gap_ns": stop - start,
            "warm_up": coverage([t for t in times
                                 if t < plan.evaluation_start]),
            "evaluation": coverage([t for t in times
                                    if t >= plan.evaluation_start]),
        }
    return {
        "sil_window_receipt": RECEIPT_VERSION,
        "preparer": {"name": PREPARER, "version": build_info.__version__,
                     "revision": build_info.SOURCE_REVISION,
                     "mcap": LIBRARY_IDENTIFIER},
        **files,
        "source_origin_ns": plan.origin,
        "intervals": {
            "warm_up": interval(plan.replay_start, plan.evaluation_start),
            "evaluation": interval(plan.evaluation_start, plan.end),
        },
        "duration_ns": plan.virtual(plan.end),
        "evaluation_window": {"from_ns": plan.virtual(plan.evaluation_start),
                              "to_ns": plan.virtual(plan.end) - 1},
        "channels": report,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=PREPARER,
        description="Select a replay window with a warm-up from a Recording "
                    "and rebase it to Virtual time zero.",
        allow_abbrev=False,
    )
    parser.add_argument("window", type=Path, help="the window document (JSON)")
    parser.add_argument("source", type=Path, help="the source Recording")
    parser.add_argument("-o", "--output", type=Path, required=True,
                        help="the .mcap Recording to write")
    parser.add_argument("--receipt", type=Path, default=None,
                        help="write the receipt here instead of to standard "
                             "output")
    args = parser.parse_args(argv)
    try:
        receipt = prepare(args.window, args.source, args.output)
    except WindowError as e:
        sys.stderr.write(f"{PREPARER}: error: {e}\n")
        return 2
    text = json.dumps(receipt, indent=2) + "\n"
    if args.receipt is None:
        sys.stdout.write(text)
        return 0
    try:
        args.receipt.write_text(text)
    except OSError as e:
        sys.stderr.write(f"{PREPARER}: error: cannot write receipt "
                         f"{str(args.receipt)!r}: {e}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
