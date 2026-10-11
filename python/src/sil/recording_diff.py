"""Where two Recordings of one Manifest first differ.

The determinism check asks for exact equality, so this is a byte-level diff:
no contract and no tolerance. The Messages are compared in stored order, as
(Channel, virtual time, payload bytes). The first different Message is the
first difference. Two Recordings that differ in no Message still differ
somewhere, and the difference says so.
"""

from __future__ import annotations

import hashlib
from itertools import zip_longest
from pathlib import Path
from typing import NamedTuple

from mcap.exceptions import McapError

from sil.recording import read_channels, read_metadata, read_records


def first_difference(run1: str | Path, run2: str | Path) -> dict | None:
    """The first difference between two Recordings, or None when they are
    bit-identical."""
    if Path(run1).read_bytes() == Path(run2).read_bytes():
        return None
    try:
        return (_metadata_difference(run1, run2)
                or _channel_difference(run1, run2)
                or _first_message_difference(run1, run2)
                or _difference(
                    "bytes",
                    detail="the Recordings differ in bytes outside the "
                           "metadata, Channels, Schemas and Messages"))
    except (OSError, ValueError, McapError) as error:
        return _difference("unreadable",
                           detail=f"cannot read the Recordings to find where "
                                  f"they differ: {error}")


def _metadata_difference(run1, run2) -> dict | None:
    a, b = (_metadata(path) for path in (run1, run2))
    if a == b:
        return None
    return _difference("metadata", run1=a, run2=b,
                       detail=f"the metadata records differ: run 1 {a}, "
                              f"run 2 {b}")


def _metadata(path) -> list[dict]:
    return [{"name": name, "metadata": metadata}
            for name, metadata in read_metadata(path)]


def _channel_difference(run1, run2) -> dict | None:
    """A different Channel list, or else the first Channel whose Schema
    differs."""
    a, b = read_channels(run1), read_channels(run2)
    names_a, names_b = [c.name for c in a], [c.name for c in b]
    if names_a != names_b:
        return _difference("channels", run1=names_a, run2=names_b,
                           detail=f"the Channels differ: run 1 {names_a}, "
                                  f"run 2 {names_b}")
    for x, y in zip(a, b):
        if x != y:
            return _difference(
                "schema", channel=x.name, run1=_schema(x), run2=_schema(y),
                detail=f"the Schema of Channel {x.name!r} differs")
    return None


def _schema(channel) -> dict:
    return {"name": channel.schema_name, "schema": channel.schema}


class _Message(NamedTuple):
    """One recorded Message, as the byte-level diff compares it."""

    channel: str
    t_ns: int
    payload: bytes


def _first_message_difference(run1, run2) -> dict | None:
    pairs = zip_longest(map(_Message._make, read_records(run1)),
                        map(_Message._make, read_records(run2)))
    for index, (a, b) in enumerate(pairs):
        if a != b:
            first = a or b
            return _difference(
                "message", channel=first.channel, t_ns=first.t_ns,
                message_index=index, run1=_message(a), run2=_message(b),
                detail=_message_detail(index, a, b))
    return None


def _message_detail(index: int, a: _Message | None, b: _Message | None) -> str:
    if a is not None and b is not None and a[:2] == b[:2]:
        return (f"Message {index} in stored order, on Channel {a.channel!r} "
                f"at t_ns {a.t_ns}, has a different payload")
    return (f"Message {index} in stored order differs: run 1 {_describe(a)}, "
            f"run 2 {_describe(b)}")


def _difference(kind: str, *, channel=None, t_ns=None, message_index=None,
                run1=None, run2=None, detail: str) -> dict:
    return {"kind": kind, "channel": channel, "t_ns": t_ns,
            "message_index": message_index, "run1": run1, "run2": run2,
            "detail": detail}


def _message(message: _Message | None) -> dict | None:
    # The payload's hash, not its bytes: a large Message would swamp the
    # report. Read the payload from the kept Recordings.
    if message is None:
        return None
    return {"channel": message.channel, "t_ns": message.t_ns,
            "payload_sha256": hashlib.sha256(message.payload).hexdigest()}


def _describe(message: _Message | None) -> str:
    if message is None:
        return "has no Message"
    return f"has {message.channel!r} at t_ns {message.t_ns}"
