"""Indexed navigation of a finalized Recording (spec #345 s1 and s3).

`prepare_navigation` hashes the source and builds, or reuses, a disposable
SQLite index of Message locations. The returned `Navigator` is the source
interface that playback controllers and tool adapters read:

- `describe()`: source identity, Channel catalog, bounds and completeness.
- `messages(channels, start_ns, end_ns, continuation, limits)`: one bounded
  page of the Messages in [start_ns, end_ns), ordered by
  (publication_time_ns, record_index).
- `snapshot(channels, time_ns)`: the latest Message per Channel at or before
  `time_ns`, or None for a Channel with no Message up to then.

Payloads stay in the MCAP; the index holds only locations. `record_index` is
the whole-source stored-order ordinal (see `sil.visualization.source`).
Times are integer nanoseconds over the full unsigned 64-bit range.

The index is keyed by source SHA-256 and INDEX_VERSION, written to a
temporary file and published with an atomic rename only when complete.
Reuse verifies the digest again. The source file identity (device, inode,
size, mtime) is checked before and after preparation and around every read;
an observed change is refused with SourceError. A change that keeps all four
is not detected.

Memory is bounded by `Limits`: at most one decoded chunk of
`max_decoded_bytes` is read at a time, decoded chunks are cached up to
`chunk_cache_bytes`, and no Message payload above `max_message_bytes` is
read. Sizes are checked before allocation or decompression. SQLite page
caches and decoder metadata come in addition, so memory use is not constant.

A Navigator is not safe for concurrent use; use it from one thread at a time.
"""

from __future__ import annotations

import base64
import binascii
import heapq
import itertools
import json
import os
import sqlite3
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path

from mcap.reader import make_reader

from sil import recording
from sil.recording import OversizeMessage
from sil.visualization.source import (Source, SourceChannel, SourceError,
                                      SourceMessage, completeness, open_source,
                                      select_channels)

INDEX_VERSION = 1
CONTINUATION_VERSION = 1
ORDER = "publication_time_ns,record_index"
MIB = 1 << 20
_TIME_LIMIT = 1 << 64  # exclusive
_TIME_SHIFT = 1 << 63  # stores unsigned 64-bit times in signed SQLite integers
_PAGE_SIZE = 4096


class NavigationError(SourceError):
    """Navigation refused a request or a source."""


class InvalidRequest(NavigationError):
    """A time, interval or limit is out of range. Nothing was read."""


class ContinuationError(InvalidRequest):
    """A continuation is malformed or belongs to another source, index
    version, Channel selection, interval or order."""


class LimitExceeded(NavigationError):
    """A chunk, Message or page row exceeds a configured limit. It is
    refused, never silently skipped.

    For a refused Message, `record_index`, `channel`, `publication_time_ns`
    and `size_bytes` name it, so a reader can report it and resume after it
    with `Navigator.continuation_after`. They are None otherwise."""

    def __init__(self, message: str, *, record_index: int | None = None,
                 channel: str | None = None,
                 publication_time_ns: int | None = None,
                 size_bytes: int | None = None):
        super().__init__(message)
        self.record_index = record_index
        self.channel = channel
        self.publication_time_ns = publication_time_ns
        self.size_bytes = size_bytes


class PreparationCancelled(NavigationError):
    """Preparation stopped on request. No index was published."""


class IndexCorrupt(NavigationError):
    """The published index does not match the source it names."""


class IndexStorageError(OSError):
    """The index could not be written, for example because the disk or the
    `max_index_bytes` budget is full. No index was published."""


@dataclass(frozen=True)
class Limits:
    """Source read bounds. `max_index_bytes` caps the index file; None
    leaves it to the free disk space."""

    max_decoded_bytes: int = 64 * MIB
    chunk_cache_bytes: int = 128 * MIB
    max_message_bytes: int = 8 * MIB
    max_index_bytes: int | None = None

    def __post_init__(self):
        for name in ("max_decoded_bytes", "chunk_cache_bytes",
                     "max_message_bytes"):
            _require_positive(name, getattr(self, name))
        if self.max_index_bytes is not None:
            _require_positive("max_index_bytes", self.max_index_bytes)
        if self.chunk_cache_bytes < self.max_decoded_bytes:
            raise InvalidRequest("chunk_cache_bytes must hold one decoded "
                                 "chunk of max_decoded_bytes")


