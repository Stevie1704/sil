"""Copy the retained part of a qualification artifact (#319).

    python retain.py ARTIFACT EVIDENCE

ARTIFACT is the downloaded `profiling-qualification-evidence` workflow
artifact. EVIDENCE receives, per environment, `results.json` (environment
identity, every command with its exit code, checks and observations, and
the SHA-256 of each raw capture), `results.md`, `docker-options.txt` and
`image-id.txt`, plus the artifact's `digests.txt` and
`host-paranoid-before.txt`. The profile summaries, the portable sets and
the HTML pages stay in the artifact; `digests.txt` has the SHA-256 of each.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

RETAINED = {"results.json", "results.md", "docker-options.txt",
            "image-id.txt", "digests.txt", "host-paranoid-before.txt"}
SKIPPED = {"profiles", "sets", "result-work"}


def main(artifact: Path, evidence: Path) -> int:
    for source in sorted(artifact.rglob("*")):
        relative = source.relative_to(artifact)
        if not source.is_file() or source.name not in RETAINED or \
                SKIPPED & set(relative.parts):
            continue
        target = evidence / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2])))
