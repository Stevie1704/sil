"""Prepare the agent investigation proof of issue #283.

Preparation needs the source checkout and the built controller library. It
writes two directories into OUT:

- `agent/`: the investigator's workspace. It holds the sealed regression
  bundles, the evidence of the failed regression matrix, `investigation.json`
  (the cases, the allowed actions and the budgets), `brief.md` and the proof
  driver `tools/investigate.py`.
- `evaluator/`: what the investigator does not get. It holds the case list,
  the private bundle of the `late-private` case and the digest of every
  workspace file, so `verify.py` can show that the original artifacts did not
  change.

The four cases replay the `cadence` maneuver of the ADAS reference example
(`examples/adas-reference`) into one controller build, under the comparison
contract and the independently enumerated references of that example:

| Case | Run | Expected matrix status |
| --- | --- | --- |
| `delay-regression` | radar delay experiment | `behavioral-failure` (comparison) |
| `nominal-control` | no fault | `pass` |
| `monitor-timeout` | no fault, with a command monitor that stalls | `behavioral-failure` (Run exit 1, a missed response deadline) |
| `late-private` | 10 ms input Latency, a private bundle | `behavioral-failure`; only its summary is shared |

    python proofs/agent-investigation/prepare.py OUT \\
        --python-bin VENV/bin --sil-bin PREFIX/bin \\
        --target build/adas_reference_arrival_time.so
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import NamedTuple

from sil.manifest import SubscriberRoute

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
EXAMPLE = ROOT / "examples" / "adas-reference"
MANEUVER = "cadence"
INPUT_CHANNELS = [f"{MANEUVER}.{role}" for role in ("radar", "camera", "ego")]
CONTRACT = f"{MANEUVER}.contract.json"
# A Process participant that does not answer within this deadline fails the
# Run. The Native controller is not under it.
PARTICIPANT_TIMEOUT_MS = 2_000
CASE_TIMEOUT_S = 120
MS = 1_000_000

BUDGETS = {
    # Each follow-up experiment runs its Manifest twice (determinism).
    "runs": 16,
    "elapsed_s": 3_600,
    "tool_calls": 200,
    "tool_output_bytes": 2_000_000,
    # The driver cannot count inference. The evaluator compares the
    # harness's token total of the session with it after the session.
    "inference_tokens": 400_000,
}
TOOL_OUTPUT_LIMIT = 65_536


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bundles = _load("bundle_prepare", ROOT / "examples" / "bundle" / "prepare.py")
example_prepare = _load("adas_prepare", EXAMPLE / "prepare.py")
example_manifest = _load("adas_manifest", EXAMPLE / "manifest.py")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def prepared_references() -> dict[str, dict]:
    """Each prepared reference of the maneuver, with the input-Channel settings
    of the experiment it was enumerated for."""
    references = {f"{MANEUVER}.expected.mcap": {
        "experiment": "none", "channels": {}}}
    for name, experiment in example_manifest.EXPERIMENTS.items():
        channels = {}
        for role, interceptors in experiment.get("interceptors", {}).items():
            channels[f"{MANEUVER}.{role}"] = {"interceptors": interceptors}
        if "input_latency_ns" in experiment:
            for channel in INPUT_CHANNELS:
                channels.setdefault(channel, {})["latency_ns"] = \
                    experiment["input_latency_ns"]
        references[f"{MANEUVER}.{name}.expected.mcap"] = {
            "experiment": name, "channels": channels}
    return references


class Case(NamedTuple):
    """One regression case: its bundle and how its Manifest differs."""

    name: str
    experiment: str | None
    reference: str
    monitor: bool = False


CASES = (
    Case("delay-regression", "delay", f"{MANEUVER}.delay.expected.mcap"),
    Case("nominal-control", None, f"{MANEUVER}.expected.mcap"),
    Case("monitor-timeout", None, f"{MANEUVER}.expected.mcap", monitor=True),
    Case("late-private", "late", f"{MANEUVER}.late.expected.mcap"),
)
PRIVATE = "late-private"


def build_bundle(root: Path, case: Case, runtime, target: Path,
                 maneuvers: Path) -> None:
    """Write the bundle of `case` into `root`. `maneuvers` is where the
    example's preparation wrote the Recordings and the contract."""
    bundle = bundles.Bundle(root)
    library = bundle.copy(target, "target", "adas_reference.so")
    for suffix, role in ((".inputs.csv", "conversion-input"),
                         (".inputs.mapping.json", "conversion-input"),
                         (".inputs.mcap", "recording"),
                         (".inputs.receipt.json", "receipt")):
        bundle.copy(maneuvers / f"{MANEUVER}{suffix}", role)
    bundle.copy(maneuvers / CONTRACT, "contract")
    for reference in prepared_references():
        stem = reference.removesuffix(".mcap")
        bundle.copy(maneuvers / reference, "reference")
        bundle.copy(maneuvers / f"{stem}.mapping.json", "conversion-input")
        bundle.copy(maneuvers / f"{stem}.receipt.json", "receipt")
        bundle.copy(EXAMPLE / "maneuvers" / f"{stem}.csv", "conversion-input")
    m = (example_manifest.experiment_manifest(bundle.root, library, case.experiment)
         if case.experiment else
         example_manifest.reference_manifest(bundle.root, library,
                                             maneuvers=(MANEUVER,)))
    if case.monitor:
        monitor = bundle.copy(HERE / "monitor.py", "participant")
        m.add_process("monitor", command=["python3", str(monitor)],
                      step_period_ns=example_manifest.PERIOD_NS,
                      subscribes=[SubscriberRoute(f"{MANEUVER}.command", capacity=3)],
                      publishes=[])
    m.write(bundle.path(f"{case.name}.json", "manifest"))
    runs = [{"name": case.name, "manifest": f"{case.name}.json",
             "participant_timeout_ms": PARTICIPANT_TIMEOUT_MS,
             "comparisons": [{"name": case.reference.removesuffix(".expected.mcap"),
                              "contract": CONTRACT,
                              "reference": case.reference}]}]
    bundle.declare(runtime, case.name, runs,
                   files=tuple(bundles.native_libraries(target)))


