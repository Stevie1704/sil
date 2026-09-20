"""Determinism-check mode: run a manifest twice, bit-compare the recordings.

Same artifacts + same machine class must give bit-identical MCAP output;
any difference means a participant broke the determinism contract and is
reported loudly (exit 3) the day it is introduced.

A caller who bounds a direct Run with a Process-participant response deadline
can bound both Runs of a check with the same option, so the Run the check
judges is the Run the caller executes.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

EXIT_VIOLATION = 3

# The ceiling the runner accepts: std::chrono::milliseconds::max(). Mirroring
# it here keeps the checker from forwarding a value its two Runs reject.
MAX_PARTICIPANT_TIMEOUT_MS = 2**63 - 1


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(runner: Path, manifest: Path, out=sys.stdout, err=sys.stderr,
          *, participant_timeout_ms: int | None = None) -> int:
    deadline_argument = [] if participant_timeout_ms is None else [
        "--participant-timeout-ms", str(participant_timeout_ms)
    ]
    hashes = []
    with tempfile.TemporaryDirectory() as tmp:
        for i in (1, 2):
            mcap_path = Path(tmp) / f"run{i}.mcap"
            proc = subprocess.run(
                [str(runner), str(manifest), "-o", str(mcap_path),
                 *deadline_argument],
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


def _positive_milliseconds(text: str) -> int:
    """The runner's rule: a plain positive integer of millisecond range."""
    if not (text.isascii() and text.isdigit()) or not (
        0 < int(text) <= MAX_PARTICIPANT_TIMEOUT_MS
    ):
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a positive integer of at most "
            f"{MAX_PARTICIPANT_TIMEOUT_MS} milliseconds"
        )
    return int(text)


class _GivenOnce(argparse.Action):
    """Two deadlines are a mistake, not a choice; the runner agrees."""

    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest) is not None:
            parser.error(f"{option_string} may be given only once")
        setattr(namespace, self.dest, values)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sil-check",
        description="Run a manifest twice and fail on any output difference.",
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--runner", type=Path, default=Path("sil-run"),
                        help="path to the sil-run executable")
    parser.add_argument("--participant-timeout-ms", action=_GivenOnce,
                        type=_positive_milliseconds, default=None,
                        help="wall-clock deadline in milliseconds for each "
                             "Process participant response, forwarded to "
                             "both runs")
    args = parser.parse_args(argv)
    return check(args.runner, args.manifest,
                 participant_timeout_ms=args.participant_timeout_ms)


if __name__ == "__main__":
    sys.exit(main())
