"""Check the reports of an agent investigation against the evidence (#283).

An investigator's report is a claim. This script checks each claim it can
check with an executable step, and it does not use the investigator's tools to
do so:

- Every quoted Recording value is decoded again from the MCAP file with the
  Schema the Recording stores, by this script's own decoder, at the quoted
  Channel, publication time, same-time ordinal and field. Every quoted JSON
  value is read again at its JSON Pointer. The artifact digest must match.
- Every cited experiment record is checked against the files: the spec, the
  Manifest, the lock and the summary digests; the parent lock; the contract
  and references, which must be those of the parent bundle; the Manifest,
  which may differ from the parent's on the input Channels' Latency and
  Interceptors only; the determinism hashes; and each comparison verdict and
  first divergence, which `sil.compare` computes again from the files. The
  values of a first divergence are decoded again as well.
- The outcome must fit the facts: `no-failure` needs a passed case,
  `execution-failure` a failure report of the case's Run, `diagnosed` an
  experiment and a preserved regression case that fails against the parent
  case's references, `inconclusive` the missing evidence, and `incomplete` a
  budget refusal in the ledger.
- The ledger is within its budgets, or has a budget refusal, and no command
  line in it starts a Run outside an experiment.
- With `--digests`, every workspace file that preparation wrote is unchanged.
- With `--reference`, each outcome is compared with the evaluator's expected
  outcome. Whether a report found the known cause is the evaluator's
  judgement, not a check of this script.

A check whose input is absent, for example a bundle that was not retained, is
`unavailable` with the reason, never `pass`.

    python verify.py WORKSPACE [--digests evaluator/digests.json]
        [--reference reference.json] [-o verification.json]

Exit 0 when no check fails, 1 when one fails, 2 for a usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys
from datetime import datetime
from pathlib import Path

from mcap.reader import make_reader

from sil.compare import compare, read_contract

FORMAT = 1
OUTCOMES = {"diagnosed", "no-failure", "execution-failure", "inconclusive",
            "incomplete"}
REPORT_KEYS = {"sil_investigation_report", "case", "outcome", "summary",
               "observations", "hypotheses", "experiments", "conclusions",
               "unresolved_alternatives", "missing_evidence", "regression_case"}
# Little-endian, declared order, no padding: the Schema byte contract of
# docs/running.md, written down here a second time on purpose.
STRUCT = {"u8": "B", "u16": "H", "u32": "I", "u64": "Q", "i8": "b", "i16": "h",
          "i32": "i", "i64": "q", "f32": "f", "f64": "d"}
INPUT_KEYS = ("latency_ns", "interceptors")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Checks:
    def __init__(self):
        self.items: list[dict] = []

    def add(self, check: str, subject: str, status: str, detail: str = "") -> bool:
        self.items.append({"check": check, "subject": subject, "status": status,
                           "detail": detail})
        return status == "pass"

    def expect(self, check: str, subject: str, ok: bool, detail: str = "") -> bool:
        return self.add(check, subject, "pass" if ok else "fail", "" if ok else detail)


# Independent reading -----------------------------------------------------------


def _decode(schema: dict, data: bytes) -> dict:
    fields, fmt = [], "<"
    for field in schema["fields"]:
        count = field.get("count")
        code = STRUCT[field["type"]]
        if count is None:
            fmt += code
        elif field["type"] == "u8":
            fmt += f"{count}s"
        else:
            fmt += code * count
        fields.append((field["name"], count, field["type"]))
    flat = struct.unpack(fmt, data)
    values, i = {}, 0
    for name, count, kind in fields:
        if count is None or kind == "u8":
            value = flat[i]
            values[name] = value.hex() if isinstance(value, bytes) else value
            i += 1
        else:
            values[name] = list(flat[i:i + count])
            i += count
    return values


def recorded_value(path: Path, channel: str, t_ns: int, ordinal: int,
                   field: str, index: int | None):
    """The decoded value, or raises LookupError with the reason."""
    seen = 0
    with open(path, "rb") as f:
        for schema, ch, message in make_reader(f).iter_messages(log_time_order=False):
            if ch.topic != channel or message.log_time != t_ns:
                continue
            if seen < ordinal:
                seen += 1
                continue
            values = _decode(json.loads(schema.data), message.data)
            if field not in values:
                raise LookupError(f"{channel} has no field {field!r}")
            value = values[field]
            if index is not None:
                if not isinstance(value, list) or not 0 <= index < len(value):
                    raise LookupError(f"{field} has no element {index}")
                value = value[index]
            return value
    raise LookupError(f"no Message {ordinal} on {channel} at t_ns {t_ns}")


def json_value(document, pointer: str):
    if pointer and not pointer.startswith("/"):
        raise LookupError(f"{pointer!r} is not a JSON Pointer")
    value = document
    for token in pointer.split("/")[1:] if pointer else []:
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not token.isdigit() or int(token) >= len(value):
                raise LookupError(f"no element {token!r}")
            value = value[int(token)]
        elif isinstance(value, dict) and token in value:
            value = value[token]
        else:
            raise LookupError(f"no member {token!r}")
    return value


def _same(a, b) -> bool:
    return type(a) is type(b) and a == b if isinstance(a, bool) or isinstance(b, bool) else a == b


# Workspace ---------------------------------------------------------------------


class Workspace:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.investigation = json.loads((self.root / "investigation.json").read_text())
        self.cases = {c["name"]: c for c in self.investigation["cases"]}
        ledger = self.root / "ledger.jsonl"
        self.ledger = ([json.loads(line) for line in ledger.read_text().splitlines()
                        if line] if ledger.is_file() else [])
        matrix = self.root / self.investigation["matrix"]
        self.matrix = json.loads(matrix.read_text()) if matrix.is_file() else None
        # The result of each experiment record, checked once.
        self.records: dict[str, bool] = {}

    def path(self, relative: str) -> Path | None:
        path = (self.root / relative).resolve()
        return path if path.is_relative_to(self.root) else None


def check_observation(ws: Workspace, checks: Checks, case: str, o: dict) -> None:
    subject = f"{case}/{o.get('id', '?')}"
    path = ws.path(o.get("artifact", ""))
    if path is None:
        checks.add("observation", subject, "fail", f"{o.get('artifact')!r} is outside the workspace")
        return
    if not path.is_file():
        checks.add("observation", subject, "unavailable",
                   f"{o.get('artifact')} is not retained here")
        return
    if not checks.expect("observation-digest", subject, sha256(path) == o.get("sha256"),
                         f"{o.get('artifact')} digest {sha256(path)} is not {o.get('sha256')}"):
        return
    try:
        if "pointer" in o:
            actual = json_value(json.loads(path.read_text()), o["pointer"])
        else:
            actual = recorded_value(path, o["channel"], o["t_ns"], o.get("ordinal", 0),
                                    o["field"], o.get("index"))
    except (LookupError, KeyError, ValueError, struct.error) as error:
        checks.add("observation-value", subject, "fail", f"cannot read the value: {error}")
        return
    checks.expect("observation-value", subject, _same(actual, o.get("value")),
                  f"quoted {o.get('value')!r}, the artifact holds {actual!r}")


def _rebased(value, old: str, new: str):
    if isinstance(value, str):
        return new + value[len(old):] if value == old or value.startswith(old + "/") else value
    if isinstance(value, list):
        return [_rebased(v, old, new) for v in value]
    if isinstance(value, dict):
        return {k: _rebased(v, old, new) for k, v in value.items()}
    return value


def sealed_root(bundle: Path) -> str:
    """Where the bundle was sealed: its Manifests name that path, also in a
    retained copy that lives elsewhere."""
    lock = bundle / "bundle.lock.json"
    return json.loads(lock.read_text())["root"] if lock.is_file() else str(bundle)


def manifest_changes(parent: dict, follow_up: dict, inputs: list[str]) -> list[str]:
    """Every difference of the follow-up Manifest except the input Channels'
    Latency and Interceptors."""
    def stripped(doc: dict) -> dict:
        doc = json.loads(json.dumps(doc))
        for name in inputs:
            for key in INPUT_KEYS:
                doc["channels"].get(name, {}).pop(key, None)
        return doc
    a, b = stripped(parent), stripped(follow_up)
    return [key for key in sorted(a.keys() | b.keys()) if a.get(key) != b.get(key)]


def check_divergence_values(checks: Checks, subject: str, divergence: dict | None,
                            actual: Path, reference: Path) -> None:
    if not divergence or divergence.get("kind") != "value":
        return
    try:
        a = recorded_value(actual, divergence["channel"], divergence["actual_publication_ns"],
                           0, divergence["field"], None)
        e = recorded_value(reference, divergence["reference_channel"],
                           divergence["reference_time_ns"], 0, divergence["field"], None)
    except (LookupError, KeyError) as error:
        checks.add("divergence-values", subject, "fail", str(error))
        return
    checks.expect("divergence-values", subject,
                  _same(a, divergence["actual"]) and _same(e, divergence["expected"]),
                  f"decoded actual {a!r} and expected {e!r}, the report says "
                  f"{divergence['actual']!r} and {divergence['expected']!r}")


def check_record(ws: Workspace, checks: Checks, name: str) -> bool:
    if name not in ws.records:
        ws.records[name] = _check_record(ws, checks, name)
    return ws.records[name]


def _check_record(ws: Workspace, checks: Checks, name: str) -> bool:
    home = ws.root / "experiments" / name
    record_path = home / "record.json"
    if not record_path.is_file():
        return checks.add("experiment-record", name, "fail", "no record.json")
    record = json.loads(record_path.read_text())
    ok = True
    files = {"spec": (ws.root / record["spec"]["path"], record["spec"]["sha256"]),
             "summary": (home / "evidence" / "summary.json", record["summary_sha256"]),
             "manifest": (home / "bundle" / f"{name}.json", record["manifest_sha256"]),
             "lock": (home / "bundle" / "bundle.lock.json", record["lock_sha256"])}
    for what, (path, digest) in files.items():
        if path.is_file():
            ok &= checks.expect("experiment-digest", f"{name}/{what}", sha256(path) == digest,
                                f"{path.name} digest differs from the record")
        else:
            checks.add("experiment-digest", f"{name}/{what}", "unavailable",
                       f"{path.name} is not retained here")
    parent = ws.cases.get(record["parent"]["case"])
    ok &= checks.expect("experiment-parent", name,
                        parent is not None
                        and parent["expect_lock"] == record["parent"]["lock_sha256"],
                        "the parent lock is not the case's sealed lock")
    contract = ws.investigation["experiment"]["contract"]
    ok &= checks.expect("assertions-unchanged", f"{name}/contract",
                        record["contract"]["name"] == contract,
                        f"compared under {record['contract']['name']}, not {contract}")
    parent_root = ws.root / record["parent"]["bundle"]
    for artifact, digest in [(contract, record["contract"]["sha256"]),
                             *record["references"].items()]:
        source = parent_root / artifact
        if source.is_file():
            ok &= checks.expect("assertions-unchanged", f"{name}/{artifact}",
                                sha256(source) == digest,
                                f"{artifact} is not the parent bundle's")
        else:
            checks.add("assertions-unchanged", f"{name}/{artifact}", "unavailable",
                       "the parent bundle is not retained here")
    follow_up = home / "bundle" / f"{name}.json"
    declaration = parent_root / "bundle.json"
    if follow_up.is_file() and declaration.is_file():
        (parent_run,) = json.loads(declaration.read_text())["runs"]
        changes = manifest_changes(
            _rebased(json.loads((parent_root / parent_run["manifest"]).read_text()),
                     sealed_root(parent_root), sealed_root(home / "bundle")),
            json.loads(follow_up.read_text()),
            ws.investigation["experiment"]["input_channels"])
        ok &= checks.expect("intervention-scope", name, not changes,
                            f"the Manifest also changes {changes}")
    else:
        checks.add("intervention-scope", name, "unavailable",
                   "the Manifests are not retained here")
    ok &= _check_results(checks, name, record, home)
    return ok


def _check_results(checks: Checks, name: str, record: dict, home: Path) -> bool:
    ok = True
    (result,) = record["result"]
    runs = home / "evidence" / "runs" / name
    first, second = runs / "run-1.mcap", runs / "run-2.mcap"
    determinism = result.get("determinism")
    if determinism and first.is_file() and second.is_file():
        hashes = [sha256(first), sha256(second)]
        ok &= checks.expect("determinism", name,
                            hashes == determinism["recording_sha256"]
                            and (hashes[0] == hashes[1]) == (determinism["verdict"] == "pass"),
                            "the Recordings do not have the recorded digests or verdict")
    elif determinism:
        checks.add("determinism", name, "unavailable", "the Recordings are not retained here")
    bundle = home / "bundle"
    for comparison, verdict in result.get("comparisons", {}).items():
        subject = f"{name}/{comparison}"
        reference = bundle / f"{comparison}.expected.mcap"
        contract = bundle / record["contract"]["name"]
        if not (first.is_file() and reference.is_file() and contract.is_file()):
            checks.add("comparison-verdict", subject, "unavailable",
                       "the Recording, reference or contract is not retained here")
            continue
        report = compare(read_contract(contract), first, reference)
        ok &= checks.expect(
            "comparison-verdict", subject,
            report["verdict"] == verdict["verdict"]
            and report["first_divergence"] == record["first_divergences"].get(comparison),
            f"recomputed {report['verdict']} with first divergence "
            f"{report['first_divergence']}, recorded {verdict['verdict']}")
        check_divergence_values(checks, subject, report["first_divergence"], first, reference)
    return ok


def case_run_failed(ws: Workspace, case: str) -> bool | None:
    """Whether the case's original Run failed with a failure report; None
    when its Run evidence is not available."""
    runs = ws.root / "ci" / "cases" / case / "runs" / case
    if not runs.is_dir():
        return None
    return (runs / "run-1.mcap.failure.json").is_file()


def case_status(ws: Workspace, case: str) -> str | None:
    for entry in (ws.matrix or {}).get("cases", []):
        if entry["name"] == case:
            return entry["status"]
    return None


def check_regression_case(ws: Workspace, checks: Checks, case: str,
                          name: str) -> None:
    """The preserved case must fail against every reference of the parent
    case's Run, under the unchanged contract."""
    record = json.loads((ws.root / "experiments" / name / "record.json").read_text())
    declaration = ws.root / record["parent"]["bundle"] / "bundle.json"
    if not declaration.is_file():
        checks.add("regression-case", case, "unavailable",
                   "the parent bundle is not retained here")
        return
    (parent_run,) = json.loads(declaration.read_text())["runs"]
    original = {c["reference"] for c in parent_run["comparisons"]}
    (result,) = record["result"]
    checks.expect("regression-case", case,
                  original <= set(record["references"]) and result["verdict"] == "fail",
                  f"{name} must fail against the parent's references {sorted(original)}")


