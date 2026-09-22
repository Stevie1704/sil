"""Retain a compact report; full trajectories and binaries stay in CI artifacts.

Run automatically by closed_loop.py, or repeat offline:
    python proofs/acc-fmi/loop_evidence.py RAW_DIRECTORY REPORT_DIRECTORY
"""
import json
import shutil
import sys
from pathlib import Path

from proof_support import file_sha256, write_json


def retain(source, target):
    target.mkdir(parents=True, exist_ok=True)
    report = {name: json.loads((source / f"{name}.json").read_text())
              for name in ("results", "environment", "configuration")}
    report["fmus"] = {name: json.loads((source / f"{name}.identity.json").read_text())
                      for name in ("AccController", "AccPlant")}
    report["artifacts_sha256"] = {p.name: file_sha256(p) for p in sorted(source.iterdir())
                                  if p.is_file() and p.suffix in (".json", ".mcap", ".fmu")}
    write_json(target / "report.json", report)
    # The host adds this after container execution; offline regeneration copies it.
    if (source / "image-id.txt").exists():
        shutil.copyfile(source / "image-id.txt", target / "image-id.txt")


if __name__ == "__main__":
    retain(Path(sys.argv[1]), Path(sys.argv[2]))