@dataclass(frozen=True)
class PageLimits:
    """Bounds of one `messages` page: payload rows and payload bytes."""

    max_rows: int = 1000
    max_bytes: int = 8 * MIB

    def __post_init__(self):
        _require_positive("max_rows", self.max_rows)
        _require_positive("max_bytes", self.max_bytes)


@dataclass(frozen=True)
class Progress:
    scanned_bytes: int
    total_bytes: int
    messages: int


@dataclass(frozen=True)
class ChannelSummary:
    """A source Channel. `start_ns` and `end_ns` are its first and last
    publication times, inclusive, or None without Messages."""

    name: str
    schema_name: str
    schema: dict
    schema_sha256: str
    message_count: int
    start_ns: int | None
    end_ns: int | None


@dataclass(frozen=True)
class IndexInfo:
    path: Path
    version: int
    size_bytes: int
    reused: bool


@dataclass(frozen=True)
class Description:
    """`start_ns` and `end_ns` bound all Messages, inclusive; both are None
    for an empty source, which has no playback range."""

    source: Source
    channels: tuple[ChannelSummary, ...]
    message_count: int
    start_ns: int | None
    end_ns: int | None
    completeness: dict
    index: IndexInfo


@dataclass(frozen=True)
class Page:
    """Messages in (publication_time_ns, record_index) order. `complete` is
    True when no Message of the request follows; otherwise `continuation`
    resumes after the last Message."""

    messages: tuple[SourceMessage, ...]
    continuation: str | None
    complete: bool


def prepare_navigation(source: str | Path, *, cache_dir: str | Path,
                       limits: Limits | None = None,
                       progress: Callable[[Progress], None] | None = None,
                       cancel: threading.Event | None = None) -> Navigator:
    """Validate and hash `source`, then reuse or build its index in
    `cache_dir`.

    `progress` is called after each scanned chunk. Setting `cancel` stops a
    build with PreparationCancelled. Raises SourceError for a source that is
    not a finalized supported Recording or that changes, LimitExceeded for
    an oversize chunk and IndexStorageError when the index cannot be
    written."""
    limits = limits or Limits()
    opened = open_source(source)
    ids = _channel_ids(opened)
    cache_dir = Path(cache_dir)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise IndexStorageError(f"cannot create index directory "
                                f"{str(cache_dir)!r}: {e}") from e
    index_path = cache_dir / f"{opened.sha256}.v{INDEX_VERSION}.sqlite"
    connection = _reuse(index_path, opened)
    reused = connection is not None
    if connection is None:
        _build(opened, ids, index_path, limits, progress, cancel)
        connection = _reuse(index_path, opened)
        if connection is None:
            raise IndexCorrupt(f"index {str(index_path)!r} does not verify "
                               "after it was built")
    try:
        opened.check_unchanged()
    except SourceError:
        connection.close()
        raise
    return Navigator(opened, ids, connection, index_path, limits, reused)