def sil(runtime, *args, check: bool = True) -> subprocess.CompletedProcess:
    """The installed `sil` command, with no source tree on any path."""
    env = {"PATH": f"{runtime.python_bin}:{runtime.sil_bin}:/usr/bin:/bin",
           "PYTHONNOUSERSITE": "1"}
    proc = subprocess.run([str(runtime.python_bin / "sil"), *map(str, args)],
                          env=env, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise SystemExit(f"sil {' '.join(map(str, args))} exited "
                         f"{proc.returncode}:\n{proc.stdout}{proc.stderr}")
    return proc


def seal(runtime, root: Path) -> str:
    out = sil(runtime, "bundle", "seal", root).stdout
    return out.rsplit("lock sha256 ", 1)[1].strip()


def share_only_the_summary(ci: Path) -> None:
    """A supplier keeps its bundle and evidence private and shares only the
    bundle summary (docs/regression-bundles.md)."""
    case = ci / "cases" / PRIVATE
    for path in sorted(case.iterdir()):
        if path.name != "summary.json":
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    (ci / "logs" / f"{PRIVATE}.log").unlink()


def digests(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): file_sha256(p)
            for p in sorted(root.rglob("*")) if p.is_file()}


def prepare(out: Path, runtime, target: Path) -> dict:
    """Write `agent/` and `evaluator/` into `out` and run the matrix. Returns
    the matrix summary."""
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} is not empty; name a new directory")
    out = out.resolve()
    agent, evaluator = out / "agent", out / "evaluator"
    with tempfile.TemporaryDirectory() as work:
        maneuvers = Path(work)
        example_prepare.prepare(maneuvers)
        locks = {}
        for case in CASES:
            home = evaluator if case.name == PRIVATE else agent
            root = home / "bundles" / case.name
            build_bundle(root, case, runtime, target, maneuvers)
            locks[case.name] = seal(runtime, root)
    cases = [{"name": case.name,
              "bundle": str((evaluator if case.name == PRIVATE else agent)
                            / "bundles" / case.name),
              "expect_lock": locks[case.name], "timeout_s": CASE_TIMEOUT_S}
             for case in CASES]
    _write_json(evaluator / "cases.json", {"sil_matrix": 1, "cases": cases})
    ci = agent / "ci"
    matrix = sil(runtime, "bundle", "matrix", evaluator / "cases.json", "-o", ci,
                 check=False)
    if matrix.returncode != 1:
        raise SystemExit(f"the regression matrix must fail (exit 1), got "
                         f"{matrix.returncode}:\n{matrix.stdout}{matrix.stderr}")
    share_only_the_summary(ci)
    write_workspace(agent, runtime, locks)
    _write_json(evaluator / "digests.json", digests(agent))
    return json.loads((ci / "summary.json").read_text())


def write_workspace(agent: Path, runtime, locks: dict[str, str]) -> None:
    investigation = {
        "sil_investigation": 1,
        "matrix": "ci/summary.json",
        "cases": [{"name": case.name,
                   "bundle": None if case.name == PRIVATE else f"bundles/{case.name}",
                   "expect_lock": locks[case.name],
                   "evidence": f"ci/cases/{case.name}"}
                  for case in CASES],
        "experiment": {
            "input_channels": INPUT_CHANNELS,
            "contract": CONTRACT,
            "prepared_references": prepared_references(),
        },
        "budgets": BUDGETS,
        "tool_output_limit": TOOL_OUTPUT_LIMIT,
        "runtime": {"python_bin": str(runtime.python_bin),
                    "sil_bin": str(runtime.sil_bin)},
    }
    _write_json(agent / "investigation.json", investigation)
    shutil.copyfile(HERE / "brief.md", agent / "brief.md")
    (agent / "tools").mkdir()
    shutil.copyfile(HERE / "investigate.py", agent / "tools" / "investigate.py")
    for name in ("specs", "experiments", "reports"):
        (agent / name).mkdir()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path, help="a new directory")
    parser.add_argument("--python-bin", type=Path, required=True,
                        help="the runtime directory holding python3 with SiL installed")
    parser.add_argument("--sil-bin", type=Path, required=True,
                        help="the runtime directory holding sil-run")
    parser.add_argument("--target", type=Path, required=True,
                        help="the controller build under investigation")
    args = parser.parse_args()
    runtime = bundles.Runtime(args.python_bin.resolve(), args.sil_bin.resolve())
    summary = prepare(args.out, runtime, args.target.resolve())
    for case in summary["cases"]:
        print(f"{case['name']}: {case['status']}")


if __name__ == "__main__":
    main()
