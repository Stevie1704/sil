"""pytest-facing frontend: run a manifest, get typed messages back.

A test is a scheduled participant (see sil.participant); this module covers
the outer half: invoking the runner, surfacing aborts as exceptions with the
participant's assertion message, and decoding the recording for post-hoc
checks.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from sil import schema
from sil.check import MAX_PARTICIPANT_TIMEOUT_MS
from sil.manifest import Manifest


class RunFailure(AssertionError):
    """The Run aborted; the message carries the participant's reason.

    `exit_code` is the Run's own, so a test can tell an assertion the Run
    failed (1) from a Manifest the loader rejected (2).
    """

    def __init__(self, reason: str, exit_code: int):
        super().__init__(reason)
        self.exit_code = exit_code


def participant_command(file: str | Path, cls: str) -> list[str]:
    """Command line for a manifest process participant defined in a file."""
    return [sys.executable, "-m", "sil.participant", f"{file}:{cls}"]


@dataclass(frozen=True)
class RunResult:
    mcap_path: Path
    manifest_hash: str
    _types: dict[str, schema.MessageType]
    _topic_schemas: dict[str, str]

    def messages(self, topic: str) -> list[tuple[int, dict]]:
        """Returns [(virtual_time_ns, fields_dict), ...] for one channel."""
        from sil.recording import read_records

        message_type = self._types[self._topic_schemas[topic]]
        return [
            (log_time, message_type.unpack(data))
            for channel_topic, log_time, data in read_records(self.mcap_path)
            if channel_topic == topic
        ]


def _deadline_argument(participant_timeout_ms: int | None) -> list[str]:
    """The runner's option for the deadline; the runner's rule for its value."""
    if participant_timeout_ms is None:
        return []
    if isinstance(participant_timeout_ms, bool) or not isinstance(
        participant_timeout_ms, int
    ):
        raise TypeError(
            "participant_timeout_ms must be an int, not "
            f"{type(participant_timeout_ms).__name__}"
        )
    if not 0 < participant_timeout_ms <= MAX_PARTICIPANT_TIMEOUT_MS:
        raise ValueError(
            f"participant_timeout_ms must be a positive integer of at most "
            f"{MAX_PARTICIPANT_TIMEOUT_MS} milliseconds, "
            f"not {participant_timeout_ms}"
        )
    return ["--participant-timeout-ms", str(participant_timeout_ms)]


def run_simulation(manifest: Manifest, *, runner: str | Path,
                   workdir: str | Path,
                   participant_timeout_ms: int | None = None) -> RunResult:
    """Runs a manifest to completion. Raises RunFailure on abort, so a
    participant assertion surfaces as a normal pytest failure.

    `participant_timeout_ms` is the runner's `--participant-timeout-ms`
    guard, forwarded unchanged: each Process participant response wait gets
    that many wall-clock milliseconds, and a missed deadline is a RunFailure
    with exit code 1. Omitted, the wait stays unlimited. It is not Manifest
    data, so it changes neither the Manifest hash nor a timely Run's
    Recording.
    """
    deadline_argument = _deadline_argument(participant_timeout_ms)
    workdir = Path(workdir)
    ref = manifest.write(workdir / "manifest.json")
    mcap_path = workdir / "out.mcap"
    proc = subprocess.run(
        [str(runner), str(ref.path), "-o", str(mcap_path),
         *deadline_argument],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RunFailure(proc.stderr.strip(), proc.returncode)

    doc = manifest.to_doc()
    return RunResult(
        mcap_path=mcap_path,
        manifest_hash=ref.hash,
        _types=schema.load(doc["schemas"]),
        _topic_schemas={name: c["schema"] for name, c in doc["channels"].items()},
    )
