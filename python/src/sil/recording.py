"""Recording-format seam on the Python read side.

The kernel selects a recording format from the output extension; the Python
reader used to decode a run's output mirrors that seam so an unrecognized
extension fails with a clear error here too, rather than handing an arbitrary
file to an MCAP decoder. MCAP is the only format in v1.
"""

from __future__ import annotations

import itertools
import json
import struct
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


class OversizeMessage(ValueError):
    """A chunk or Message is larger than the caller's limit. It is refused
    before its bytes are read or decompressed."""


class MessageLocation(NamedTuple):
    """Where one Message is stored in an MCAP file.

    `record_index` is the Message's ordinal in the whole file's stored order.
    `chunk_offset` is the file offset of the Chunk record that holds it, or
    -1 for a Message outside any chunk. `offset` is the offset of the Message
    record in the decompressed chunk records, or in the file for a Message
    outside any chunk. `size` is the length of its payload."""

    record_index: int
    channel_id: int
    log_time: int
    chunk_offset: int
    offset: int
    size: int


_MCAP_MAGIC = b"\x89MCAP0\r\n"
_OP_MESSAGE = 0x05
_OP_CHUNK = 0x06
_OP_DATA_END = 0x0F
_RECORD_HEAD = struct.Struct("<BQ")
_MESSAGE_HEAD = struct.Struct("<HIQQ")  # channel, sequence, log, publish time
_CHUNK_HEAD = struct.Struct("<QQQII")  # start, end, size, crc, compression len
_MAX_COMPRESSION_NAME = 64


LOCATION_BATCH = 1 << 16


def locate_messages(path: str | Path, *, max_chunk_bytes: int,
                    batch_size: int = LOCATION_BATCH
                    ) -> Iterator[tuple[int, list[MessageLocation]]]:
    """Every Message of an MCAP file in stored order, with its location.

    Yields (bytes scanned so far, locations) in batches of at most
    `batch_size` locations; a batch never spans two chunks. It reads the
    data section in physical order, as `stored_messages` does, and holds one
    chunk at a time. A chunk whose compressed or decompressed size exceeds
    `max_chunk_bytes` raises OversizeMessage before it is read."""
    scan = _scan(path, max_chunk_bytes, payloads=False)
    for scanned, group in itertools.groupby(scan, key=lambda found: found[0]):
        locations = (location for _, location, _ in group)
        while batch := list(itertools.islice(locations, batch_size)):
            yield scanned, batch


def stored_messages(path: str | Path, *, max_chunk_bytes: int
                    ) -> Iterator[tuple[MessageLocation, bytes]]:
    """Every Message of an MCAP file with its payload, in stored order.

    Chunked and top-level Messages are both visited, in physical order, so
    `record_index` is the same whole-file ordinal as in `locate_messages`.
    Chunks are bounded by `max_chunk_bytes` as there."""
    for _, location, payload in _scan(path, max_chunk_bytes, payloads=True):
        yield location, payload


def _scan(path: str | Path, max_chunk_bytes: int, payloads: bool
          ) -> Iterator[tuple[int, MessageLocation, bytes | None]]:
    """(end offset of the record that holds it, location, payload or None)
    of every Message, in physical order."""
    record_index = 0
    with open(path, "rb") as stream:
        if stream.read(len(_MCAP_MAGIC)) != _MCAP_MAGIC:
            raise ValueError(f"{str(path)!r} is not an MCAP file")
        while True:
            start = stream.tell()
            opcode, length = _read_head(stream)
            end = start + _RECORD_HEAD.size + length
            if opcode == _OP_DATA_END:
                return
            if opcode == _OP_CHUNK:
                records = _read_chunk_records(stream, start, length,
                                              max_chunk_bytes)
                found = _messages_in_chunk(records, start, record_index,
                                           payloads)
            elif opcode == _OP_MESSAGE:
                found = [_top_level_message(stream, start, length,
                                            record_index, payloads)]
            else:
                stream.seek(length, 1)
                continue
            for location, payload in found:
                record_index += 1
                yield end, location, payload
            stream.seek(end)


def read_chunk(stream: BinaryIO, chunk_offset: int, *, max_chunk_bytes: int
               ) -> bytes:
    """The decompressed records of the Chunk record at `chunk_offset`.

    Refuses with OversizeMessage a chunk larger than `max_chunk_bytes`,
    compressed or decompressed, before it is read."""
    stream.seek(chunk_offset)
    opcode, length = _read_head(stream)
    if opcode != _OP_CHUNK:
        raise ValueError(f"no Chunk record at file offset {chunk_offset}")
    return _read_chunk_records(stream, chunk_offset, length, max_chunk_bytes)


def message_in_chunk(records: bytes, offset: int) -> tuple[int, int, bytes]:
    """(channel id, log time, payload) of the Message record at `offset` in
    decompressed chunk records."""
    if offset < 0 or offset + _RECORD_HEAD.size > len(records):
        raise ValueError(f"no Message record at chunk offset {offset}")
    opcode, length = _RECORD_HEAD.unpack_from(records, offset)
    end = offset + _RECORD_HEAD.size + length
    if opcode != _OP_MESSAGE or length < _MESSAGE_HEAD.size or end > len(records):
        raise ValueError(f"no Message record at chunk offset {offset}")
    channel_id, _, log_time, _ = _MESSAGE_HEAD.unpack_from(
        records, offset + _RECORD_HEAD.size)
    return channel_id, log_time, records[end - length + _MESSAGE_HEAD.size:end]