def check_outcome(ws: Workspace, checks: Checks, case: str, report: dict) -> None:
    outcome = report["outcome"]
    failed = case_run_failed(ws, case)
    if outcome == "no-failure":
        checks.expect("outcome-facts", case, case_status(ws, case) == "pass",
                      f"the matrix status is {case_status(ws, case)}, not pass")
    elif outcome == "execution-failure":
        checks.expect("outcome-facts", case, failed is True,
                      "the case's Run wrote no failure report")
    elif outcome == "diagnosed":
        named = [e.get("name") for e in report["experiments"]]
        regression = report.get("regression_case")
        checks.expect("outcome-facts", case,
                      failed is False and bool(named) and regression in named
                      and all(check_record(ws, checks, n) for n in named),
                      "a diagnosis needs a completed Run, verified experiments and "
                      "a preserved regression case among them")
        if regression in named:
            check_regression_case(ws, checks, case, regression)
    elif outcome == "inconclusive":
        checks.expect("outcome-facts", case, bool(report["missing_evidence"]),
                      "an inconclusive report must name the missing evidence")
    elif outcome == "incomplete":
        checks.expect("outcome-facts", case,
                      any(e["kind"] == "refusal" and "budget exhausted" in e["reason"]
                          for e in ws.ledger),
                      "no budget refusal in the ledger")


