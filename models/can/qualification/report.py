"""Bind retained results to the exact model, test inputs and runner."""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_support import PROFILE, digest

ROOT = Path(__file__).resolve().parents[3]
OUTPUT = ROOT / "build/can"


report = {
    "specification": PROFILE["specification"],
    "spec_revision": PROFILE["upstream"]["spec"]["revision"],
    "external_revision": PROFILE["upstream"]["examples"]["revision"],
    "runner_sha256": digest(Path("/opt/kernel/sil-run")),
    "artifacts": {
        p.name: digest(p)
        for p in sorted(OUTPUT.iterdir())
        if p.is_file()
        and p.name != "qualification.json"
        and p.suffix in {".fmu", ".mcap", ".json", ".txt", ".xml", ".log"}
    },
    "inputs": {
        p.relative_to(ROOT).as_posix(): digest(p)
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
