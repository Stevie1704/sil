#!/usr/bin/env python3
"""Observational comparison of the old and the new SHA-256 backend (#325).

Two parts, both from Release builds:

- File hashing: `sil_sha256_digest` hashes each representative file with the
  kept minimal implementation and with the kernel backend, in memory, in
  alternation.
- Runs: an old `sil-run` (built at the revision before the backend change) and
  the new `sil-run` run the workloads of tools/diagnostics_overhead.py in
  alternating pairs. Both must write the same Recording bytes.

    tools/sha256_backend_measure.py --build-dir build --old-runner OLD/sil-run \\
        --machine "<a name>" --conditions "<load, storage, cache>" \\
        --json docs/bench/sha256-backend.json

Each digest is checked against hashlib. The values describe one machine, build
and load. No test asserts a duration from them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from diagnostics_overhead import workloads  # noqa: E402

FILE_SIZES = {
    "runner-sized, 1 MiB": 1 << 20,
    "library-sized, 16 MiB": 16 << 20,
    "Recording-sized, 256 MiB": 256 << 20,
}


def spread(values: list[float]) -> dict:
    return {"median": statistics.median(values), "min": min(values), "max": max(values)}


def hash_once(tool: Path, path: Path, backend: str, expected: str) -> dict:
    fields = subprocess.run(
        [str(tool), str(path), "65536", "--backend", backend],
        capture_output=True, text=True, check=True).stdout.split()
    if fields[0] != expected:
        raise SystemExit(f"{backend} digest {fields[0]} is not {expected}")
    return {"cpu_ns": float(fields[1]), "wall_ns": float(fields[2])}


def measure_files(tool: Path, directory: Path, repeats: int) -> dict:
    results = {}
    for name, size in FILE_SIZES.items():
        # A repeated random MiB: SHA-256 cost does not depend on the content.
        data = random.Random(size).randbytes(1 << 20) * (size >> 20)
        path = directory / "input.bin"
        path.write_bytes(data)
        expected = hashlib.sha256(data).hexdigest()
        samples: dict[str, list[dict]] = {"minimal": [], "kernel": []}
        for _ in range(repeats):
            for backend, runs in samples.items():
                runs.append(hash_once(tool, path, backend, expected))
        results[name] = {
            "bytes": size,
            "sha256": expected,
            **{backend: {
                "cpu_ns": spread([run["cpu_ns"] for run in runs]),
                "wall_ns": spread([run["wall_ns"] for run in runs]),
            } for backend, runs in samples.items()},
        }
    return results


def run_once(launcher: Path, runner: Path, manifest: Path, work: Path) -> tuple[int, str]:
    recording = work / "out.mcap"
    start = time.monotonic_ns()
    subprocess.run([str(launcher), str(runner), str(manifest), "-o", str(recording)],
                   capture_output=True, check=True, cwd=work)
    wall = time.monotonic_ns() - start
    return wall, hashlib.sha256(recording.read_bytes()).hexdigest()


def measure_runs(build: Path, old: Path, directory: Path, pairs: int) -> dict:
    launcher = build / "sil-diagnostics-launcher"
    new = build / "sil-run"
    results = {}
    for name, (description, manifest) in workloads(directory, build).items():
        walls: dict[str, list[int]] = {"old": [], "new": []}
        recording = None
        size = 0
        for _ in range(pairs):
            for label, runner in (("old", old), ("new", new)):
                with tempfile.TemporaryDirectory() as work:
                    wall, digest = run_once(launcher, runner, manifest, Path(work))
                    size = (Path(work) / "out.mcap").stat().st_size
                walls[label].append(wall)
                if recording not in (None, digest):
                    raise SystemExit(f"{name}: Recording bytes differ between runners")
                recording = digest
        results[name] = {
            "description": description,
            "recording_bytes": size,
            "recording_sha256": recording,
            "old_wall_ns": spread(walls["old"]),
            "new_wall_ns": spread(walls["new"]),
        }
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--build-dir", type=Path, default=ROOT / "build")
    parser.add_argument("--old-runner", type=Path, required=True)
    parser.add_argument("--machine", required=True)
    parser.add_argument("--conditions", required=True,
                        help="load, storage and cache conditions of this measurement")
    parser.add_argument("--repeats", type=int, default=15)
    parser.add_argument("--pairs", type=int, default=5)
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()

    build = args.build_dir.resolve()
    with tempfile.TemporaryDirectory() as scratch:
        directory = Path(scratch)
        document = {
            "machine": args.machine,
            "conditions": args.conditions,
            "platform": platform.platform(),
            "architecture": platform.machine(),
            "repeats": args.repeats,
            "pairs": args.pairs,
            "files": measure_files(build / "sil_sha256_digest", directory, args.repeats),
            "runs": measure_runs(build, args.old_runner.resolve(), directory, args.pairs),
        }
    args.json.write_text(json.dumps(document, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
