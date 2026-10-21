"""Retain small, digest-verifiable experiment evidence from a workflow artifact.

    python retain.py ARTIFACT/heaptrack-shim-evidence proofs/heaptrack-shim/evidence

Raw captures, Recordings and compiled binaries stay in the workflow artifact.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path


def main(artifact: Path, evidence: Path):
    inventory = json.loads((artifact / "digests.json").read_text())
    for relative, expected in inventory.items():
        source = artifact / relative
        if source.suffix == ".json" or source.name.endswith("stderr.txt") or \
                source.name == "stdout.txt":
            assert {"sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "bytes": source.stat().st_size} == expected, relative
            target = evidence / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    shutil.copy2(artifact / "digests.json", evidence / "digests.json")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
