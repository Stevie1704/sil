"""Measurements of the live visualization qualification draft (#350).

    PYTHONPATH=python/src python tools/live_visualization_measure.py \
        recordings A.mcap [B.mcap ...]
    PYTHONPATH=python/src python tools/live_visualization_measure.py \
        time MANIFEST.json --sil-run build/sil-run --runs 7

`recordings` prints, per Recording, the Message count, payload sizes,
publication instants (distinct publication times), rates, per-instant
sizes, Burst depth (Messages per Channel per instant) and descending
timestamps, as JSON. `time` runs a Manifest `--runs` times, times each Run
with `time.perf_counter` and requires each output to have the bytes of the
first. docs/research/live-visualization-measurements.json holds the retained
output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

from sil.recording import read_channels, read_records


def describe(path: Path) -> dict:
    records = list(read_records(path))
    sizes = Counter(len(data) for _, _, data in records)
    per_instant = Counter(t for _, t, _ in records)
    bytes_per_instant = Counter()
    burst = Counter()
    for topic, t, data in records:
        bytes_per_instant[t] += len(data)
        burst[(topic, t)] += 1
    times = [t for _, t, _ in records]
    span_s = (max(times) - min(times)) / 1e9 if times else 0.0
    total = sum(len(data) for _, _, data in records)
    return {
        "file": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size_bytes": path.stat().st_size,
        "channels": len(read_channels(path)),
        "messages": len(records),
        "payload_sizes": sorted(sizes),
        "publication_instants": len(per_instant),
        "virtual_span_s": span_s,
        "messages_per_virtual_s": len(records) / span_s if span_s else None,
        "payload_bytes_per_virtual_s": total / span_s if span_s else None,
        "max_payload_bytes_per_instant": max(bytes_per_instant.values(), default=0),
        "mean_payload_bytes_per_instant":
            total / len(per_instant) if per_instant else 0.0,
        "max_messages_per_instant": max(per_instant.values(), default=0),
        "max_burst_depth": max(burst.values(), default=0),
        "descending_timestamps": sum(b < a for a, b in zip(times, times[1:])),
    }


def time_runs(manifest: Path, sil_run: Path, runs: int) -> dict:
    walls, digests = [], set()
    with tempfile.TemporaryDirectory() as work:
        for run in range(runs):
            out = Path(work) / f"run-{run}.mcap"
            start = time.perf_counter()
            subprocess.run([str(sil_run), str(manifest), "-o", str(out)],
                           check=True, capture_output=True)
            walls.append(time.perf_counter() - start)
            digests.add(hashlib.sha256(out.read_bytes()).hexdigest())
    if len(digests) != 1:
        sys.exit(f"the {runs} Runs gave {len(digests)} different Recordings")
    return {"manifest": str(manifest), "runs": runs,
            "wall_s": [round(w, 6) for w in walls],
            "median_s": statistics.median(walls), "min_s": min(walls),
            "max_s": max(walls), "recording_sha256": digests.pop()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    recordings = commands.add_parser("recordings")
    recordings.add_argument("paths", nargs="+", type=Path)
    timing = commands.add_parser("time")
    timing.add_argument("manifest", type=Path)
    timing.add_argument("--sil-run", type=Path, default=Path("build/sil-run"))
    timing.add_argument("--runs", type=int, default=7)
    args = parser.parse_args(argv)
    if args.command == "recordings":
        result = [describe(path) for path in args.paths]
    else:
        result = time_runs(args.manifest, args.sil_run, args.runs)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