class Navigator:
    """The source interface over one prepared Recording."""

    def __init__(self, source: Source, ids: dict[str, int],
                 connection: sqlite3.Connection, index_path: Path,
                 limits: Limits, reused: bool):
        self._source = source
        self._ids = ids
        self._names = {id_: name for name, id_ in ids.items()}
        self._db = connection
        self._index_path = index_path
        self._limits = limits
        self._reused = reused
        self._stream = open(source.path, "rb")
        self._chunks: OrderedDict[int, bytes] = OrderedDict()
        self._cached_bytes = 0
        self._catalog = {name: (count, _time(first), _time(last))
                         for name, count, first, last in self._query(
                             "SELECT name, messages, first, last FROM channels")}

    def describe(self) -> Description:
        channels = tuple(ChannelSummary(c.name, c.schema_name, c.schema,
                                        c.schema_sha256, *self._catalog[c.name])
                         for c in self._source.channels)
        firsts = [c.start_ns for c in channels if c.start_ns is not None]
        lasts = [c.end_ns for c in channels if c.end_ns is not None]
        return Description(
            self._source, channels, sum(c.message_count for c in channels),
            min(firsts, default=None), max(lasts, default=None),
            completeness(self._source),
            IndexInfo(self._index_path, INDEX_VERSION,
                      os.stat(self._index_path).st_size, self._reused))

    def messages(self, channels: Collection[str] | None, start_ns: int,
                 end_ns: int, continuation: str | None = None,
                 limits: PageLimits | None = None) -> Page:
        """One page of the Messages of `channels` (None: all) published in
        [start_ns, end_ns). A Message larger than the page byte limit or
        `max_message_bytes` raises LimitExceeded when it would come first
        on a page."""
        page = limits or PageLimits()
        selected = select_channels(self._source, channels)
        _require_time("start_ns", start_ns, _TIME_LIMIT)
        _require_time("end_ns", end_ns, _TIME_LIMIT)
        if start_ns > end_ns:
            raise InvalidRequest(f"start_ns {start_ns} is after end_ns {end_ns}")
        pin = self._pin(selected, start_ns, end_ns)
        after = ((start_ns, -1) if continuation is None
                 else _resume(continuation, pin, start_ns, end_ns))
        self._source.check_unchanged()
        rows = ([] if start_ns == end_ns else
                self._rows_after(selected, after, end_ns - 1, page.max_rows + 1))
        taken, used = [], 0
        for row in rows[:page.max_rows]:
            size = row[5]
            if size > min(page.max_bytes, self._limits.max_message_bytes):
                if not taken:
                    raise self._refusal(
                        row, f"the page limit is {page.max_bytes} and the "
                        f"Message limit {self._limits.max_message_bytes}")
                break
            if used + size > page.max_bytes:
                break
            taken.append(row)
            used += size
        messages = tuple(self._message(row) for row in taken)
        self._source.check_unchanged()
        if len(taken) == len(rows):
            return Page(messages, None, True)
        return Page(messages, _continuation(pin, messages[-1].publication_time_ns,
                                           messages[-1].record_index), False)

    def continuation_after(self, channels: Collection[str] | None,
                           start_ns: int, end_ns: int, time_ns: int,
                           record_index: int) -> str:
        """A continuation of `messages(channels, start_ns, end_ns)` that
        resumes strictly after (time_ns, record_index). With record_index
        `2**63 - 1` it resumes after every Message at `time_ns`. A reader
        uses it to continue from its own position, for example after a
        Message refused with LimitExceeded."""
        selected = select_channels(self._source, channels)
        _require_time("start_ns", start_ns, _TIME_LIMIT)
        _require_time("end_ns", end_ns, _TIME_LIMIT)
        _require_time("time_ns", time_ns, _TIME_LIMIT - 1)
        _require_time("record_index", record_index, _TIME_SHIFT - 1)
        if not start_ns <= time_ns < end_ns:
            raise ContinuationError("the continuation is outside the interval")
        return _continuation(self._pin(selected, start_ns, end_ns),
                             time_ns, record_index)

    def snapshot(self, channels: Collection[str] | None, time_ns: int
                 ) -> dict[str, SourceMessage | None]:
        """The latest Message of each selected Channel published at or
        before `time_ns`; equal times resolve to the highest record_index.
        A Channel without such a Message maps to None, never to a value."""
        selected = select_channels(self._source, channels)
        _require_time("time_ns", time_ns, _TIME_LIMIT - 1)
        self._source.check_unchanged()
        rows = {channel.name: self._query(
            _SELECT + " WHERE channel = ? AND time <= ? "
            "ORDER BY time DESC, record_index DESC LIMIT 1",
            (self._ids[channel.name], time_ns - _TIME_SHIFT)).fetchone()
            for channel in selected}
        result = {name: None if row is None else self._message(row)
                  for name, row in rows.items()}
        self._source.check_unchanged()
        return result

    def close(self) -> None:
        self._stream.close()
        self._db.close()
        self._chunks.clear()
        self._cached_bytes = 0

    def __enter__(self) -> Navigator:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _pin(self, selected: tuple[SourceChannel, ...], start_ns: int,
             end_ns: int) -> dict:
        return {"continuation": CONTINUATION_VERSION,
                "index_version": INDEX_VERSION,
                "source_sha256": self._source.sha256,
                "channels": [channel.name for channel in selected],
                "start_ns": str(start_ns), "end_ns": str(end_ns),
                "order": ORDER}

    def _rows_after(self, selected: tuple[SourceChannel, ...],
                    after: tuple[int, int], last_ns: int, count: int) -> list:
        """Up to `count` rows after `after` = (time, record_index) and at or
        before `last_ns`, in (time, record_index) order."""
        bounds = (after[0] - _TIME_SHIFT, after[1], last_ns - _TIME_SHIFT, count)
        order = " AND time <= ? ORDER BY time, record_index LIMIT ?"
        if len(selected) == len(self._source.channels):
            return self._query(_SELECT + " WHERE (time, record_index) > (?, ?)"
                               + order, bounds).fetchall()
        # One range per Channel on (channel, time, record_index), merged, so
        # a sparse selection does not scan the other Channels' rows.
        ranges = [self._query(_SELECT + " WHERE channel = ? AND "
                              "(time, record_index) > (?, ?)" + order,
                              (self._ids[channel.name], *bounds)).fetchall()
                  for channel in selected]
        return list(itertools.islice(
            heapq.merge(*ranges, key=lambda row: (row[2], row[0])), count))

    def _message(self, row) -> SourceMessage:
        record_index, channel_id, time, chunk, offset, size = row
        if size > self._limits.max_message_bytes:
            raise self._refusal(
                row, f"the limit is {self._limits.max_message_bytes}")
        try:
            if chunk < 0:
                found = recording.read_message(
                    self._stream, offset,
                    max_message_bytes=self._limits.max_message_bytes)
            else:
                found = recording.message_in_chunk(self._chunk(chunk), offset)
        except OversizeMessage as e:
            raise LimitExceeded(str(e)) from e
        except (ValueError, EOFError) as e:
            raise IndexCorrupt(f"index location of record_index "
                               f"{record_index} is wrong: {e}") from e
        if found[:2] != (channel_id, time + _TIME_SHIFT) or len(found[2]) != size:
            raise IndexCorrupt(f"index location of record_index {record_index} "
                               "names another Message")
        return SourceMessage(record_index, self._names[channel_id],
                             time + _TIME_SHIFT, found[2])

    def _refusal(self, row, limit: str) -> LimitExceeded:
        record_index, channel_id, time, _, _, size = row
        return LimitExceeded(
            f"Message record_index {record_index} has {size} bytes; {limit}",
            record_index=record_index, channel=self._names[channel_id],
            publication_time_ns=time + _TIME_SHIFT, size_bytes=size)

    def _chunk(self, offset: int) -> bytes:
        records = self._chunks.pop(offset, None)
        if records is None:
            records = recording.read_chunk(
                self._stream, offset,
                max_chunk_bytes=self._limits.max_decoded_bytes)
            self._cached_bytes += len(records)
        self._chunks[offset] = records
        while self._cached_bytes > self._limits.chunk_cache_bytes:
            _, evicted = self._chunks.popitem(last=False)
            self._cached_bytes -= len(evicted)
        return records

    def _query(self, sql: str, parameters=()) -> sqlite3.Cursor:
        try:
            return self._db.execute(sql, parameters)
        except sqlite3.DatabaseError as e:
            raise IndexCorrupt(f"index {str(self._index_path)!r} is "
                               f"unreadable: {e}") from e


