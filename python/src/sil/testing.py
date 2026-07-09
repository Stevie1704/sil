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
from sil.manifest import Manifest


class RunFailure(AssertionError):
    """The simulation aborted; the message carries the participant's reason."""


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


def run_simulation(manifest: Manifest, *, runner: str | Path,
                   workdir: str | Path) -> RunResult:
    """Runs a manifest to completion. Raises RunFailure on abort, so a
    participant assertion surfaces as a normal pytest failure."""
    workdir = Path(workdir)
    ref = manifest.write(workdir / "manifest.json")
    mcap_path = workdir / "out.mcap"
    proc = subprocess.run(
        [str(runner), str(ref.path), "-o", str(mcap_path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RunFailure(proc.stderr.strip())

    doc = manifest.to_doc()
    return RunResult(
        mcap_path=mcap_path,
        manifest_hash=ref.hash,
        _types=schema.load(doc["schemas"]),
        _topic_schemas={name: c["schema"] for name, c in doc["channels"].items()},
    )
