"""Recording-format seam on the Python read side.

The kernel selects a recording format from the output extension; the Python
reader used to decode a run's output mirrors that seam so an unrecognized
extension fails with a clear error here too, rather than handing an arbitrary
file to an MCAP decoder. MCAP is the only format in v1.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator


class UnknownRecordingFormat(ValueError):
    """Raised for a recording path whose extension names no known format."""


def read_records(path: str | Path) -> Iterator[tuple[str, int, bytes]]:
    """Yields (topic, log_time_ns, data) for every recorded message, in stored
    order. Dispatches on the path extension; raises UnknownRecordingFormat for
    an unrecognized one."""
    path = Path(path)
    if path.suffix == ".mcap":
        return _read_mcap(path)
    raise UnknownRecordingFormat(
        f"unrecognized recording format {path.suffix!r} for recording "
        f"{str(path)!r}"
    )


def read_schemas(path: str | Path) -> dict[str, dict]:
    """The schema declaration of every recorded Channel, by Channel name.

    Dispatches like `read_records`. A Channel with no Messages is still
    listed, because the Recording declares it."""
    path = Path(path)
    if path.suffix != ".mcap":
        # read_records states the refusal.
        read_records(path)
    from mcap.reader import make_reader

    with open(path, "rb") as f:
        summary = make_reader(f).get_summary()
    if summary is None:
        raise ValueError(f"recording {str(path)!r} has no summary section")
    return {
        channel.topic: json.loads(summary.schemas[channel.schema_id].data)
        for channel in summary.channels.values()
    }


def _read_mcap(path: Path) -> Iterator[tuple[str, int, bytes]]:
    from mcap.reader import make_reader

    with open(path, "rb") as f:
        for _, channel, message in make_reader(f).iter_messages():
            yield channel.topic, message.log_time, message.data
