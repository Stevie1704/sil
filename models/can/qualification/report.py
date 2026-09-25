"""Bind retained results to the exact model, test inputs and runner."""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_support import PROFILE, digest

ROOT = Path(__file__).resolve().parents[3]
OUTPUT = ROOT / "build/can"
# The sanitizers qualify.sh actually used; an emulated host cannot run ASan.
SANITIZERS = (OUTPUT / "sanitizers.txt").read_text().split(":")[1].strip()


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
        "arbitration": "three-node exact traces, buffer/discard, equal-ID and FIFO cases",
        "same_manifest": "repeat Recording bytes with forward/reverse terminal declarations",
        "rejection": "malformed/unsupported traffic, full queues and conflicting equal IDs",
        "fault_schedule": "expected and observed baseline, Bus Error, retry, suppression and no-match tables through independent FMPy and SiL paths",
        "retry_exhaustion": "same final Bus Error and request-drop semantics asserted through FMPy and SiL",
        "co_transmission": "identical-timestamp senders, primary identity and single sender flag asserted through FMPy and SiL",
        "rule_precedence": "overlapping rules in both orders asserted through FMPy and SiL",
        "delivery_suppression": "named receiver omission with no Bus Error or retry asserted through FMPy and SiL",
        "notification_fixture_scope": "first-party adapted consumer exercises the SiL path; it is not independent implementation compatibility evidence",
        "bus_error_source": "FMI-LS-BUS 1.0.0 Network Abstraction Tables 14-16",
        "error_confinement_scope": "fixed FMI-LS-BUS Bit Error notification; no electrical/error-counter claim",
        "core_sanitizers": f"{SANITIZERS} sanitizers: truncation and single-byte mutations passed",
        "format_error": "corrupt operations answered with FMI-LS-BUS Format Error to the sender at the next nanosecond; unsupported well-formed operations return fmi3Error",
        "abi_sanitizers": f"{SANITIZERS} sanitizers: lifecycle, reset, free, interleaved instances, output/callback lifetime and bounded malformed corpus through the C entry points (abi.json)",
        "capacity": "declared per-instance limits and measured C++ heap reported separately (capacity.json); not OS memory isolation",
        "release_staging": "two controlled builds byte-identical before the versioned archive, release.json identities and SHA256SUMS are staged (release/)",
        "packaged_rebuild": "sources/build.sh rebuilds the identical shared library without Python; NEEDED entries limited to the C and C++ runtime",
        "example": "documented example configuration reproduced by the independent FMPy master against a hand-derived table and by SiL with repeat-identical Recordings (example/)",
    },
}
(OUTPUT / "qualification.json").write_text(
    json.dumps(report, indent=2, sort_keys=True) + "\n"
)
