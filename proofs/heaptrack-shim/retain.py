"""Retain small, digest-verifiable experiment evidence from a workflow artifact.

    python retain.py ARTIFACT/heaptrack-shim-evidence proofs/heaptrack-shim/evidence

Small Recordings are retained for byte comparisons; raw captures and compiled
binaries stay in the workflow artifact.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from evidence import digest


def main(artifact: Path, evidence: Path):
    inventory = json.loads((artifact / "digests.json").read_text())
    for relative, expected in inventory.items():
        source = artifact / relative
        if source.suffix in {".json", ".mcap"} or source.name.endswith("stderr.txt") or \
                source.name == "stdout.txt":
            assert digest(source) == expected, relative
            target = evidence / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    shutil.copy2(artifact / "digests.json", evidence / "digests.json")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
