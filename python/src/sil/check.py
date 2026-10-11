"""Determinism-check mode: run a manifest twice, bit-compare the recordings.

Same artifacts + same machine class must give bit-identical MCAP output;
any difference means a participant broke the determinism contract and is
reported loudly (exit 3) the day it is introduced.

A caller who bounds a direct Run with a Process-participant response deadline
can bound both Runs of a check with the same option, so the Run the check
judges is the Run the caller executes.

On a violation the check reports where the two Recordings first differ, so a
caller can find the cause without running the Manifest again. `--keep` keeps
both Recordings, and `--json` writes the `sil_check_report`.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from sil.recording_diff import first_difference

EXIT_USAGE = 2
EXIT_VIOLATION = 3
REPORT_VERSION = 1
DETERMINISTIC = "deterministic"
VIOLATION = "violation"

# The ceiling the runner accepts: std::chrono::milliseconds::max(). Mirroring
# it here keeps the checker from forwarding a value its two Runs reject.
MAX_PARTICIPANT_TIMEOUT_MS = 2**63 - 1


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(runner: Path, manifest: Path, out=sys.stdout, err=sys.stderr,
          *, participant_timeout_ms: int | None = None,
          keep: Path | None = None, as_json: bool = False) -> int:
    deadline_argument = [] if participant_timeout_ms is None else [
        "--participant-timeout-ms", str(participant_timeout_ms)
    ]
    with contextlib.ExitStack() as stack:
        try:
            directory = Path(stack.enter_context(_workdir(keep)))
        except OSError as error:
            err.write(f"sil check: error: cannot keep the Recordings in "
                      f"{str(keep)!r}: {error}\n")
            return EXIT_USAGE
        recordings = [directory / f"run{i}.mcap" for i in (1, 2)]
        for mcap_path in recordings:
            proc = subprocess.run(
                [str(runner), str(manifest), "-o", str(mcap_path),
                 *deadline_argument],
                capture_output=True, text=True,
            )
            if proc.returncode != 0:
                err.write(proc.stderr)
                return proc.returncode
        hashes = [file_sha256(path) for path in recordings]
        difference = first_difference(*recordings)

    # Only kept Recordings outlive the check, so only they are named.
    kept = recordings if keep is not None else [None, None]
    report = _report(manifest, hashes, kept, difference)
    _write(report, out, err, as_json=as_json)
    return EXIT_VIOLATION if difference is not None else 0


def _write(report: dict, out, err, *, as_json: bool) -> None:
    if as_json:
        out.write(json.dumps(report, indent=2) + "\n")
        return
    run1, run2 = (_run_line(run) for run in report["runs"])
    if report["verdict"] == VIOLATION:
        err.write(
            "DETERMINISM VIOLATION: two runs of the same manifest produced "
            f"different recordings\n  run 1: {run1}\n  run 2: {run2}\n"
            f"  first difference: {report['first_difference']['detail']}\n"
        )
    else:
        out.write(f"deterministic: {report['runs'][0]['sha256']}\n")


def _workdir(keep: Path | None):
    """The directory the two Runs write to: `keep`, or a temporary one."""
    if keep is None:
        return tempfile.TemporaryDirectory()
    keep.mkdir(parents=True, exist_ok=True)
    return contextlib.nullcontext(keep)


def _run_line(run: dict) -> str:
    if run["recording"] is None:
        return run["sha256"]
    return f"{run['sha256']} ({run['recording']})"


def _report(manifest: Path, hashes: list[str], kept: list[Path | None],
            difference: dict | None) -> dict:
    return {
        "sil_check_report": REPORT_VERSION,
        "verdict": VIOLATION if difference is not None else DETERMINISTIC,
        "manifest": str(manifest),
        "runs": [{"sha256": sha256,
                  "recording": None if path is None else str(path)}
                 for sha256, path in zip(hashes, kept)],
        "first_difference": difference,
    }


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
        prog="sil check",
        description="Run a manifest twice and fail on any output difference.",
        # The runner matches its options exactly. A checker that answered to
        # --participant-timeout-m would accept a spelling the Runs it forwards
        # to reject, and every option added later would silently change which
        # flag an abbreviation resolves to.
        allow_abbrev=False,
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--runner", type=Path, default=Path("sil-run"),
                        help="path to the sil-run executable")
    parser.add_argument("--participant-timeout-ms", action=_GivenOnce,
                        type=_positive_milliseconds, default=None,
                        help="wall-clock deadline in milliseconds for each "
                             "Process participant response, forwarded to "
                             "both runs")
    parser.add_argument("--keep", type=Path, metavar="DIR", default=None,
                        help="keep both Recordings in DIR as run1.mcap and "
                             "run2.mcap")
    parser.add_argument("--json", action="store_true",
                        help="write the sil_check_report as JSON to stdout")
    args = parser.parse_args(argv)
    return check(args.runner, args.manifest,
                 participant_timeout_ms=args.participant_timeout_ms,
                 keep=args.keep, as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