def read_message(stream: BinaryIO, offset: int, *, max_message_bytes: int
                 ) -> tuple[int, int, bytes]:
    """(channel id, log time, payload) of the Message record at file offset
    `offset`, outside any chunk."""
    stream.seek(offset)
    opcode, length = _read_head(stream)
    if opcode != _OP_MESSAGE or length < _MESSAGE_HEAD.size:
        raise ValueError(f"no Message record at file offset {offset}")
    size = length - _MESSAGE_HEAD.size
    if size > max_message_bytes:
        raise OversizeMessage(f"Message at file offset {offset} has {size} "
                              f"bytes; the limit is {max_message_bytes}")
    channel_id, _, log_time, _ = _MESSAGE_HEAD.unpack(
        _read_exact(stream, _MESSAGE_HEAD.size))
    return channel_id, log_time, _read_exact(stream, size)


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    data = stream.read(size)
    if len(data) != size:
        raise EOFError("the MCAP file ends inside a record")
    return data


def _read_head(stream: BinaryIO) -> tuple[int, int]:
    return _RECORD_HEAD.unpack(_read_exact(stream, _RECORD_HEAD.size))


def _read_chunk_records(stream: BinaryIO, chunk_offset: int, length: int,
                        max_chunk_bytes: int) -> bytes:
    """Decompress the Chunk record whose head was just read."""
    _, _, size, _, name_length = _CHUNK_HEAD.unpack(
        _read_exact(stream, _CHUNK_HEAD.size))
    if size > max_chunk_bytes:
        raise OversizeMessage(f"chunk at file offset {chunk_offset} "
                              f"decompresses to {size} bytes; the limit is "
                              f"{max_chunk_bytes}")
    if name_length > _MAX_COMPRESSION_NAME:
        raise ValueError(f"chunk at file offset {chunk_offset} has a "
                         f"{name_length}-byte compression name")
    compression = _read_exact(stream, name_length).decode()
    (stored,) = struct.unpack("<Q", _read_exact(stream, 8))
    if stored > max_chunk_bytes:
        raise OversizeMessage(f"chunk at file offset {chunk_offset} has "
                              f"{stored} compressed bytes; the limit is "
                              f"{max_chunk_bytes}")
    records = _decompress(_read_exact(stream, stored), compression, size)
    if len(records) != size:
        raise ValueError(f"chunk at file offset {chunk_offset} decompresses "
                         f"to {len(records)} bytes, not the declared {size}")
    return records


def _decompress(data: bytes, compression: str, size: int) -> bytes:
    """At most size + 1 bytes, so a frame that lies about its size cannot
    allocate more than the declared, already checked size."""
    if compression == "":
        return data
    if compression == "zstd":
        import io

        import zstandard

        with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data)) as r:
            return r.read(size + 1)
    if compression == "lz4":
        import lz4.frame

        return lz4.frame.LZ4FrameDecompressor().decompress(data, max_length=size + 1)
    raise ValueError(f"unsupported chunk compression {compression!r}")


def _messages_in_chunk(records: bytes, chunk_offset: int, record_index: int,
                       payloads: bool
                       ) -> Iterator[tuple[MessageLocation, bytes | None]]:
    position, end = 0, len(records)
    while position < end:
        if position + _RECORD_HEAD.size > end:
            raise ValueError(f"chunk at file offset {chunk_offset} ends "
                             "inside a record")
        opcode, length = _RECORD_HEAD.unpack_from(records, position)
        following = position + _RECORD_HEAD.size + length
        if opcode == _OP_MESSAGE:
            if length < _MESSAGE_HEAD.size or following > end:
                raise ValueError(f"short Message in chunk at {chunk_offset}")
            channel_id, _, log_time, _ = _MESSAGE_HEAD.unpack_from(
                records, position + _RECORD_HEAD.size)
            yield (MessageLocation(record_index, channel_id, log_time,
                                   chunk_offset, position,
                                   length - _MESSAGE_HEAD.size),
                   records[following - length + _MESSAGE_HEAD.size:following]
                   if payloads else None)
            record_index += 1
        position = following
    if position != end:
        raise ValueError(f"chunk at file offset {chunk_offset} ends inside "
                         "a record")


def _top_level_message(stream: BinaryIO, start: int, length: int,
                       record_index: int, payloads: bool
                       ) -> tuple[MessageLocation, bytes | None]:
    if length < _MESSAGE_HEAD.size:
        raise ValueError(f"short Message at file offset {start}")
    channel_id, _, log_time, _ = _MESSAGE_HEAD.unpack(
        _read_exact(stream, _MESSAGE_HEAD.size))
    size = length - _MESSAGE_HEAD.size
    return (MessageLocation(record_index, channel_id, log_time, -1, start, size),
            _read_exact(stream, size) if payloads else None)


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
