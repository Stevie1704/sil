"""Seal once, or run and check the installed OSMP qualification offline (issue #233).

`run.py --seal` runs while the runtime image is built: it seals each bundle
and writes the two case lists with the lock digests that `seal` returned.
`run.py [evidence]` runs in the container with no network, as UID 10001:

1. The nominal matrix must exit 0, and the controls matrix must exit 1.
2. Each case must reach the status, Run exit codes, diagnostics and
   comparison verdicts that preparation decided before any Run. The late
   control must first diverge where the references predict.
3. No process may survive a case, and no Run working directory may remain.
4. The cost of the two FMUs at their 20 ms Period is measured for #125:
   wall-clock only, never a verdict.

`acceptance.json` holds every check.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path

BUNDLES = Path("/bundles")
OPT = Path("/opt/osmp")
EXPECTED = OPT / "expected.json"
COST = OPT / "cost"
COST_STEPS = {"startup": 1, "long": 1500}
PERIOD_NS = 20_000_000
WARMUP, REPEATS = 1, 5


def write(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def kind_of(name: str) -> str:
    return "controls" if name.startswith("control-") else "nominal"


def seal() -> None:
    cases = {"nominal": [], "controls": []}
    for name, expected in json.loads(EXPECTED.read_text()).items():
        root = BUNDLES / name
        sealed = subprocess.run(["sil-bundle", "seal", str(root)],
                                capture_output=True, text=True, check=True)
        print(sealed.stdout, end="")
        lock = hashlib.sha256((root / "bundle.lock.json").read_bytes()).hexdigest()
        cases[kind_of(name)].append({"name": name, "bundle": str(root),
                                     "expect_lock": lock,
                                     "timeout_s": expected["timeout_s"]})
    for kind, entries in cases.items():
        write(OPT / f"{kind}.json", {"sil_matrix": 1, "cases": entries})


def _parent(pid: int) -> int:
    # The command name in field 2 may contain spaces; it ends at the last ')'.
    stat = Path(f"/proc/{pid}/stat").read_text()
    return int(stat[stat.rindex(")") + 2:].split()[1])


def leftover_processes() -> list[int]:
    """Live processes other than this driver and its ancestors; Linux only."""
    own, pid = set(), os.getpid()
    while pid > 0:
        own.add(pid)
        pid = _parent(pid)
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit() and int(entry.name) not in own:
            try:
                _parent(int(entry.name))
            except (FileNotFoundError, ProcessLookupError):
                continue  # exited while listing
            found.append(int(entry.name))
    return found


def check_case(name: str, entry: dict, expected: dict, evidence: Path) -> dict:
    """Every outcome preparation decided for one case."""
    runs = entry.get("runs", [])
    check = {"status": entry.get("status"), "expected": expected["status"],
             "passed": entry.get("status") == expected["status"]}
    if "run_exit_codes" in expected:
        check["run_exit_codes"] = [r.get("exit_code") for r in runs]
        check["passed"] &= check["run_exit_codes"] == expected["run_exit_codes"]
    logs = "\n".join(p.read_text() for p in sorted(evidence.rglob("run-1.log")))
    if "diagnostic" in expected:
        check["diagnostic"] = [line for line in logs.splitlines()
                               if any(d in line for d in expected["diagnostic"])]
        check["passed"] &= all(d in logs for d in expected["diagnostic"])
    if "comparisons" in expected:
        reports = {p.name.removesuffix(".comparison.json"): json.loads(p.read_text())
                   for p in evidence.rglob("*.comparison.json")}
        check["comparisons"] = {n: r.get("verdict") for n, r in reports.items()}
        check["passed"] &= check["comparisons"] == expected["comparisons"]
        first = reports.get("independent", {}).get("first_divergence") or {}
        check["first_divergence"] = first
        check["passed"] &= all(first.get(key) == value
                               for key, value in expected["first_divergence"].items())
    check["leftover_processes"] = leftover_processes()
    check["leftover_files"] = sorted(str(p) for p in evidence.rglob(".sil-run-*"))
    check["passed"] &= not check["leftover_processes"] and not check["leftover_files"]
    return check


def run_matrices(out: Path) -> dict:
    expected = json.loads(EXPECTED.read_text())
    checks = {}
    for kind, exit_code in (("nominal", 0), ("controls", 1)):
        matrix = subprocess.run(["sil-matrix", str(OPT / f"{kind}.json"), "-o",
                                 str(out / kind), "--jobs", "1"],
                                capture_output=True, text=True)
        (out / f"{kind}.log").write_text(matrix.stdout + matrix.stderr)
        summary = out / kind / "summary.json"
        # A matrix that wrote no summary fails its case-set check below.
        result = json.loads(summary.read_text()) if summary.is_file() else {"cases": []}
        entries = {c["name"]: c for c in result["cases"]}
        names = {n for n in expected if kind_of(n) == kind}
        cases = {name: check_case(name, entries.get(name, {}), expected[name],
                                  out / kind / "cases" / name)
                 for name in sorted(names)}
        checks[kind] = {"exit_code": matrix.returncode, "cases": cases,
                        "passed": matrix.returncode == exit_code and set(entries) == names
                        and all(c["passed"] for c in cases.values())}
    return checks


def _timed(manifest: Path, directory: Path) -> float:
    directory.mkdir(parents=True)
    started = time.perf_counter()
    subprocess.run(["sil-run", str(manifest), "--no-recording"], cwd=directory,
                   check=True, capture_output=True, text=True)
    elapsed = time.perf_counter() - started
    leftovers = list(directory.iterdir())
    if leftovers:
        raise RuntimeError(f"a cost Run left {leftovers}")
    return elapsed


def _spread(samples: list[float]) -> float:
    return max(samples) - min(samples)


def cost(work: Path) -> dict:
    """Wall-clock of the source and one sensor, each in its own Importer.

    `startup` is one Step: two interpreters, two Importers, archive
    extraction, `modelDescription.xml`, `dlopen`, instantiation and
    initialization, then one Step. `long` is the whole 30 s experiment. The
    difference over the extra Steps estimates one Step of both FMUs with
    their Step protocol round trips. Observational, not a capacity claim.
    """
    samples = {}
    for name in COST_STEPS:
        manifest = COST / f"{name}.json"
        for index in range(WARMUP):
            _timed(manifest, work / name / f"warmup-{index}")
        samples[name] = [_timed(manifest, work / name / f"run-{index}")
                         for index in range(REPEATS)]
    median = {name: statistics.median(values) for name, values in samples.items()}
    extra_steps = COST_STEPS["long"] - COST_STEPS["startup"]
    per_step_us = (median["long"] - median["startup"]) / extra_steps * 1e6
    spread_us = (_spread(samples["long"]) + _spread(samples["startup"])) / extra_steps * 1e6
    return {
        "policy": {"warmup": WARMUP, "repeats": REPEATS, "statistic": "median",
                   "recording": "off"},
        "machine": {"system": platform.system(), "machine": platform.machine(),
                    "cpus": os.cpu_count(), "python": platform.python_version()},
        "steps": COST_STEPS, "period_ns": PERIOD_NS,
        "wall_clock_s": {name: {"median": round(median[name], 4),
                                "min": round(min(values), 4), "max": round(max(values), 4)}
                         for name, values in samples.items()},
        "startup_ms": round(median["startup"] * 1e3, 1),
        "per_step_both_fmus_us": {"estimate": round(per_step_us, 1),
                                  "spread": round(spread_us, 1),
                                  "resolved": abs(per_step_us) > spread_us},
        "real_time_factor_long": round(COST_STEPS["long"] * PERIOD_NS / 1e9
                                       / median["long"], 1),
    }


def run(out: Path) -> bool:
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise SystemExit("name a new, empty evidence directory")
    checks = run_matrices(out)
    passed = all(c["passed"] for c in checks.values())
    result = {"passed": passed, "matrices": checks,
              # Measured only after the verdicts, so it cannot change one.
              "cost": cost(out.parent / "cost-runs") if passed else None}
    write(out / "acceptance.json", result)
    print(json.dumps(result, indent=2))
    return passed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seal", action="store_true")
    parser.add_argument("out", type=Path, nargs="?", default=Path("/work/evidence"))
    args = parser.parse_args()
    if args.seal:
        seal()
    else:
        sys.exit(0 if run(args.out.resolve()) else 1)
