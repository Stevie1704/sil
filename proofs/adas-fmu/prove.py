"""Prepare and qualify the ADAS reference FMU in the preparation image.

1. Build the archive twice, in separate scratch directories, and require
   identical bytes. When `evidence/AdasReference.identity.json` is
   committed, require the same archive digest: the pin.
2. Audit the interface (audit.py).
3. Drive the archive with FMPy against the authored expectations
   (check.py).
4. Build the wrong-sign control from the same sources and require the
   check to fail on the hazard maneuvers: the expectations detect a sign
   error that native/FMU agreement could not.

    python proofs/adas-fmu/prove.py OUT_DIR

writes the archive, its identity and every report into OUT_DIR, and exits 1
when a step fails.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROOF_DIR = Path(__file__).resolve().parent
ROOT = PROOF_DIR.parents[1]
sys.path.insert(0, str(ROOT / "examples" / "adas-reference" / "fmu"))
import package  # noqa: E402

import audit  # noqa: E402
import check  # noqa: E402

PIN = PROOF_DIR / "evidence" / "AdasReference.identity.json"
CONTROL_DEFINE = "ADAS_REFERENCE_WRONG_SIGN"
# The maneuvers with a closing object, which a sign error turns into CLEAR.
CONTROL_FAILURES = {"maneuver_hazard", "maneuver_release",
                    "maneuver_boundaries", "maneuver_ordering",
                    "two_instances"}


def reproduce(out_dir: Path) -> dict:
    first = package.build(out_dir / "first")
    second = package.build(out_dir / "second")
    fmu = out_dir / "AdasReference.fmu"
    fmu.write_bytes((out_dir / "first" / "AdasReference.fmu").read_bytes())
    (out_dir / "AdasReference.identity.json").write_text(
        json.dumps(first, indent=2) + "\n")
    pinned = json.loads(PIN.read_text()) if PIN.exists() else None
    return {
        "first_sha256": first["archive_sha256"],
        "second_sha256": second["archive_sha256"],
        "identical": (out_dir / "first" / "AdasReference.fmu").read_bytes()
        == (out_dir / "second" / "AdasReference.fmu").read_bytes(),
        "pinned_sha256": pinned and pinned["archive_sha256"],
        "matches_pin": pinned is None
        or pinned["archive_sha256"] == first["archive_sha256"],
    }


def control(out_dir: Path) -> dict:
    built = package.build(out_dir, defines=(CONTROL_DEFINE,))
    report = check.check(out_dir / "AdasReference.fmu", out_dir)
    failed = {r["case"] for r in report["cases"] if not r["passed"]}
    return {"define": CONTROL_DEFINE,
            "archive_sha256": built["archive_sha256"],
            "failed": sorted(failed),
            "expected_failures": sorted(CONTROL_FAILURES),
            "detected": failed == CONTROL_FAILURES}


def prove(out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    reproduced = reproduce(out_dir)
    fmu = out_dir / "AdasReference.fmu"
    findings = audit.audit(fmu, out_dir)
    checked = check.check(fmu, out_dir)
    controlled = control(out_dir / "control")
    (out_dir / "control.json").write_text(
        json.dumps(controlled, indent=2) + "\n")
    summary = {
        "archive_sha256": reproduced["first_sha256"],
        "reproducible": reproduced,
        "audit_findings": findings,
        "check_passed": checked["passed"],
        "control": controlled,
    }
    summary["passed"] = (reproduced["identical"] and reproduced["matches_pin"]
                         and not findings and checked["passed"]
                         and controlled["detected"])
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out_dir", type=Path, help="where the evidence goes")
    summary = prove(parser.parse_args().out_dir)
    print(json.dumps(summary, indent=2))
    sys.exit(0 if summary["passed"] else 1)
