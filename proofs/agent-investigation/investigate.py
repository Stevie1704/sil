"""The investigation driver of the agent investigation proof (issue #283).

It gives an investigator two actions in its workspace, and counts both
against the budgets of `investigation.json`:

    python3 tools/investigate.py tool -- COMMAND [ARG]...
    python3 tools/investigate.py experiment specs/NAME.json
    python3 tools/investigate.py status

`tool` runs one allowed command and passes its output through. It counts
the exact bytes it passes on. Only the commands in `READERS` and the `sil`
commands in `SIL_COMMANDS` are allowed, and every path argument must be in
the workspace. A `sil` command that executes a Run is refused: a Run is an
experiment. `python3`, `find` and `sort` can still write files or start a
process; the driver cannot stop that. The ledger records every command line,
and `verify.py` fails a ledger with a Run started that way.

`experiment` runs one follow-up experiment of a case. The spec names the
parent case, the hypothesis, why the experiment separates the explanations,
the changed input Channels and the prepared references to compare with. The
driver copies the parent's bundle into `experiments/NAME/bundle` with a new
Manifest, seals it, runs it with `sil bundle run` into
`experiments/NAME/evidence`, and writes `experiments/NAME/record.json`. The
parent bundle, its contract and its references do not change.

Each action appends one line to `ledger.jsonl`. When a budget is exhausted,
the driver refuses every later action with exit 65; a command or spec that is
not allowed exits 64. Otherwise `tool` exits with the command's exit code and
`experiment` with that of `sil bundle run`.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
LEDGER = WORKSPACE / "ledger.jsonl"
EXIT_NOT_ALLOWED = 64
EXIT_BUDGET = 65
SPEC_FORMAT = 1
RECORD_FORMAT = 1

# The driver is copied into the workspace and runs alone, so it shares no
# helper with prepare.py or verify.py.
READERS = {"cat", "head", "tail", "ls", "wc", "grep", "diff", "cmp", "find",
           "sort", "uniq", "cut", "shasum", "sha256sum", "python3"}
# The read-only `sil` commands, by their first one or two words.
SIL_COMMANDS = {("recording", "channels"), ("recording", "show"), ("compare",),
                ("bundle", "verify"), ("manifest", "validate"), ("docs",),
                ("fmi", "inspect")}
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


class NotAllowed(Exception):
    """An action outside the investigation's allowed actions."""


