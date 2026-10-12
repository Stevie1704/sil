"""Retain a finished trial of the agent investigation proof (#283).

Copies the evidence of the trial from the investigator's workspace `OUT/agent`
into the evidence directory: everything outside the bundles, and of each
bundle only what `verify.py` reads or a report cites. Those are the
declaration, the lock, the Manifests, the contract and the references that a
Run compared with. The other bundle files (conversion inputs, receipts, the
input Recording, unused references, the library builds) are the same bytes
that `prepare.py` writes again from the source tree. The driver copy is
`investigate.py` of this directory.

Then it runs `verify.py` on the full workspace, with the preparation digests
and the evaluator's reference, and keeps that result as `verification.json`.

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


def bundle_evidence(bundle: Path, compared: set[str]) -> set[Path]:
    """The files of `bundle` that a check reads: `compared` names the
    references that a Run of any bundle compared with."""
    declaration = json.loads((bundle / "bundle.json").read_text())
    names = {"bundle.json", "bundle.lock.json"}
    names |= {name for name, role in declaration["artifacts"].items()
              if role in ("manifest", "contract")
              or (role == "reference" and name in compared)}
    return {bundle / name for name in names}


def kept_files(agent: Path) -> set[Path]:
    bundles = [*agent.glob("bundles/*"), *agent.glob("experiments/*/bundle")]
    compared = {c["reference"]
                for bundle in bundles
                for run in json.loads((bundle / "bundle.json").read_text())["runs"]
                for c in run.get("comparisons", [])}
    cited = {agent / o["artifact"]
             for report in agent.glob("reports/*.json")
             for o in json.loads(report.read_text()).get("observations", [])
             if isinstance(o, dict) and isinstance(o.get("artifact"), str)}
    kept = {path for path in agent.rglob("*")
            if path.is_file() and not any(path.is_relative_to(b) for b in bundles)
            and path.relative_to(agent).parts[0] != "tools"}
    for bundle in bundles:
        kept |= bundle_evidence(bundle, compared)
    return {path for path in kept | cited
            if path.is_file() and path.suffix not in SKIPPED_SUFFIXES
            and "__pycache__" not in path.parts}


def retain(out: Path, evidence: Path) -> dict:
    agent = out / "agent"
    if evidence.exists():
        raise SystemExit(f"{evidence} exists; name a new directory")
    for path in sorted(kept_files(agent)):
        target = evidence / path.relative_to(agent)
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
