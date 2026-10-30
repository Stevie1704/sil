#!/usr/bin/env python3
"""Recording navigation cost: preparation, index size, RSS and seek latency.

Writes the spec #345 fixture (default: 10,000,000 Messages of 64 bytes on 16
Channels, 1 MiB uncompressed chunks), then measures in separate child
processes, so each phase reports its own peak RSS:

1. cold preparation: hash, scan and index build into an empty cache;
2. warm preparation: hash and reuse of the published index;
3. seeks: `--seeks` seeded random `snapshot(None, t)` calls over all
   Channels in a fresh process (cold: empty chunk and SQLite caches), then
   the same seeks again in that process (warm caches).

The operating system file cache is not dropped between phases, so "cold"
means cold process caches, not a cold disk. Every seek result is checked
against the expected record_index, which the fixture's regular layout
gives directly. Fixture writing is not measured.

    tools/navigation_bench.py --machine "<name>" [--messages N] \\
        [--workdir DIR] [--json out.json] [--markdown out.md]
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import resource
import statistics
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python" / "src"))

MS = 1_000_000
CHUNK_BYTES = 1 << 20
SCHEMA = {"fields": [{"name": f"w{i}", "type": "u64"} for i in range(8)]}
# Linux reports ru_maxrss in KiB, macOS in bytes.
RSS_UNIT = 1 if sys.platform == "darwin" else 1024


def write_fixture(path: Path, messages: int, channels: int) -> None:
    """Message i is on Channel i % channels at (i // channels) ms; all
    Channels publish at each time, so every time is an equal-time Burst."""
    from mcap.writer import CompressionType, Writer

    payload = struct.Struct("<8Q")
    with open(path, "wb") as stream:
        writer = Writer(stream, chunk_size=CHUNK_BYTES,
                        compression=CompressionType.NONE)
        writer.start(profile="sil", library="sil-navigation-bench")
        schema = writer.register_schema("bench.Wide", "sil_pod",
                                        json.dumps(SCHEMA).encode())
        ids = [writer.register_channel(f"ch{k:02d}", "sil_pod", schema)
               for k in range(channels)]
        for i in range(messages):
            t = (i // channels) * MS
            writer.add_message(ids[i % channels], log_time=t, publish_time=t,
                               data=payload.pack(i, *range(7)))
        writer.finish()


def expected_index(time_ns: int, channel: int, messages: int,
                   channels: int) -> int | None:
    """record_index of the latest Message of `channel` at or before
    `time_ns` in the fixture layout."""
    i = (time_ns // MS) * channels + channel
    while i >= messages:
        i -= channels
    return i if i >= 0 else None


def peak_rss_mib() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * RSS_UNIT / (1 << 20)


def phase_prepare(source: Path, cache: Path) -> dict:
    from sil.visualization.navigation import prepare_navigation

    started = time.perf_counter()
    with prepare_navigation(source, cache_dir=cache) as nav:
        elapsed = time.perf_counter() - started
        described = nav.describe()
    return {"seconds": elapsed, "reused": described.index.reused,
            "index_bytes": described.index.size_bytes,
            "messages": described.message_count, "peak_rss_mib": peak_rss_mib()}


def phase_seek(source: Path, cache: Path, seeks: int, seed: int,
               channels: int) -> dict:
    from sil.visualization.navigation import prepare_navigation

    with prepare_navigation(source, cache_dir=cache) as nav:
        described = nav.describe()
        assert described.index.reused
        rng = random.Random(seed)
        times = [rng.randint(described.start_ns, described.end_ns)
                 for _ in range(seeks)]
        rss_before = peak_rss_mib()
        rounds = {}
        for name in ("cold", "warm"):
            latencies = []
            for t in times:
                started = time.perf_counter()
                snapshot = nav.snapshot(None, t)
                latencies.append((time.perf_counter() - started) * 1000)
                for k, record in enumerate(snapshot.values()):
                    want = expected_index(t, k, described.message_count, channels)
                    got = None if record is None else record.record_index
                    if got != want:
                        raise AssertionError(f"seek {t}: channel {k} has "
                                             f"{got}, expected {want}")
            rounds[name] = latencies
    return {"rounds": rounds, "peak_rss_mib": peak_rss_mib(),
            "rss_before_seeks_mib": rss_before}


def child(args: list[str]) -> dict:
    result = subprocess.run([sys.executable, __file__, *args], check=True,
                            capture_output=True, text=True)
    return json.loads(result.stdout)


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


def summary(latencies: list[float]) -> dict:
    return {"min_ms": min(latencies), "median_ms": statistics.median(latencies),
            "p95_ms": percentile(latencies, 0.95), "max_ms": max(latencies)}


def markdown(report: dict) -> str:
    f = report["fixture"]
    cold, warm = report["prepare_cold"], report["prepare_warm"]
    lines = [
        "# Recording navigation benchmark (generated)",
        "",
        "Generated by `tools/navigation_bench.py`.",
        "",
        f"- Machine: {report['machine']}; {report['platform']}",
        f"- Python {report['python']}, SQLite {report['sqlite']}",
        f"- Fixture: {f['messages']:,} Messages of {f['payload_bytes']} bytes, "
        f"{f['channels']} Channels, {f['chunk_bytes'] >> 20} MiB uncompressed "
        f"chunks, {f['bytes'] / (1 << 20):.1f} MiB",
        f"- Seeks: {report['seeks']} seeded (seed {report['seed']}) "
        "`snapshot(None, t)` over all Channels, every result checked",
        "- OS file cache not dropped: cold means cold process caches only",
        "",
        "| Phase | Wall (s) | Peak RSS (MiB) |",
        "| --- | --- | --- |",
        f"| Cold preparation (hash, scan, build) | {cold['seconds']:.1f} | "
        f"{cold['peak_rss_mib']:.1f} |",
        f"| Warm preparation (hash, reuse) | {warm['seconds']:.1f} | "
        f"{warm['peak_rss_mib']:.1f} |",
        "",
        f"Index size: {cold['index_bytes'] / (1 << 20):.1f} MiB "
        f"({cold['index_bytes'] / f['messages']:.1f} bytes per Message).",
        "",
        "| Seeks | min (ms) | median (ms) | p95 (ms) | max (ms) |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name in ("cold", "warm"):
        s = report["seek"][name]
        lines.append(f"| {name} | {s['min_ms']:.2f} | {s['median_ms']:.2f} | "
                     f"{s['p95_ms']:.2f} | {s['max_ms']:.2f} |")
    lines += ["", f"Seek process peak RSS: {report['seek']['peak_rss_mib']:.1f} MiB.", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--machine", required=True)
    parser.add_argument("--messages", type=int, default=10_000_000)
    parser.add_argument("--channels", type=int, default=16)
    parser.add_argument("--seeks", type=int, default=200)
    parser.add_argument("--seed", type=int, default=345)
    parser.add_argument("--workdir", type=Path, default=Path("navigation-bench"))
    parser.add_argument("--json", type=Path)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args()

    import sqlite3

    args.workdir.mkdir(parents=True, exist_ok=True)
    source = args.workdir / f"fixture-{args.messages}x{args.channels}.mcap"
    cache = args.workdir / "cache"
    if not source.exists():
        write_fixture(source, args.messages, args.channels)
    for stale in cache.glob("*") if cache.exists() else ():
        stale.unlink()
    base = [str(source), str(cache)]
    prepare_cold = child(["--phase", "prepare", *base])
    prepare_warm = child(["--phase", "prepare", *base])
    seek = child(["--phase", "seek", *base, str(args.seeks), str(args.seed),
                  str(args.channels)])
    report = {
        "machine": args.machine,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "fixture": {"messages": args.messages, "channels": args.channels,
                    "payload_bytes": 64, "chunk_bytes": CHUNK_BYTES,
                    "bytes": source.stat().st_size},
        "seeks": args.seeks, "seed": args.seed,
        "prepare_cold": prepare_cold, "prepare_warm": prepare_warm,
        "seek": {"cold": summary(seek["rounds"]["cold"]),
                 "warm": summary(seek["rounds"]["warm"]),
                 "peak_rss_mib": seek["peak_rss_mib"],
                 "raw_ms": seek["rounds"]},
    }
    assert not prepare_cold["reused"] and prepare_warm["reused"]
    text = markdown(report)
    sys.stdout.write(text)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n")
    if args.markdown:
        args.markdown.write_text(text)
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--phase":
        phase, rest = sys.argv[2], sys.argv[3:]
        source, cache = Path(rest[0]), Path(rest[1])
        if phase == "prepare":
            out = phase_prepare(source, cache)
        else:
            out = phase_seek(source, cache, int(rest[2]), int(rest[3]), int(rest[4]))
        json.dump(out, sys.stdout)
        sys.exit(0)
    sys.exit(main())