class Exhausted(Exception):
    """An action after a budget was exhausted."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def investigation() -> dict:
    return json.loads((WORKSPACE / "investigation.json").read_text())


def ledger() -> list[dict]:
    if not LEDGER.is_file():
        return []
    return [json.loads(line) for line in LEDGER.read_text().splitlines() if line]


def append(entry: dict) -> None:
    entries = ledger()
    entry = {"seq": len(entries) + 1, "utc": _now(), **entry}
    with LEDGER.open("a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


def usage(entries: list[dict]) -> dict:
    """What the investigation has used. Elapsed time counts from the first
    action, so reading the brief does not count."""
    started = (datetime.fromisoformat(entries[0]["utc"]) if entries
               else datetime.now(timezone.utc))
    return {
        "runs": sum(e.get("runs", 0) for e in entries),
        "elapsed_s": int((datetime.now(timezone.utc) - started).total_seconds()),
        "tool_calls": sum(1 for e in entries if e["kind"] == "tool"),
        "tool_output_bytes": sum(e.get("delivered_bytes", 0) for e in entries),
    }


def check_budget(runs_needed: int = 0) -> None:
    budgets = investigation()["budgets"]
    used = usage(ledger())
    used["runs"] += runs_needed
    exhausted = [name for name in ("runs", "elapsed_s", "tool_calls",
                                   "tool_output_bytes")
                 if used[name] > budgets[name]
                 or (name != "runs" and used[name] == budgets[name])]
    if exhausted:
        raise Exhausted(f"budget exhausted: {', '.join(exhausted)} "
                        f"(used {json.dumps(usage(ledger()))}, "
                        f"budgets {json.dumps(budgets)})")


def environment() -> dict[str, str]:
    runtime = investigation()["runtime"]
    return {"PATH": f"{runtime['python_bin']}:{runtime['sil_bin']}:/usr/bin:/bin",
            "PYTHONNOUSERSITE": "1", "LC_ALL": "C"}


# tool ---------------------------------------------------------------------------


def check_command(argv: list[str]) -> None:
    if not argv:
        raise NotAllowed("name a command after `tool --`")
    program = argv[0]
    if program == "sil":
        words = [a for a in argv[1:] if not a.startswith("-")]
        if "--help" not in argv and not any(
                tuple(words[:len(c)]) == c for c in SIL_COMMANDS):
            raise NotAllowed(
                f"`sil {' '.join(words[:2])}` is not allowed; allowed: "
                + ", ".join("sil " + " ".join(c) for c in sorted(SIL_COMMANDS))
                + ". A Run is an experiment: use `investigate.py experiment`")
    elif program not in READERS:
        raise NotAllowed(f"{program!r} is not allowed; allowed: sil, "
                         + ", ".join(sorted(READERS)))
    for arg in argv[1:]:
        if "/" in arg or arg.startswith(".."):
            path = (WORKSPACE / arg.split("=", 1)[-1]).resolve()
            if not path.is_relative_to(WORKSPACE):
                raise NotAllowed(f"{arg!r} is outside the workspace {WORKSPACE}")


def tool(argv: list[str]) -> int:
    check_command(argv)
    check_budget()
    limit = investigation()["tool_output_limit"]
    started = time.monotonic()
    proc = subprocess.run(argv, cwd=WORKSPACE, env=environment(),
                          capture_output=True)
    output = proc.stdout + proc.stderr
    delivered = output[:limit]
    sys.stdout.buffer.write(delivered)
    if len(output) > limit:
        notice = (f"\n[investigate: output truncated: {limit} of {len(output)} "
                  "bytes delivered; narrow the command]\n").encode()
        sys.stdout.buffer.write(notice)
        delivered += notice
    sys.stdout.flush()
    append({"kind": "tool", "argv": argv, "exit_code": proc.returncode,
            "stdout_bytes": len(proc.stdout), "stderr_bytes": len(proc.stderr),
            "delivered_bytes": len(delivered),
            "elapsed_s": round(time.monotonic() - started, 3)})
    return proc.returncode


# experiment ---------------------------------------------------------------------


def _object(value, context: str, required: set, optional: set = frozenset()) -> dict:
    if not isinstance(value, dict):
        raise NotAllowed(f"{context} must be an object")
    missing, unknown = required - value.keys(), value.keys() - required - optional
    if missing or unknown:
        raise NotAllowed(f"{context}: missing {sorted(missing)}, not allowed "
                         f"{sorted(unknown)}")
    return value


def read_spec(path: Path) -> dict:
    try:
        spec = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise NotAllowed(f"cannot read the spec {path}: {error}") from error
    _object(spec, "spec", {"sil_investigation_experiment", "name", "parent",
                           "hypothesis", "distinguishes", "channels", "references"})
    if spec["sil_investigation_experiment"] != SPEC_FORMAT:
        raise NotAllowed(f"sil_investigation_experiment must be {SPEC_FORMAT}")
    if not isinstance(spec["name"], str) or not NAME.fullmatch(spec["name"]):
        raise NotAllowed(f"name must match {NAME.pattern}")
    if (WORKSPACE / "experiments" / spec["name"]).exists():
        raise NotAllowed(f"experiment {spec['name']!r} exists; name a new one")
    for key in ("hypothesis", "distinguishes"):
        if not isinstance(spec[key], str) or not spec[key].strip():
            raise NotAllowed(f"{key} must say what the experiment tests")
    settings = investigation()["experiment"]
    channels = _object(spec["channels"], "channels", set(),
                       set(settings["input_channels"]))
    for name, change in channels.items():
        _object(change, f"channels.{name}", set(), {"latency_ns", "interceptors"})
        latency = change.get("latency_ns", 0)
        if type(latency) is not int or latency < 0:
            raise NotAllowed(f"channels.{name}.latency_ns must be a non-negative integer")
        if not isinstance(change.get("interceptors", []), list):
            raise NotAllowed(f"channels.{name}.interceptors must be a list")
    references = spec["references"]
    if not isinstance(references, list) or not set(references) <= set(
            settings["prepared_references"]) or len(set(references)) != len(references):
        raise NotAllowed("references must name prepared references, each once: "
                         + ", ".join(settings["prepared_references"]))
    return spec


def parent_case(name: str) -> dict:
    cases = {case["name"]: case for case in investigation()["cases"]}
    case = cases.get(name)
    if case is None or case["bundle"] is None:
        raise NotAllowed("parent must be a case with a bundle: "
                         + ", ".join(n for n, c in cases.items() if c["bundle"]))
    return case


def _rebased(value, old: str, new: str):
    """`value` with every string that names a path under `old` moved to `new`."""
    if isinstance(value, str):
        return new + value[len(old):] if value == old or value.startswith(old + "/") else value
    if isinstance(value, list):
        return [_rebased(v, old, new) for v in value]
    if isinstance(value, dict):
        return {k: _rebased(v, old, new) for k, v in value.items()}
    return value


def follow_up_manifest(parent: dict, parent_root: Path, root: Path,
                       channels: dict) -> dict:
    doc = _rebased(parent, str(parent_root), str(root))
    for name, change in channels.items():
        channel = doc["channels"][name]
        if "latency_ns" in change:
            channel["latency_ns"] = change["latency_ns"]
        if "interceptors" in change:
            channel.pop("interceptors", None)
            if change["interceptors"]:
                channel["interceptors"] = change["interceptors"]
    return doc


def intervention(parent: dict, follow_up: dict) -> dict:
    """What the follow-up Manifest changes on each input Channel."""
    changed = {}
    for name, channel in follow_up["channels"].items():
        before = parent["channels"][name]
        for key in ("latency_ns", "interceptors"):
            if before.get(key) != channel.get(key):
                changed.setdefault(name, {})[key] = {
                    "parent": before.get(key), "follow_up": channel.get(key)}
    return changed


def canonical(doc: dict) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"),
                      allow_nan=False) + "\n"


def command(argv: list[str]) -> dict:
    """Runs one installed `sil` command, `argv` without the program."""
    started = time.monotonic()
    sil = Path(investigation()["runtime"]["python_bin"]) / "sil"
    proc = subprocess.run([str(sil), *argv], cwd=WORKSPACE, env=environment(),
                          capture_output=True, text=True)
    return {"argv": ["sil", *(str(Path(a).relative_to(WORKSPACE))
                              if a.startswith(str(WORKSPACE)) else a for a in argv)],
            "exit_code": proc.returncode, "stdout": proc.stdout,
            "stderr": proc.stderr,
            "elapsed_s": round(time.monotonic() - started, 3)}


def runs_of(summary: dict) -> int:
    """The Runs a bundle summary executed: a failed first Run is not repeated."""
    return sum(1 + (run["determinism"] is not None) for run in summary["runs"])


def experiment(spec_path: Path) -> int:
    spec_path = (WORKSPACE / spec_path).resolve() if not spec_path.is_absolute() else spec_path
    if not spec_path.is_relative_to(WORKSPACE):
        raise NotAllowed(f"the spec {spec_path} is outside the workspace")
    spec = read_spec(spec_path)
    case = parent_case(spec["parent"])
    check_budget(runs_needed=2)
    parent_root = WORKSPACE / case["bundle"]
    verified = command(["bundle", "verify", str(parent_root),
                        "--expect-lock", case["expect_lock"]])
    if verified["exit_code"] != 0:
        raise NotAllowed(f"the parent bundle does not verify: {verified['stderr']}")
    declaration = json.loads((parent_root / "bundle.json").read_text())
    (parent_run,) = declaration["runs"]
    parent_doc = json.loads((parent_root / parent_run["manifest"]).read_text())

    home = WORKSPACE / "experiments" / spec["name"]
    root = home / "bundle"
    root.mkdir(parents=True)
    manifest_name = f"{spec['name']}.json"
    artifacts = {name: role for name, role in declaration["artifacts"].items()
                 if role != "manifest"}
    for name in artifacts:
        shutil.copyfile(parent_root / name, root / name)
    doc = follow_up_manifest(parent_doc, parent_root, root, spec["channels"])
    (root / manifest_name).write_text(canonical(doc))
    artifacts[manifest_name] = "manifest"
    contract = investigation()["experiment"]["contract"]
    run = {"name": spec["name"], "manifest": manifest_name,
           "participant_timeout_ms": parent_run["participant_timeout_ms"],
           "determinism": True,
           "comparisons": [{"name": ref.removesuffix(".expected.mcap"),
                            "contract": contract, "reference": ref}
                           for ref in spec["references"]]}
    (root / "bundle.json").write_text(json.dumps(
        {**declaration, "name": spec["name"], "artifacts": artifacts,
         "runs": [run]}, indent=2) + "\n")
    shutil.copyfile(spec_path, home / "spec.json")

    commands = [command(["manifest", "validate", str(root / manifest_name),
                         "--json"])]
    if commands[0]["exit_code"] != 0:
        shutil.rmtree(home)
        raise NotAllowed(f"the follow-up Manifest is not valid:\n{commands[0]['stdout']}")
    commands.append(command(["bundle", "seal", str(root)]))
    if commands[-1]["exit_code"] != 0:
        shutil.rmtree(home)
        raise NotAllowed(f"the follow-up bundle does not seal:\n{commands[-1]['stderr']}")
    lock = commands[-1]["stdout"].rsplit("lock sha256 ", 1)[-1].strip()
    evidence = home / "evidence"
    commands.append(command(["bundle", "run", str(root), "-o", str(evidence),
                             "--expect-lock", lock]))
    try:
        summary = json.loads((evidence / "summary.json").read_text())
    except (OSError, ValueError) as error:
        # No summary tells how many Runs started, so count the most.
        append({"kind": "experiment", "name": spec["name"], "parent": case["name"],
                "exit_code": commands[-1]["exit_code"], "runs": 2,
                "error": f"no bundle summary: {error}"})
        sys.stderr.write(commands[-1]["stdout"] + commands[-1]["stderr"])
        return commands[-1]["exit_code"] or 1
    record = {
        "sil_investigation_experiment_record": RECORD_FORMAT,
        "name": spec["name"],
        "spec": {"path": f"experiments/{spec['name']}/spec.json",
                 "sha256": file_sha256(home / "spec.json")},
        "hypothesis": spec["hypothesis"],
        "distinguishes": spec["distinguishes"],
        "parent": {"case": case["name"], "bundle": case["bundle"],
                   "lock_sha256": case["expect_lock"],
                   "manifest_sha256": file_sha256(parent_root / parent_run["manifest"])},
        "intervention": intervention(parent_doc, doc),
        "bundle": f"experiments/{spec['name']}/bundle",
        "lock_sha256": lock,
        "manifest_sha256": file_sha256(root / manifest_name),
        "contract": {"name": contract, "sha256": file_sha256(root / contract)},
        "references": {ref: file_sha256(root / ref) for ref in spec["references"]},
        "commands": [{k: v for k, v in c.items() if k not in ("stdout", "stderr")}
                     for c in commands],
        "evidence": f"experiments/{spec['name']}/evidence",
        "summary_sha256": file_sha256(evidence / "summary.json"),
        "result": summary["runs"],
        "first_divergences": {
            name: json.loads((evidence / "runs" / spec["name"]
                              / f"{name}.comparison.json").read_text()
                             ).get("first_divergence")
            for name in summary["runs"][0]["comparisons"]},
    }
    (home / "record.json").write_text(json.dumps(record, indent=2) + "\n")
    append({"kind": "experiment", "name": spec["name"], "parent": case["name"],
            "exit_code": commands[-1]["exit_code"], "runs": runs_of(summary),
            "elapsed_s": round(sum(c["elapsed_s"] for c in commands), 3)})
    sys.stdout.write(commands[-1]["stdout"] + commands[-1]["stderr"])
    sys.stdout.write(f"record: experiments/{spec['name']}/record.json\n")
    return commands[-1]["exit_code"]


def status() -> int:
    print(json.dumps({"used": usage(ledger()),
                      "budgets": investigation()["budgets"]}, indent=2))
    return 0


def main(argv: list[str]) -> int:
    try:
        if argv[:2] == ["tool", "--"]:
            return tool(argv[2:])
        if len(argv) == 2 and argv[0] == "experiment":
            return experiment(Path(argv[1]))
        if argv == ["status"]:
            return status()
        raise NotAllowed("usage: investigate.py tool -- COMMAND [ARG]... | "
                         "experiment SPEC | status")
    except NotAllowed as refusal:
        append({"kind": "refusal", "argv": argv, "reason": str(refusal)})
        sys.stderr.write(f"investigate: not allowed: {refusal}\n")
        return EXIT_NOT_ALLOWED
    except Exhausted as exhausted:
        append({"kind": "refusal", "argv": argv, "reason": str(exhausted)})
        sys.stderr.write(f"investigate: {exhausted}. Write the reports now; mark "
                         "each unfinished case `incomplete`.\n")
        return EXIT_BUDGET


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
