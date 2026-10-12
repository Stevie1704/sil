"""Recording-format seam on the Python read side.

The kernel selects a recording format from the output extension; the Python
reader used to decode a run's output mirrors that seam so an unrecognized
extension fails with a clear error here too, rather than handing an arbitrary
file to an MCAP decoder. MCAP is the only format in v1.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from pathlib import Path
from typing import Iterator, NamedTuple


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


def _read_mcap(path: Path, channels: Collection[str] | None,
               start_ns: int | None, end_ns: int | None,
               ) -> Iterator[tuple[str, int, bytes]]:
    from mcap.reader import NonSeekingReader, make_reader
    from mcap.records import Chunk, Message
    from mcap.stream_reader import StreamReader, breakup_chunk

    with open(path, "rb") as f:
        summary = make_reader(f).get_summary()
        if summary is None or not summary.chunk_indexes:
            f.seek(0)
            for _, channel, message in NonSeekingReader(f).iter_messages(
                    topics=channels, start_time=start_ns, end_time=end_ns,
                    log_time_order=False):
                yield channel.topic, message.log_time, message.data
            return

        # MCAP's seeking stored-order iterator queues all chunks before their
        # Messages, retaining every selected payload before its first yield.
        # Decode one indexed chunk at a time instead. Physical file order is
        # Publish order; timestamps must not reorder equal-time Messages.
        selected = None if channels is None else set(channels)
        for index in sorted(summary.chunk_indexes, key=lambda c: c.chunk_start_offset):
            if start_ns is not None and index.message_end_time < start_ns:
                continue
            if end_ns is not None and index.message_start_time >= end_ns:
                continue
            if selected is not None and index.message_index_offsets and not any(
                    summary.channels[id_].topic in selected
                    for id_ in index.message_index_offsets):
                continue
            f.seek(index.chunk_start_offset)
            chunk = next(StreamReader(f, skip_magic=True, emit_chunks=True).records)
            if not isinstance(chunk, Chunk):
                raise ValueError("Recording chunk index does not point to a Chunk")
            for record in breakup_chunk(chunk):
                if not isinstance(record, Message):
                    continue
                channel = summary.channels[record.channel_id]
                if selected is not None and channel.topic not in selected:
                    continue
                if start_ns is not None and record.log_time < start_ns:
                    continue
                if end_ns is not None and record.log_time >= end_ns:
                    continue
                yield channel.topic, record.log_time, record.data
