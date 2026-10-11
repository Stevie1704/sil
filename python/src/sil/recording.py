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


def read_records(path: str | Path, *, topics: Collection[str] | None = None,
                 start_ns: int | None = None, end_ns: int | None = None,
                 ) -> Iterator[tuple[str, int, bytes]]:
    """Yields (topic, log_time_ns, data) for every recorded message, in stored
    order. Dispatches on the path extension; raises UnknownRecordingFormat for
    an unrecognized one.

    `topics` keeps only those Channels, and `start_ns` and `end_ns` only the
    log times in [start_ns, end_ns). The MCAP index applies the filters, so
    a long Recording is read message by message, not all at once."""
    path = Path(path)
    if path.suffix == ".mcap":
        return _read_mcap(path, topics, start_ns, end_ns)
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
    path = Path(path)
    if path.suffix != ".mcap":
        # read_records states the refusal.
        read_records(path)
    from mcap.reader import make_reader

    with open(path, "rb") as f:
        for metadata in make_reader(f).iter_metadata():
            if metadata.name == "sil" and "manifest_hash" in metadata.metadata:
                return metadata.metadata["manifest_hash"]
    return None


def _read_mcap(path: Path, topics: Collection[str] | None,
               start_ns: int | None, end_ns: int | None,
               ) -> Iterator[tuple[str, int, bytes]]:
    from mcap.reader import make_reader

    with open(path, "rb") as f:
        for _, channel, message in make_reader(f).iter_messages(
                topics=None if topics is None else list(topics),
                start_time=start_ns, end_time=end_ns):
            yield channel.topic, message.log_time, message.data
