"""Copy the retained part of a profiling-evidence artifact (issue #314).

    python retain.py ARTIFACT EVIDENCE

ARTIFACT is the downloaded `profiling-evidence` workflow artifact. Raw
captures (perf.data, Heaptrack data, Recordings, perf script listings) are
large and contain process memory, so they stay in the artifact. EVIDENCE
receives the JSON and Markdown results, the probes, the small text outputs
and digests.txt: the SHA-256 and size of every artifact file, retained or
not.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

RETAINED_NAMES = {
    "probe.json", "results.json", "results.md", "manifest.json",
    "docker-options.txt", "image-id.txt", "host-paranoid-before.txt",
    "stderr.txt", "buildid-list.txt", "mmap-events.txt", "report-stats.txt",
}
RETAINED_SUFFIXES = (".interpret.stderr", ".stderr")
SKIPPED_PARTS = {"build", "sidecar", "buildid", "probe"}


def retained(relative: Path) -> bool:
    if SKIPPED_PARTS & set(relative.parts[:-1]):
        return False
    return relative.name in RETAINED_NAMES or (
        relative.name.endswith(RETAINED_SUFFIXES)
        and not relative.name.startswith("script-"))


def main(artifact: Path, evidence: Path) -> None:
    digests = []
    for path in sorted(p for p in artifact.rglob("*") if p.is_file()):
        relative = path.relative_to(artifact)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        digests.append(f"{digest}  {path.stat().st_size:>11}  {relative}")
        if retained(relative):
            target = evidence / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    (evidence / "digests.txt").write_text("\n".join(digests) + "\n")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: retain.py ARTIFACT EVIDENCE")
    main(Path(sys.argv[1]), Path(sys.argv[2]))
