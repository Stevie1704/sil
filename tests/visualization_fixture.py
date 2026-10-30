"""Source Recordings for the visualization export tests and qualification.

`write_source` writes a `sil_pod` MCAP as the kernel writes it, so a test
states its own types, times and Publish order.

`write_qualification_source` writes the fixed Recording that a Foxglove
qualification exports and opens (docs/visualization.md). It is deterministic:
the same file on every run.

    python tests/visualization_fixture.py qualification.mcap
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from mcap.writer import CompressionType, Writer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python" / "src"))

from sil.schema import MessageType  # noqa: E402


def write_source(path, schemas: dict, channels: dict[str, str],
                 messages, *, metadata: dict | None = None,
                 chunk_size: int = 1024 * 1024,
                 compression: CompressionType = CompressionType.NONE):
    """A `sil_pod` Recording: `channels` maps Channel name to Schema name,
    `messages` is (channel, publication time, field values) in Publish
    order."""
    types = {name: MessageType(name, spec) for name, spec in schemas.items()}
    with open(path, "wb") as stream:
        writer = Writer(stream, chunk_size=chunk_size,
                        compression=compression)
        writer.start(profile="sil", library="sil-test")
        if metadata is not None:
            writer.add_metadata("sil", metadata)
        schema_ids = {name: writer.register_schema(
            name, "sil_pod", json.dumps(spec).encode())
            for name, spec in schemas.items()}
        channel_ids = {name: writer.register_channel(
            name, "sil_pod", schema_ids[schema])
            for name, schema in channels.items()}
        for seq, (channel, time_ns, values) in enumerate(messages):
            data = types[channels[channel]].pack(**values)
            writer.add_message(channel_ids[channel], log_time=time_ns,
                               publish_time=time_ns, data=data, sequence=seq)
        writer.finish()
    return path


MS = 1_000_000
QUALIFICATION_SCHEMAS = {
    "q.Speed": {"fields": [{"name": "speed_mps", "type": "f64"},
                           {"name": "accel_mps2", "type": "f32"}]},
    "q.Counter": {"fields": [{"name": "count", "type": "u64"},
                             {"name": "delta", "type": "i64"},
                             {"name": "small", "type": "i32"}]},
    "q.Sensor": {"fields": [{"name": "reading", "type": "f32"}]},
    "q.Event": {"fields": [{"name": "id", "type": "u16"}]},
    "q.Arrays": {"fields": [{"name": "raw", "type": "u8", "count": 4},
                            {"name": "vector", "type": "f32", "count": 3},
                            {"name": "ids", "type": "u64", "count": 2}]},
}
QUALIFICATION_CHANNELS = {
    "vehicle/speed": "q.Speed",
    "counter": "q.Counter",
    "sensor": "q.Sensor",
    "burst": "q.Event",
    "sparse": "q.Event",
    "arrays": "q.Arrays",
}
_NONFINITE = (float("nan"), float("inf"), float("-inf"))


def qualification_messages():
    """10 s of Virtual time: a 10 ms speed signal for plots, counters above
    2^53, a sensor with non-finite readings, a three-Message equal-time
    Burst every second, one sparse Message at 5 s, and arrays."""
    for step in range(1000):
        t = step * 10 * MS
        yield ("vehicle/speed", t, {"speed_mps": 20 + 5 * math.sin(step / 50),
                                    "accel_mps2": math.cos(step / 50) / 10})
        if step % 10 == 0:
            yield ("counter", t, {"count": 2**53 + step, "delta": -(2**62) + step,
                                  "small": step})
            yield ("sensor", t, {"reading": _NONFINITE[step // 10 % 3]
                                 if step % 50 == 0 else step / 10})
        if step % 100 == 0:
            for burst in range(3):
                yield ("burst", t, {"id": step + burst})
            yield ("arrays", t, {"raw": bytes([step % 256, 1, 2, 255]),
                                 "vector": [step / 100, -1.0, float("nan")],
                                 "ids": [2**64 - 1 - step, step]})
        if step == 500:
            yield ("sparse", t, {"id": 7})


def write_qualification_source(path) -> Path:
    return write_source(path, QUALIFICATION_SCHEMAS, QUALIFICATION_CHANNELS,
                        qualification_messages())


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} OUTPUT.mcap")
    write_qualification_source(Path(sys.argv[1]))