LIST_OF_OBJECTS = ("observations", "hypotheses", "experiments", "conclusions")
LIST_OF_STRINGS = ("unresolved_alternatives", "missing_evidence")


def structure_problems(report: dict) -> list[str]:
    problems = [f"{key} must be a list of objects" for key in LIST_OF_OBJECTS
                if not isinstance(report[key], list)
                or not all(isinstance(item, dict) for item in report[key])]
    problems += [f"{key} must be a list of strings" for key in LIST_OF_STRINGS
                 if not isinstance(report[key], list)
                 or not all(isinstance(item, str) for item in report[key])]
    if report["regression_case"] is not None and not isinstance(report["regression_case"], str):
        problems.append("regression_case must be a name or null")
    return problems


def check_report(ws: Workspace, checks: Checks, case: str, path: Path) -> str | None:
    try:
        report = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        checks.add("report-format", case, "fail", str(error))
        return None
    problems = []
    if not isinstance(report, dict) or report.keys() != REPORT_KEYS:
        problems.append(f"keys must be {sorted(REPORT_KEYS)}")
    elif report["sil_investigation_report"] != FORMAT or report["case"] != case \
            or report["outcome"] not in OUTCOMES:
        problems.append("version, case or outcome is not valid")
    else:
        problems += structure_problems(report)
    if not checks.expect("report-format", case, not problems, "; ".join(problems)):
        return None
    ids = {o.get("id") for o in report["observations"]}
    hypotheses = {h.get("id") for h in report["hypotheses"]}
    experiments = {e.get("name") for e in report["experiments"]}
    dangling = sorted(
        {t for e in report["experiments"] for t in e.get("tests", [])} - hypotheses
        | {o for e in report["experiments"] for o in e.get("observations", [])} - ids
        | {s for c in report["conclusions"] for s in c.get("supported_by", [])}
        - ids - experiments)
    checks.expect("report-references", case, not dangling,
                  f"names no observation, hypothesis or experiment: {dangling}")
    for observation in report["observations"]:
        check_observation(ws, checks, case, observation)
    for name in experiments:
        check_record(ws, checks, name)
    check_outcome(ws, checks, case, report)
    return report["outcome"]


