"""A finalized source Recording, as the visualization interfaces read it.

`record_index` is the ordinal of a Message in the whole source's stored
order, counted before any Channel selection. With the source's SHA-256 it
is the stable locator `(source_sha256, record_index)`. It is distinct from
the window-relative `message_index` of `sil recording query`.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, NamedTuple

from mcap.exceptions import McapError
from mcap.reader import make_reader

from sil.recording import read_manifest_hash, read_records
from sil.visualization.interchange import SOURCE_ENCODING

_MAGIC = b"\x89MCAP0\r\n"


class SourceError(ValueError):
    """The source is not a finalized Recording with supported encodings."""


class SelectionError(SourceError):
    """A Channel selection names no, an unknown or a repeated Channel."""


@dataclass(frozen=True)
class SourceChannel:
    name: str
    schema_name: str
    schema: dict
    schema_sha256: str


class SourceRecord(NamedTuple):
    record_index: int
    channel: str
    publication_time_ns: int
    data: bytes


@dataclass(frozen=True)
class Source:
    path: Path
    sha256: str
    size_bytes: int
    manifest_hash: str | None
    channels: tuple[SourceChannel, ...]  # in declaration order
    _identity: tuple

    def records(self) -> Iterator[SourceRecord]:
        """Every Message in stored order, with its whole-source ordinal."""
        try:
            for index, (topic, time_ns, data) in enumerate(read_records(self.path)):
                yield SourceRecord(index, topic, time_ns, data)
        except (McapError, ValueError, KeyError, EOFError) as e:
            raise SourceError(f"source {str(self.path)!r} has an unreadable "
                              f"Message: {e}") from e

    def check_unchanged(self) -> None:
        """Refuse a source that changed since it was opened."""
        if _identity(self.path) != self._identity:
            raise SourceError(f"source {str(self.path)!r} changed while it "
                              "was read")


def open_source(path: str | Path) -> Source:
    """The identity and Channel catalog of a finalized `sil_pod` Recording."""
    path = Path(path)
    if path.suffix != ".mcap":
        raise SourceError(f"source {str(path)!r} must be an .mcap Recording")
    try:
        identity = _identity(path)
        digest, size = _sha256(path)
        _check_finalized(path, size)
        channels = _channels(path)
        manifest_hash = read_manifest_hash(path)
    except OSError as e:
        raise SourceError(f"cannot read source {str(path)!r}: {e}") from e
    except (McapError, ValueError, KeyError, EOFError) as e:
        if isinstance(e, SourceError):
            raise
        raise SourceError(f"source {str(path)!r} is not a readable "
                          f"Recording: {e}") from e
    return Source(path, digest, size, manifest_hash, channels, identity)


def _identity(path: Path) -> tuple:
    stat = os.stat(path)
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _sha256(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as stream:
        while block := stream.read(1 << 20):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _check_finalized(path: Path, size: int) -> None:
    with open(path, "rb") as stream:
        head = stream.read(len(_MAGIC))
        stream.seek(max(0, size - len(_MAGIC)))
        tail = stream.read(len(_MAGIC))
    if head != _MAGIC:
        raise SourceError(f"source {str(path)!r} is not an MCAP file")
    if size < 2 * len(_MAGIC) or tail != _MAGIC:
        raise SourceError(f"source {str(path)!r} is truncated: it does not "
                          "end with the MCAP footer magic")


def _channels(path: Path) -> tuple[SourceChannel, ...]:
    with open(path, "rb") as stream:
        summary = make_reader(stream).get_summary()
    if summary is None:
        raise SourceError(f"source {str(path)!r} has no summary section")
    channels, names = [], set()
    for channel in summary.channels.values():
        if channel.topic in names:
            raise SourceError(f"channel {channel.topic!r} is declared twice")
        names.add(channel.topic)
        schema = summary.schemas.get(channel.schema_id)
        if schema is None:
            raise SourceError(f"channel {channel.topic!r} has no schema")
        for role, encoding in (("message", channel.message_encoding),
                               ("schema", schema.encoding)):
            if encoding != SOURCE_ENCODING:
                raise SourceError(
                    f"channel {channel.topic!r} has unsupported {role} "
                    f"encoding {encoding!r}; only {SOURCE_ENCODING!r} is "
                    "supported")
        try:
            declaration = json.loads(schema.data)
        except ValueError as e:
            raise SourceError(f"schema {schema.name!r} is not JSON: {e}") from e
        channels.append(SourceChannel(channel.topic, schema.name, declaration,
                                      hashlib.sha256(schema.data).hexdigest()))
    return tuple(channels)


def select_channels(recording: Source, channels: Collection[str] | None
                    ) -> tuple[SourceChannel, ...]:
    """The named source Channels in declaration order; None selects all."""
    if channels is None:
        return recording.channels
    names = list(channels)
    if not names:
        raise SelectionError("the Channel selection is empty")
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise SelectionError(f"channel {duplicates[0]!r} is selected twice")
    known = {channel.name for channel in recording.channels}
    unknown = [name for name in names if name not in known]
    if unknown:
        raise SelectionError(f"channel {unknown[0]!r} is not in the source "
                             f"{str(recording.path)!r}")
    return tuple(c for c in recording.channels if c.name in set(names))


def completeness(recording: Source) -> dict:
    """What is known about the Run that wrote the source. Message coverage
    alone does not prove that the Run completed."""
    known = {"source_finalized": True, "run_outcome": "unknown",
             "run_exit_code": None, "evidence": None,
             "note": "Message coverage does not prove Run completion"}
    sidecar = recording.path.with_name(recording.path.name + ".provenance.json")
    try:
        document = json.loads(sidecar.read_text())
        bound = document["recording"]["sha256"] == recording.sha256
        exit_code = document["run_exit_code"]
    except (OSError, ValueError, KeyError, TypeError):
        return known
    if not bound or isinstance(exit_code, bool) or not isinstance(exit_code, int):
        return known
    return {**known, "run_outcome": "completed" if exit_code == 0 else "failed",
            "run_exit_code": exit_code, "evidence": sidecar.name}
