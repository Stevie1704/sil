"""State what produced each Run of the replay proof, by digest.

    python3 replay_provenance.py <workspace> <manifest-name> ...

For every named Manifest: its own hash, the Recording it produced, every FMU
archive its command names, the external configuration it declares, and — for
a replay Manifest — the Recording it replays, with the hash the Manifest
committed to checked against the file.

`sil-run` writes a provenance side-car of its own, which carries the same
facts and more. The released runner this proof pins predates it, so the record
is assembled here from the Manifests and the artifacts they name. Nothing is
read out of a Run: a Manifest is the single declarative input to one, so what
a Run depended on is what its Manifest names.

Stdlib only, like the rest of the fixture's own tooling: this reads artifacts
rather than driving anything.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

# The command arguments whose value is a file the kernel resolves and digests.
_FILE_ARGUMENTS = {"--instance"}
# The arguments that carry the configuration outside the FMUs themselves.
_CONFIGURATION = {"--bus-profile": "bus profile", "--connect": "connection",
                  "--start": "start value", "--bind": "binding"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def named_files(command: list[str]) -> list[str]:
    """Every file the command names, which is every `--instance` path."""
    return [
        command[index + 2] for index, argument in enumerate(command)
        if argument in _FILE_ARGUMENTS and index + 2 < len(command)
    ]


def configuration(command: list[str]) -> list[tuple[str, str]]:
    return [
        (_CONFIGURATION[argument], command[index + 1])
        for index, argument in enumerate(command)
        if argument in _CONFIGURATION and index + 1 < len(command)
    ]


def report(workspace: Path, name: str) -> None:
    manifest_path = workspace / f"{name}.json"
    document = json.loads(manifest_path.read_text())
    print(f"run                  {name}")
    print(f"  manifest           {manifest_path.name} "
          f"sha256 {digest(manifest_path)}")
    recording = workspace / f"{name}.mcap"
    if recording.exists():
        print(f"  recording          {recording.name} "
              f"sha256 {digest(recording)}")
    for participant, spec in sorted(document["participants"].items()):
        if spec["type"] == "replay":
            replayed = Path(spec["recording"])
            declared = spec["recording_hash"]
            actual = digest(replayed) if replayed.exists() else "unreadable"
            print(f"  replayed by        {participant} "
                  f"{', '.join(spec['channels'])}")
            print(f"  replayed recording {replayed.name} sha256 {declared} "
                  f"({'matches the file' if actual == declared else actual})")
            continue
        if spec["type"] != "process":
            continue
        for file in named_files(spec["command"]):
            path = Path(file)
            print(f"  fmu                {path.name} sha256 "
                  f"{digest(path) if path.exists() else 'unreadable'}")
        for label, value in configuration(spec["command"]):
            print(f"  {label:<18} {value}")
    for channel, spec in sorted(document["channels"].items()):
        latency = spec.get("latency_ns")
        if latency is not None:
            print(f"  channel            {channel} latency_ns {latency}")
        for interceptor in spec.get("interceptors", []):
            print(f"  interceptor        {channel} "
                  f"{json.dumps(interceptor, sort_keys=True)}")
    print()


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        raise SystemExit(
            "usage: replay_provenance.py <workspace> <manifest-name> ..."
        )
    workspace = Path(argv[0])
    for name in argv[1:]:
        report(workspace, name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