def metrics(ws: Workspace) -> dict:
    tools = [e for e in ws.ledger if e["kind"] == "tool"]
    return {
        "tool_calls": len(tools),
        "tool_output_bytes": sum(e["delivered_bytes"] for e in tools),
        "experiments": sum(1 for e in ws.ledger if e["kind"] == "experiment"),
        "runs": sum(e.get("runs", 0) for e in ws.ledger),
        "refusals": sum(1 for e in ws.ledger if e["kind"] == "refusal"),
        "budget_refusals": sum(1 for e in ws.ledger if e["kind"] == "refusal"
                               and "budget exhausted" in e["reason"]),
        "first_action_utc": ws.ledger[0]["utc"] if ws.ledger else None,
        "last_action_utc": ws.ledger[-1]["utc"] if ws.ledger else None,
        "elapsed_s": int((datetime.fromisoformat(ws.ledger[-1]["utc"])
                          - datetime.fromisoformat(ws.ledger[0]["utc"])).total_seconds())
        if ws.ledger else 0,
    }


# A Run that a `python3` or `find` command line starts, outside the driver.
RUN_STARTED = re.compile(r"sil-run|\bsil\W+(run|check)\b|\bbundle\W+(run|matrix)\b")


def runs_outside_the_driver(ws: Workspace) -> list[int]:
    return [e["seq"] for e in ws.ledger if e["kind"] == "tool"
            and RUN_STARTED.search(" ".join(e["argv"]))]


