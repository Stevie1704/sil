"""Read a Recording: list its Channels, or print its Messages decoded.

Each Message is decoded with the Schema the Recording stores for its Channel,
so no Manifest is needed. Both commands are read-only.

`sil recording channels` lists each Channel with its Schema name, Message
count and first and last virtual time, and the Manifest hash of the Run that
wrote the Recording (none for a Recording converted at the edge). With
`--json` it is one `sil_recording_channels` report.

`sil recording show` prints the Messages in stored order. `--channel` and the
half-open virtual-time interval [`--from`, `--to`) select them, and the MCAP
index applies that selection, so a long Recording is not loaded at once. With
`--json` each Message is one line: an object with `channel`, `t_ns` and the
decoded `fields`. A `u8` array is a lowercase hex string (opaque bytes, such
as a CAN frame, stay bytes), any other array a list, and a non-finite float
the string "NaN", "Infinity" or "-Infinity", because JSON has no such numbers.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import struct
import sys
from collections.abc import Iterator
from pathlib import Path

from mcap.exceptions import McapError

from sil import schema
from sil.recording import read_channels, read_manifest_hash, read_records

PROG = "sil recording"
CHANNELS_VERSION = 1
EXIT_USAGE = 2


class RecordingViewError(ValueError):
    """The Recording cannot be read, or the arguments name what it lacks."""


def channels(path: str | Path) -> dict:
    """The `sil_recording_channels` report of the Recording at `path`."""
    declared = _read_channels(path)
    seen: dict[str, list[int]] = {}  # Channel -> [count, first, last]
    for topic, t, _ in _read_records(path):
        stats = seen.setdefault(topic, [0, t, t])
        stats[0] += 1
        stats[2] = t
    rows = []
    for channel in declared:
        count, first, last = seen.get(channel.name, (0, None, None))
        rows.append({"channel": channel.name, "schema": channel.schema_name,
                     "messages": count, "first_t_ns": first, "last_t_ns": last})
    return {"sil_recording_channels": CHANNELS_VERSION,
            "manifest_hash": read_manifest_hash(path),
            "channels": rows}


def messages(path: str | Path, *, channels: list[str] | None = None,
             start_ns: int | None = None, end_ns: int | None = None,
             ) -> Iterator[dict]:
    """The selected Messages of the Recording, decoded, in stored order.

    Raises RecordingViewError for a Channel the Recording does not declare;
    the message lists the Channels it does."""
    try:
        declared = {c.name: schema.MessageType(c.schema_name, c.schema)
                    for c in _read_channels(path)}
    except (KeyError, TypeError, struct.error) as e:
        raise RecordingViewError(
            f"Recording {str(path)!r} stores a Schema that is not a SiL "
            f"Schema: {e!r}") from e
    unknown = [name for name in channels or () if name not in declared]
    if unknown:
        raise RecordingViewError(
            f"unknown Channel(s) {', '.join(map(repr, unknown))}; the Recording "
            f"has: {', '.join(declared) or 'no Channels'}")
    for topic, t, data in _read_records(path, topics=channels,
                                        start_ns=start_ns, end_ns=end_ns):
        try:
            fields = declared[topic].unpack(data)
        except struct.error as e:
            raise RecordingViewError(
                f"Message on {topic!r} at t_ns {t} does not match its "
                f"Schema: {e}") from e
        yield {"channel": topic, "t_ns": t,
               "fields": {name: _json_value(v) for name, v in fields.items()}}


def _json_value(value):
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, list):
        return [_json_value(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else (
            "Infinity" if value > 0 else "-Infinity")
    return value


def _read_channels(path):
    try:
        return read_channels(path)
    except (OSError, ValueError, McapError) as e:
        raise RecordingViewError(f"cannot read Recording {str(path)!r}: {e}") from e


def _read_records(path, **selection):
    try:
        yield from read_records(path, **selection)
    except (OSError, ValueError, McapError) as e:
        raise RecordingViewError(f"cannot read Recording {str(path)!r}: {e}") from e


# Command line ------------------------------------------------------------------


def _virtual_time(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = -1
    if value < 0:
        raise argparse.ArgumentTypeError(
            f"must be a non-negative integer of nanoseconds, got {text!r}")
    return value


def _positive(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(
            f"must be a positive integer, got {text!r}")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG, description="Read a Recording with the Schemas it stores.",
        allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser(
        "channels", help="list the Channels with Schema, Message count and times")
    showing = commands.add_parser(
        "show", help="print decoded Messages")
    for command in (listing, showing):
        command.add_argument("recording", type=Path, help="the .mcap Recording")
        command.add_argument("--json", action="store_true",
                             help="print JSON instead of text")
    showing.add_argument("--channel", action="append", metavar="NAME",
                         help="show only this Channel (repeatable)")
    showing.add_argument("--from", dest="start_ns", type=_virtual_time,
                         metavar="T", help="first virtual time, in ns (inclusive)")
    showing.add_argument("--to", dest="end_ns", type=_virtual_time,
                         metavar="T", help="end of the interval, in ns (exclusive)")
    showing.add_argument("--limit", type=_positive, metavar="N",
                         help="stop after N Messages")
    return parser


def _write_channels(report: dict, as_json: bool) -> None:
    if as_json:
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
        return
    sys.stdout.write(f"manifest_hash {report['manifest_hash'] or 'none'}\n")
    header = ("CHANNEL", "SCHEMA", "MESSAGES", "FIRST_T_NS", "LAST_T_NS")
    rows = [header] + [
        tuple("-" if c[key] is None else str(c[key]) for key in
              ("channel", "schema", "messages", "first_t_ns", "last_t_ns"))
        for c in report["channels"]]
    widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
    for row in rows:
        sys.stdout.write("  ".join(cell.ljust(w) for cell, w in
                                   zip(row, widths)).rstrip() + "\n")


def _write_message(message: dict, as_json: bool) -> None:
    if as_json:
        sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
        return
    fields = " ".join(f"{name}={json.dumps(value)}"
                      for name, value in message["fields"].items())
    sys.stdout.write(f"{message['t_ns']} {message['channel']} {fields}\n")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "channels":
            _write_channels(channels(args.recording), args.json)
            return 0
        selected = messages(args.recording, channels=args.channel,
                            start_ns=args.start_ns, end_ns=args.end_ns)
        for message in itertools.islice(selected, args.limit):
            _write_message(message, args.json)
    except RecordingViewError as e:
        sys.stderr.write(f"{PROG} {args.command}: error: {e}\n")
        return EXIT_USAGE
    return 0


if __name__ == "__main__":
    sys.exit(main())
