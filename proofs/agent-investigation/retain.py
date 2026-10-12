"""Retain a finished trial of the agent investigation proof (#283).

Copies the investigator's workspace `OUT/agent` into the evidence directory,
without the shared-library builds and the driver copy, which are not
evidence of the trial. Then it runs `verify.py` on the full workspace, with
the preparation digests and the evaluator's reference, and keeps that result
as `verification.json`. On the retained copy, the checks that need a library
or a moved bundle report `unavailable`.

    python retain.py OUT proofs/agent-investigation/evidence/trial-N
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import verify

HERE = Path(__file__).resolve().parent
SKIPPED_SUFFIXES = {".so", ".dylib", ".pyc"}


def retain(out: Path, evidence: Path) -> dict:
    agent = out / "agent"
    if evidence.exists():
        raise SystemExit(f"{evidence} exists; name a new directory")
    for path in sorted(agent.rglob("*")):
        relative = path.relative_to(agent)
        if (not path.is_file() or path.suffix in SKIPPED_SUFFIXES
                or relative.parts[0] == "tools" or "__pycache__" in relative.parts):
            continue
        target = evidence / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    result = verify.verify(agent, digests=out / "evaluator" / "digests.json",
                           reference=HERE / "reference.json")
    (evidence / "verification.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path, help="the directory prepare.py wrote")
    parser.add_argument("evidence", type=Path, help="a new directory")
    args = parser.parse_args()
    result = retain(args.out.resolve(), args.evidence)
    print(f"{'passed' if result['passed'] else 'failed'}: {json.dumps(result['counts'])}")


if __name__ == "__main__":
    main()
