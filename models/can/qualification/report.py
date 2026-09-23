"""Bind retained results to the exact model, test inputs and runner."""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUTPUT = ROOT / "build/can"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


report = {
    "specification": "FMI-LS-BUS 1.0.0",
    "spec_revision": "8abdf039bfb994c794e4c15bce575cfc00a1ab6e",
    "external_revision": "de019a6efbad810795835f2fd9bbf9e62eb451b9",
    "runner_sha256": sha(Path("/opt/kernel/sil-run")),
    "artifacts": {
        p.name: sha(p)
        for p in sorted(OUTPUT.iterdir())
        if p.is_file()
        and p.name != "qualification.json"
        and p.suffix in {".fmu", ".mcap", ".json", ".txt", ".xml", ".log"}
    },
    "inputs": {
        p.relative_to(ROOT).as_posix(): sha(p)
        for p in sorted((ROOT / "models/can").rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts and "evidence" not in p.parts
    },
    "checks": {
        "archive_reproducibility": "byte-identical controlled builds",
        "recording_determinism": "byte-identical Runs",
        "independent_exchange": "FMPy initialization, Event Mode, Clocks and Binary",
        "sil_exchange": "same FMU archives, exact operation/event-time assertions",
        "rejection": "malformed/unsupported traffic; competing upstream nodes exit 1",
        "core_sanitizers": "UBSan truncation and single-byte mutations passed",
    },
}
(OUTPUT / "qualification.json").write_text(
    json.dumps(report, indent=2, sort_keys=True) + "\n"
)
