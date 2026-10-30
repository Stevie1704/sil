"""Export a finalized Recording as an indexed visualization MCAP (spec #345 s2).

The output is a derivative for external MCAP viewers, in the
`sil_visualization_json` profile: JSON Messages with generated JSON Schemas,
chunks, Message and chunk indexes and a summary. It is not a Replay input;
the kernel replays `sil_pod` Recordings only. The source stays unchanged.

The selected Channels keep their names, stored order, publication times,
Message counts and whole-source `record_index` values. A receipt
`<output>.receipt.json` binds source and output by SHA-256.

Both files are written to temporary names and published without
overwriting; success is reported only after both exist. Exit codes: 0
success, 2 rejected input or configuration, 1 output or I/O failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Collection
from pathlib import Path

from mcap.writer import LIBRARY_IDENTIFIER, CompressionType, Writer

from sil import build_info
from sil.visualization import interchange
from sil.visualization.interchange import ChannelCodec
from sil.visualization.source import (Source, SourceError, completeness,
                                      open_source, select_channels)

PROG = "sil recording export"
CLI_PROFILE = "visualization-json"
RECEIPT_VERSION = 1
CONVERTER = "sil-visualization-export"
COMPRESSION = CompressionType.ZSTD
EXIT_OUTPUT = 1
EXIT_INPUT = 2


class ExportInputError(ValueError):
    """Rejected input or configuration (exit 2). Nothing is written."""


class ExportOutputError(OSError):
    """The output could not be written (exit 1). Nothing is left behind."""


def receipt_path(output: str | Path) -> Path:
    output = Path(output)
    return output.with_name(output.name + ".receipt.json")


def export_recording(source: str | Path, output: str | Path, *,
                     profile: str = CLI_PROFILE,
                     channels: Collection[str] | None = None) -> dict:
    """Write the visualization MCAP of `source` to `output` and its receipt
    beside it; return the receipt.

    `channels` selects source Channels by name; None selects all of them."""
    output = Path(output)
    receipt_file = receipt_path(output)
    if profile != CLI_PROFILE:
        raise ExportInputError(f"unsupported profile {profile!r}; the only "
                               f"profile is {CLI_PROFILE!r}")
    if output.suffix != ".mcap":
        raise ExportInputError(f"output {str(output)!r} must name an .mcap file")
    try:
        recording = open_source(source)
        _refuse_collisions(recording, output, receipt_file)
        selected = select_channels(recording, channels)
        codecs = _codecs(selected)
    except (SourceError, interchange.UnsupportedSchema) as e:
        raise ExportInputError(str(e)) from e
    with _TemporaryFiles(output, receipt_file) as temporary:
        try:
            counts = _write(recording, selected, codecs, temporary.output)
            recording.check_unchanged()
        except (SourceError, interchange.MalformedPayload) as e:
            raise ExportInputError(str(e)) from e
        except OSError as e:
            raise ExportOutputError(f"cannot write {str(output)!r}: {e}") from e
        receipt = _receipt(recording, selected, counts, output, channels)
        temporary.write_receipt(receipt)
        temporary.publish()
    return receipt


def _refuse_collisions(recording: Source, output: Path, receipt: Path) -> None:
    for path in (output, receipt):
        if path.resolve() == recording.path.resolve():
            raise ExportInputError(f"output {str(path)!r} is the source")
        if os.path.lexists(path):
            raise ExportInputError(f"output {str(path)!r} already exists; it "
                                   "is not overwritten")


def _codecs(selected) -> dict[str, ChannelCodec]:
    return {channel.name: ChannelCodec(channel.schema_name, channel.schema)
            for channel in selected}


def _write(recording: Source, selected, codecs: dict[str, ChannelCodec],
           path: Path) -> dict[str, dict]:
    """Write the output MCAP; return each Channel's count and time bounds."""
    counts = {channel.name: {"messages": 0, "first": None, "last": None}
              for channel in selected}
    with open(path, "wb") as stream:
        writer = Writer(stream, compression=COMPRESSION)
        writer.start(profile="", library=f"{CONVERTER}/{build_info.__version__}")
        writer.add_metadata("sil_visualization", _metadata(recording, selected))
        ids = _register(writer, recording, selected, codecs)
        for message in recording.messages():
            channel_id = ids.get(message.channel)
            if channel_id is None:
                continue
            payload = codecs[message.channel].payload(
                message.data, message.publication_time_ns, message.record_index)
            writer.add_message(channel_id, log_time=message.publication_time_ns,
                               publish_time=message.publication_time_ns,
                               data=payload)
            _count(counts[message.channel], message.publication_time_ns)
        writer.finish()
    return counts


def _metadata(recording: Source, selected) -> dict[str, str]:
    metadata = {
        "derivative": "visualization; not a sil_pod Replay input",
        "profile": interchange.PROFILE,
        "profile_version": str(interchange.PROFILE_VERSION),
        "source_sha256": recording.sha256,
        "channels": json.dumps([channel.name for channel in selected]),
    }
    if recording.manifest_hash is not None:
        metadata["source_manifest_hash"] = recording.manifest_hash
    return metadata


def _register(writer: Writer, recording: Source, selected,
              codecs: dict[str, ChannelCodec]) -> dict[str, int]:
    schema_ids: dict[str, int] = {}
    ids = {}
    for channel in selected:
        codec = codecs[channel.name]
        if channel.schema_sha256 not in schema_ids:
            schema_ids[channel.schema_sha256] = writer.register_schema(
                channel.schema_name, interchange.SCHEMA_ENCODING,
                codec.json_schema())
        ids[channel.name] = writer.register_channel(
            channel.name, interchange.MESSAGE_ENCODING,
            schema_ids[channel.schema_sha256], metadata={
                "sil_profile": interchange.PROFILE,
                "sil_profile_version": str(interchange.PROFILE_VERSION),
                "sil_source_sha256": recording.sha256,
                "sil_source_schema": channel.schema_name,
                "sil_source_schema_sha256": channel.schema_sha256,
            })
    return ids


