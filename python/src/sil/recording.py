"""Recording-format seam on the Python read side.

The kernel selects a recording format from the output extension; the Python
reader used to decode a run's output mirrors that seam so an unrecognized
extension fails with a clear error here too, rather than handing an arbitrary
file to an MCAP decoder. MCAP is the only format in v1.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Iterator, NamedTuple

if TYPE_CHECKING:
    from mcap.records import Channel, ChunkIndex, Message
    from mcap.summary import Summary


class UnknownRecordingFormat(ValueError):
    """Raised for a recording path whose extension names no known format."""


class RecordedChannel(NamedTuple):
    """A Channel the Recording declares, with its Schema."""

    name: str
    schema_name: str
    schema: dict


def read_records(path: str | Path, *, channels: Collection[str] | None = None,
                 start_ns: int | None = None, end_ns: int | None = None,
                 ) -> Iterator[tuple[str, int, bytes]]:
    """Yields (topic, log_time_ns, data) for every recorded message, in stored
    order. Dispatches on the path extension; raises UnknownRecordingFormat for
    an unrecognized one.

    `channels` keeps only those Channels, and `start_ns` and `end_ns` keep
    only the log times in [start_ns, end_ns). The MCAP index applies the
    filters, so the reader holds one chunk of a long Recording at a time."""
    path = Path(path)
    if path.suffix == ".mcap":
        return _read_mcap(path, channels, start_ns, end_ns)
    raise UnknownRecordingFormat(
        f"unrecognized recording format {path.suffix!r} for recording "
        f"{str(path)!r}"
    )


def read_schemas(path: str | Path) -> dict[str, dict]:
    """The schema declaration of every recorded Channel, by Channel name.

    Dispatches like `read_records`. A Channel with no Messages is still
    listed, because the Recording declares it."""
    return {channel.name: channel.schema for channel in read_channels(path)}


def read_channels(path: str | Path) -> list[RecordedChannel]:
    """Every Channel the Recording declares, in declaration order.

    Dispatches like `read_records`. A Channel with no Messages is still
    listed."""
    path = Path(path)
    if path.suffix != ".mcap":
        # read_records states the refusal.
        read_records(path)
    from mcap.reader import make_reader

    with open(path, "rb") as f:
        summary = make_reader(f).get_summary()
    if summary is None:
        raise ValueError(f"recording {str(path)!r} has no summary section")
    channels = []
    for channel in summary.channels.values():
        schema = summary.schemas[channel.schema_id]
        channels.append(RecordedChannel(channel.topic, schema.name,
                                        json.loads(schema.data)))
    return channels


def read_manifest_hash(path: str | Path) -> str | None:
    """The hash of the Manifest whose Run wrote the Recording, or None for a
    Recording no Run wrote, such as one converted at the edge.

    Dispatches like `read_records`."""
    for name, metadata in read_metadata(path):
        if name == "sil" and "manifest_hash" in metadata:
            return metadata["manifest_hash"]
    return None


def read_metadata(path: str | Path) -> list[tuple[str, dict[str, str]]]:
    """Every metadata record of the Recording as (name, metadata), in stored
    order.

    Dispatches like `read_records`."""
    path = Path(path)
    if path.suffix != ".mcap":
        # read_records states the refusal.
        read_records(path)
    from mcap.reader import make_reader

    with open(path, "rb") as f:
        return [(record.name, dict(record.metadata))
                for record in make_reader(f).iter_metadata()]


@dataclass(frozen=True)
class _RecordFilter:
    channels: frozenset[str] | None
    start_ns: int | None
    end_ns: int | None

    def matches_chunk(self, index: ChunkIndex, channels: dict[int, Channel]) -> bool:
        if self.start_ns is not None and index.message_end_time < self.start_ns:
            return False
        if self.end_ns is not None and index.message_start_time >= self.end_ns:
            return False
        return self.channels is None or not index.message_index_offsets or any(
            channels[id_].topic in self.channels for id_ in index.message_index_offsets)

    def matches_message(self, channel: Channel, message: Message) -> bool:
        return ((self.channels is None or channel.topic in self.channels)
                and (self.start_ns is None or message.log_time >= self.start_ns)
                and (self.end_ns is None or message.log_time < self.end_ns))


def _unindexed_records(stream: BinaryIO, selected: _RecordFilter
                       ) -> Iterator[tuple[str, int, bytes]]:
    from mcap.reader import NonSeekingReader

    stream.seek(0)
    topics = None if selected.channels is None else list(selected.channels)
    for _, channel, message in NonSeekingReader(stream).iter_messages(
            topics=topics, start_time=selected.start_ns, end_time=selected.end_ns,
            log_time_order=False):
        yield channel.topic, message.log_time, message.data


def _indexed_records(stream: BinaryIO, summary: Summary, selected: _RecordFilter
                     ) -> Iterator[tuple[str, int, bytes]]:
    from mcap.records import Chunk, Message
    from mcap.stream_reader import StreamReader, breakup_chunk

    # The seeking stored-order iterator retains all selected Messages before
    # yielding. Visit chunks by physical file offset to keep Publish order.
    for index in sorted(summary.chunk_indexes, key=lambda c: c.chunk_start_offset):
        if not selected.matches_chunk(index, summary.channels):
            continue
        stream.seek(index.chunk_start_offset)
        chunk = next(StreamReader(stream, skip_magic=True, emit_chunks=True).records)
        if not isinstance(chunk, Chunk):
            raise ValueError("Recording chunk index does not point to a Chunk")
        for record in breakup_chunk(chunk):
            if isinstance(record, Message):
                channel = summary.channels[record.channel_id]
                if selected.matches_message(channel, record):
                    yield channel.topic, record.log_time, record.data


def _read_mcap(path: Path, channels: Collection[str] | None,
               start_ns: int | None, end_ns: int | None,
               ) -> Iterator[tuple[str, int, bytes]]:
    from mcap.reader import make_reader

    selected = _RecordFilter(None if channels is None else frozenset(channels),
                             start_ns, end_ns)
    with open(path, "rb") as f:
        summary = make_reader(f).get_summary()
        if summary is None or not summary.chunk_indexes:
            yield from _unindexed_records(f, selected)
        else:
            yield from _indexed_records(f, summary, selected)
