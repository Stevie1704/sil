"""Retain compact review evidence from a downloaded #330 workflow artifact.

    python retain-review.py ARTIFACT EVIDENCE

Keeps the unshimmed control Recordings, clock audit/clock-record projection,
and availability reports with nonempty logs. Large captures, complete symbol
tables and generated binaries remain in the source workflow artifact.
"""
from __future__ import annotations

import json
import shutil
import sys
from collections import Counter
from pathlib import Path

from evidence import digest, save


def main(artifact: Path, evidence: Path):
    evidence.mkdir(parents=True, exist_ok=True)
    groups = ("heaptrack-shim-evidence", "heaptrack-shim-clock",
              "heaptrack-shim-bpf-native", "heaptrack-shim-bpf-container")
    for group in groups:
        root = artifact / group
        assert root.is_dir(), f"missing evidence group: {group}"
        for source in sorted(root.rglob("*")):
            if not source.is_file():
                continue
            relative = source.relative_to(root)
            keep = source.name in {"results.json", "availability.json", "image-id.txt"}
            if group == "heaptrack-shim-evidence":
                keep |= source.name == "run.mcap" and relative.parts[0].startswith("unshimmed-")
            elif group == "heaptrack-shim-clock":
                keep |= source.name in {"audit.bin", "maps.txt"}
            else:
                keep |= source.suffix in {".bt", ".c"} or (
                    source.name.endswith((".stdout.txt", ".stderr.txt")) and source.stat().st_size > 0)
            if keep:
                target = evidence / group / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        if group == "heaptrack-shim-clock":
            for mode in ("shimmed", "unshimmed"):
                capture = root / mode / "heaptrack.raw"
                counts = Counter(int(line.split()[1], 16) for line in capture.read_bytes().splitlines()
                                 if line.startswith(b"c "))
                save(evidence / group / mode / "timestamp-records.json", {
                    "source": digest(capture), "counts_by_value_ms": sorted(counts.items()),
                    "projection": "all raw c record values and multiplicities; order omitted"})
                symbols = root / mode / "caller-symbols.json"
                document = json.loads(symbols.read_text())
                save(evidence / group / mode / "steady-clock-symbols.json", {
                    "source": digest(symbols), "command": document["command"],
                    "matches": [line for line in document["stdout"].splitlines()
                                if "steady_clock3now" in line]})
    save(evidence / "digests.json", {str(p.relative_to(evidence)): digest(p)
        for p in sorted(evidence.rglob("*"))
        if p.is_file() and p.name not in {"digests.json", "origin.json"}})


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