_SELECT = "SELECT record_index, channel, time, chunk, offset, size FROM messages"
_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE channels(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                      messages INTEGER NOT NULL, first INTEGER, last INTEGER);
CREATE TABLE messages(record_index INTEGER PRIMARY KEY,
                      channel INTEGER NOT NULL, time INTEGER NOT NULL,
                      chunk INTEGER NOT NULL, offset INTEGER NOT NULL,
                      size INTEGER NOT NULL);
"""
_INDEXES = """
CREATE INDEX messages_by_channel_time ON messages(channel, time, record_index);
CREATE INDEX messages_by_time ON messages(time, record_index);
"""


def _channel_ids(source: Source) -> dict[str, int]:
    try:
        with open(source.path, "rb") as stream:
            summary = make_reader(stream).get_summary()
    except OSError as e:
        raise SourceError(f"cannot read source {str(source.path)!r}: {e}") from e
    return {channel.topic: id_ for id_, channel in summary.channels.items()}


def _reuse(index_path: Path, source: Source) -> sqlite3.Connection | None:
    """The published index for `source`, opened read-only, or None after
    removing an index that is missing, stale or unreadable."""
    if not index_path.exists():
        return None
    connection = sqlite3.connect(f"{index_path.resolve().as_uri()}?mode=ro",
                                 uri=True, check_same_thread=False)
    try:
        meta = dict(connection.execute("SELECT key, value FROM meta"))
        (last,) = connection.execute(
            "SELECT max(record_index) FROM messages").fetchone()
        names = {name for (name,) in connection.execute(
            "SELECT name FROM channels")}
        valid = (meta.get("index_version") == str(INDEX_VERSION)
                 and meta.get("source_sha256") == source.sha256
                 and meta.get("complete") == "1"
                 and meta.get("message_count") == str(
                     0 if last is None else last + 1)
                 and names == {channel.name for channel in source.channels})
    except (sqlite3.Error, ValueError):
        valid = False
    if valid:
        return connection
    connection.close()
    index_path.unlink(missing_ok=True)
    return None


def _build(source: Source, ids: dict[str, int], index_path: Path,
           limits: Limits, progress: Callable[[Progress], None] | None,
           cancel: threading.Event | None) -> None:
    """Write the index to a temporary file and publish it by rename."""
    try:
        handle, name = tempfile.mkstemp(dir=index_path.parent,
                                        prefix=f".{index_path.name}.",
                                        suffix=".tmp")
    except OSError as e:
        raise IndexStorageError(f"cannot write index in "
                                f"{str(index_path.parent)!r}: {e}") from e
    os.close(handle)
    temporary = Path(name)
    try:
        connection = sqlite3.connect(temporary)
        try:
            _configure(connection, limits, cancel)
            connection.executescript(_SCHEMA)
            count = _insert_locations(connection, source, ids, limits,
                                      progress, cancel)
            connection.executescript(_INDEXES)
            _write_catalog(connection, source, ids, count)
            connection.commit()
        finally:
            connection.close()
        _check_cancel(cancel)
        source.check_unchanged()
        with open(temporary, "rb") as published:
            os.fsync(published.fileno())
        os.replace(temporary, index_path)
    except SourceError:
        raise
    except OversizeMessage as e:
        raise LimitExceeded(str(e)) from e
    except sqlite3.Error as e:
        if cancel is not None and cancel.is_set():
            raise PreparationCancelled("index preparation was cancelled") from e
        raise IndexStorageError(f"cannot write index {str(index_path)!r}: "
                                f"{e}") from e
    except (ValueError, EOFError, KeyError) as e:
        raise SourceError(f"source {str(source.path)!r} has an unreadable "
                          f"record: {e}") from e
    except OSError as e:
        raise IndexStorageError(f"cannot write index {str(index_path)!r}: "
                                f"{e}") from e
    finally:
        temporary.unlink(missing_ok=True)


def _configure(connection: sqlite3.Connection, limits: Limits,
               cancel: threading.Event | None) -> None:
    # The file is disposable and published only when complete, so the
    # build needs no journal or intermediate syncs.
    connection.execute(f"PRAGMA page_size = {_PAGE_SIZE}")
    connection.execute("PRAGMA journal_mode = OFF")
    connection.execute("PRAGMA synchronous = OFF")
    connection.execute("PRAGMA cache_size = -65536")  # KiB
    if limits.max_index_bytes is not None:
        connection.execute(
            f"PRAGMA max_page_count = {max(1, limits.max_index_bytes // _PAGE_SIZE)}")
    if cancel is not None:
        # Interrupts long statements such as CREATE INDEX.
        connection.set_progress_handler(lambda: int(cancel.is_set()), 10_000)


def _insert_locations(connection: sqlite3.Connection, source: Source,
                      ids: dict[str, int], limits: Limits,
                      progress: Callable[[Progress], None] | None,
                      cancel: threading.Event | None) -> int:
    known = set(ids.values())
    count = 0
    for scanned, located in recording.locate_messages(
            source.path, max_chunk_bytes=limits.max_decoded_bytes):
        _check_cancel(cancel)
        unknown = {location.channel_id for location in located} - known
        if unknown:
            raise SourceError(f"source {str(source.path)!r} has a Message on "
                              f"undeclared channel id {min(unknown)}")
        connection.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)",
            [(m.record_index, m.channel_id, m.log_time - _TIME_SHIFT,
              m.chunk_offset, m.offset, m.size) for m in located])
        count += len(located)
        if progress is not None:
            progress(Progress(scanned, source.size_bytes, count))
    return count


def _check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise PreparationCancelled("index preparation was cancelled")


def _write_catalog(connection: sqlite3.Connection, source: Source,
                   ids: dict[str, int], count: int) -> None:
    stats = {row[0]: row[1:] for row in connection.execute(
        "SELECT channel, count(*), min(time), max(time) FROM messages "
        "GROUP BY channel")}
    connection.executemany(
        "INSERT INTO channels VALUES (?, ?, ?, ?, ?)",
        [(ids[c.name], c.name, *stats.get(ids[c.name], (0, None, None)))
         for c in source.channels])
    connection.executemany("INSERT INTO meta VALUES (?, ?)", [
        ("index_version", str(INDEX_VERSION)),
        ("source_sha256", source.sha256),
        ("message_count", str(count)),
        ("complete", "1"),
    ])


def _resume(token: str, pin: dict, start_ns: int, end_ns: int
            ) -> tuple[int, int]:
    """The (time, record_index) after which a continuation resumes."""
    try:
        body = json.loads(base64.b64decode(token.encode("ascii"),
                                           altchars=b"-_", validate=True))
    except (AttributeError, UnicodeError, binascii.Error, ValueError) as e:
        raise ContinuationError("the continuation is malformed") from e
    if not isinstance(body, dict) or set(body) != {*pin, "after"}:
        raise ContinuationError("the continuation is malformed")
    for key, expected in pin.items():
        if body[key] != expected:
            raise ContinuationError(f"the continuation was issued for another "
                                    f"{key}; restart without it")
    after = body["after"]
    if (not isinstance(after, list) or len(after) != 2
            or not all(_is_decimal(part) for part in after)):
        raise ContinuationError("the continuation is malformed")
    time_ns, record_index = int(after[0]), int(after[1])
    if not start_ns <= time_ns < end_ns:
        raise ContinuationError("the continuation is outside the interval")
    return time_ns, record_index


def _continuation(pin: dict, time_ns: int, record_index: int) -> str:
    body = {**pin, "after": [str(time_ns), str(record_index)]}
    return base64.urlsafe_b64encode(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).decode("ascii")


def _is_decimal(value) -> bool:
    return (isinstance(value, str) and value.isascii() and value.isdigit()
            and value == str(int(value)))


def _time(stored: int | None) -> int | None:
    return None if stored is None else stored + _TIME_SHIFT


def _require_time(name: str, value, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRequest(f"{name} must be an integer, not {value!r}")
    if not 0 <= value <= maximum:
        raise InvalidRequest(f"{name} {value} is outside 0..{maximum}")


def _require_positive(name: str, value) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InvalidRequest(f"{name} must be a positive integer, not {value!r}")
