"""Determinism-check mode: run a manifest twice, bit-compare the recordings.

Same artifacts + same machine class must give bit-identical MCAP output;
any difference means a participant broke the determinism contract and is
reported loudly (exit 3) the day it is introduced.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

EXIT_VIOLATION = 3


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(runner: Path, manifest: Path, out=sys.stdout, err=sys.stderr) -> int:
    hashes = []
    with tempfile.TemporaryDirectory() as tmp:
        for i in (1, 2):
            mcap_path = Path(tmp) / f"run{i}.mcap"
            proc = subprocess.run(
                [str(runner), str(manifest), "-o", str(mcap_path)],
                capture_output=True, text=True,
            )
            if proc.returncode != 0:
                err.write(proc.stderr)
                return proc.returncode
            hashes.append(file_sha256(mcap_path))

    if hashes[0] != hashes[1]:
        err.write(
            "DETERMINISM VIOLATION: two runs of the same manifest produced "
            f"different recordings\n  run 1: {hashes[0]}\n  run 2: {hashes[1]}\n"
        )
        return EXIT_VIOLATION
    out.write(f"deterministic: {hashes[0]}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sil-check",
        description="Run a manifest twice and fail on any output difference.",
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--runner", type=Path, default=Path("sil-run"),
                        help="path to the sil-run executable")
    args = parser.parse_args(argv)
    return check(args.runner, args.manifest)


if __name__ == "__main__":
    sys.exit(main())