def verify(root: Path, digests: Path | None = None,
           reference: Path | None = None) -> dict:
    ws = Workspace(root)
    checks = Checks()
    if digests is not None:
        for name, digest in json.loads(digests.read_text()).items():
            path = ws.root / name
            checks.expect("original-unchanged", name,
                          path.is_file() and sha256(path) == digest,
                          "changed or removed after preparation")
    expected = json.loads(reference.read_text())["cases"] if reference else {}
    outcomes = {}
    for case in ws.cases:
        path = ws.root / "reports" / f"{case}.json"
        if not path.is_file():
            checks.add("report-format", case, "fail", "no report")
            continue
        outcomes[case] = check_report(ws, checks, case, path)
        if case in expected:
            checks.expect("expected-outcome", case,
                          outcomes[case] == expected[case]["outcome"],
                          f"reported {outcomes[case]}, the evaluator expects "
                          f"{expected[case]['outcome']}")
    m = metrics(ws)
    budgets = ws.investigation["budgets"]
    within = all(m[k] <= budgets[k]
                 for k in ("runs", "elapsed_s", "tool_calls", "tool_output_bytes"))
    outside = runs_outside_the_driver(ws)
    checks.expect("runs-in-experiments", "ledger", not outside,
                  f"ledger entries {outside} start a Run outside an experiment")
    checks.add("budget", "ledger", "pass" if within or m["budget_refusals"] else "fail",
               "" if within else "over budget without a refusal")
    return {"sil_investigation_verification": FORMAT,
            "passed": not any(c["status"] == "fail" for c in checks.items),
            "outcomes": outcomes, "metrics": m,
            "counts": {s: sum(1 for c in checks.items if c["status"] == s)
                       for s in ("pass", "fail", "unavailable")},
            "checks": checks.items}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--digests", type=Path,
                        help="the digests preparation wrote (evaluator/digests.json)")
    parser.add_argument("--reference", type=Path,
                        help="the evaluator's expected outcomes")
    parser.add_argument("-o", "--output", type=Path, help="write the result here")
    args = parser.parse_args(argv)
    result = verify(args.workspace, args.digests, args.reference)
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    for check in result["checks"]:
        if check["status"] != "pass":
            print(f"{check['status']}: {check['check']} {check['subject']}: {check['detail']}")
    print(f"{'passed' if result['passed'] else 'failed'}: {json.dumps(result['counts'])}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