def _count(entry: dict, time_ns: int) -> None:
    entry["messages"] += 1
    entry["first"] = time_ns if entry["first"] is None else min(entry["first"], time_ns)
    entry["last"] = time_ns if entry["last"] is None else max(entry["last"], time_ns)


def _bounds(first: int | None, last: int | None) -> dict | None:
    if first is None:
        return None
    return {"start_ns": str(first), "end_ns": str(last)}


def _receipt(recording: Source, selected, counts: dict[str, dict],
             output: Path, requested: Collection[str] | None) -> dict:
    firsts = [c["first"] for c in counts.values() if c["first"] is not None]
    lasts = [c["last"] for c in counts.values() if c["last"] is not None]
    return {
        "sil_visualization_export": RECEIPT_VERSION,
        "converter": {"name": CONVERTER, "version": build_info.__version__,
                      "revision": build_info.SOURCE_REVISION,
                      "mcap": LIBRARY_IDENTIFIER},
        "profile": {"name": interchange.PROFILE,
                    "version": interchange.PROFILE_VERSION},
        "derivative": "visualization; not a sil_pod Replay input",
        "source": {"file": recording.path.name, "sha256": recording.sha256,
                   "size_bytes": recording.size_bytes,
                   "manifest_hash": recording.manifest_hash},
        "output": {"file": output.name, "sha256": None, "size_bytes": None,
                   "compression": COMPRESSION.name.lower()},
        "selection": {"channels": None if requested is None
                      else [channel.name for channel in selected]},
        "channels": [{
            "source_channel": channel.name,
            "topic": channel.name,
            "source_schema": channel.schema_name,
            "source_schema_sha256": channel.schema_sha256,
            "schema_encoding": interchange.SCHEMA_ENCODING,
            "message_encoding": interchange.MESSAGE_ENCODING,
            "messages": counts[channel.name]["messages"],
            "bounds": _bounds(counts[channel.name]["first"],
                              counts[channel.name]["last"]),
        } for channel in selected],
        "messages": sum(c["messages"] for c in counts.values()),
        "bounds": _bounds(min(firsts, default=None), max(lasts, default=None)),
        "numeric_policy": interchange.NUMERIC_POLICY,
        "completeness": completeness(recording),
    }


class _TemporaryFiles:
    """Temporary output and receipt beside their final paths; publishing
    links both into place without overwriting, and any failure removes
    everything this export created."""

    def __init__(self, output: Path, receipt: Path):
        self._final = (output, receipt)
        self._temporary: list[Path] = []
        self._published: list[Path] = []
        self.output = self._new(output)

    def _new(self, final: Path) -> Path:
        try:
            handle, name = tempfile.mkstemp(dir=final.parent,
                                            prefix=f".{final.name}.",
                                            suffix=".tmp")
        except OSError as e:
            self._cleanup()
            raise ExportOutputError(f"cannot write {str(final)!r}: {e}") from e
        os.close(handle)
        self._temporary.append(Path(name))
        return Path(name)

    def write_receipt(self, receipt: dict) -> None:
        output, receipt_file = self._final
        try:
            receipt["output"]["sha256"], receipt["output"]["size_bytes"] = (
                _file_sha256(self.output))
            self.receipt = self._new(receipt_file)
            self.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
        except OSError as e:
            raise ExportOutputError(
                f"cannot write receipt {str(receipt_file)!r}: {e}") from e

    def publish(self) -> None:
        for temporary, final in zip((self.output, self.receipt), self._final):
            try:
                os.link(temporary, final)
            except FileExistsError as e:
                raise ExportInputError(f"output {str(final)!r} already "
                                       "exists; it is not overwritten") from e
            except OSError as e:
                raise ExportOutputError(f"cannot write {str(final)!r}: {e}") from e
            self._published.append(final)
        self._published.clear()

    def _cleanup(self) -> None:
        for path in (*self._temporary, *self._published):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        self._temporary.clear()
        self._published.clear()

    def __enter__(self) -> _TemporaryFiles:
        return self

    def __exit__(self, *exc) -> None:
        self._cleanup()


def _file_sha256(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as stream:
        while block := stream.read(1 << 20):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Export a finalized Recording as an indexed MCAP that "
                    "external viewers such as Foxglove open directly. The "
                    "output is a visualization derivative, not a Replay input.",
        allow_abbrev=False,
    )
    parser.add_argument("source", type=Path, help="the finalized .mcap Recording")
    parser.add_argument("--profile", required=True, choices=[CLI_PROFILE],
                        help="the interchange profile")
    parser.add_argument("-o", "--output", type=Path, required=True,
                        help="the .mcap file to write; the receipt is written "
                             "to <output>.receipt.json")
    parser.add_argument("--channel", action="append", dest="channels",
                        metavar="NAME",
                        help="export only this Channel; repeat for more "
                             "(default: every Channel)")
    args = parser.parse_args(argv)
    try:
        receipt = export_recording(args.source, args.output,
                                   profile=args.profile, channels=args.channels)
    except ExportInputError as e:
        sys.stderr.write(f"{PROG}: error: {e}\n")
        return EXIT_INPUT
    except ExportOutputError as e:
        sys.stderr.write(f"{PROG}: error: {e}\n")
        return EXIT_OUTPUT
    sys.stdout.write(f"wrote {args.output} ({receipt['messages']} Messages) "
                     f"and {receipt_path(args.output)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
