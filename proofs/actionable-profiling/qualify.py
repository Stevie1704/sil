#!/usr/bin/env python3
"""Retain the integrated #354 workflow on real Linux perf/Heaptrack.

Run with an installed SiL and native runners on PATH. No fake backend or
permission change is made here. Each command has a finite timeout; sizes,
elapsed time and process peak RSS are observations, never timing gates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path

from sil.manifest import Manifest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(out: Path, label: str, argv: list[str], observations: list) -> None:
    begin = time.monotonic_ns()
    resources = out / f"{label}.resources"
    with (out / f"{label}.stdout").open("wb") as stdout, \
            (out / f"{label}.stderr").open("wb") as stderr:
        process = subprocess.Popen(
            ["/usr/bin/time", "-v", "-o", str(resources), *argv],
            stdout=stdout, stderr=stderr, start_new_session=True)
        try:
            code = process.wait(timeout=240)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
    peak = re.search(r"Maximum resident set size \(kbytes\): (\d+)",
                     resources.read_text())
    if peak is None:
        raise RuntimeError("GNU time did not retain a per-command peak RSS")
    observations.append({"label": label, "argv": argv,
                         "exit_code": code,
                         "elapsed_ns": time.monotonic_ns() - begin,
                         "peak_rss_kib": int(peak.group(1))})
    if code:
        raise RuntimeError(f"{label}: exit {code}; see {out}")


def minimal(source: Path, target: Path) -> Path:
    """Copy only the original portable contract, never optional evidence."""
    target.mkdir()
    for relative in ("profile.json", "cpu/samples.json",
                     "activation/correlation.json", "heap/allocations.json"):
        path = source / relative
        if path.is_file():
            (target / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target / relative)
    return target / "profile.json"


def qualify(out: Path, include: Path) -> dict:
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise RuntimeError("real capture qualification requires Linux x86-64/glibc")
    out.mkdir(parents=True, exist_ok=False)
    observations: list[dict] = []
    cli = [sys.executable, "-m", "sil.cli"]
    manifests = {}
    for variant, work in (("base", 40_000_000), ("new", 20_000_000)):
        library = out / f"{variant}.so"
        run(out, f"build-{variant}", ["cc", "-O2", "-g", "-fPIC", "-shared",
            "-fno-omit-frame-pointer", f"-DWORK={work}", "-I", str(include),
            str(HERE / "late_outlier.c"), "-o", str(library)], observations)
        manifest = Manifest(duration_ns=620_000_000)
        manifest.add_schemas({"Event": {"fields": [
            {"name": "task", "type": "u64"},
            {"name": "index", "type": "u64"},
            {"name": "publication", "type": "u64"}]}})
        manifest.add_channel("events", schema="Event")
        manifest.add_native("late", library=str(library), publishes=["events"])
        path = out / f"{variant}.json"
        manifest.write(path)
        manifests[variant] = path

    ordinary = out / "ordinary.mcap"
    run(out, "ordinary", [*cli, "run", str(manifests["base"]), "-o",
                           str(ordinary)], observations)
    selections = []
    for variant, policy in (("base", "full"), ("new", "external")):
        manifest = manifests[variant]
        directory = out / variant
        run(out, f"capture-{variant}", [*cli, "profile", str(manifest), "--out",
            str(directory), "--collector", "cpu", "--activations",
            "--max-activation-bytes", "33554432", "--provenance-mode", policy],
            observations)
        profile = json.loads((directory / "profile.json").read_text())
        correlation = json.loads((directory / "activation/correlation.json").read_text())
        samples = json.loads((directory / "cpu/samples.json").read_text())
        rows = correlation["activations"]
        assert len(rows) == 32240
        found = [(i, row) for i, row in enumerate(rows)
                 if row["participant"] == "late" and row["task"] == "task51"
                 and row["index"] == 610]
        assert len(found) == 1
        ref, selected = found[0]
        assert ref > 500 and selected["virtual_time_ns"] == 610_000_000
        assert selected["outcome"] == "completed"
        matching = [row for row, classification in
                    zip(samples["samples"], correlation["samples"])
                    if classification == ["execution_context", ref]]
        assert len(matching) == selected["samples"]
        assert sum(row[3] for row in matching) == selected["weight_ns"]
        # Sampling can miss arbitrarily short work; absence is evidence too.
        selections.append({"variant": variant, "activation": selected,
                           "execution_context_samples": len(matching),
                           "sampled_source_locations": [
                               samples["frames"][samples["stacks"][row[4]][0]].get("source")
                               for row in matching if samples["stacks"][row[4]]],
                           "profile_identity": profile["identity"]})
        assert digest(directory / "run.mcap") == digest(ordinary)
        run(out, f"render-{variant}", [*cli, "profile", "render",
            str(directory / "profile.json"), "--participant", "late",
            "--task", "task51", "--activation-index", "610", "--diagnostics",
            str(directory / "run.diagnostics.json"), "--html",
            str(out / f"{variant}-selected.html")], observations)
        copied = minimal(directory, out / f"{variant}-minimal")
        run(out, f"minimal-{variant}", [*cli, "profile", "render", str(copied),
            "--html", str(out / f"{variant}-minimal.html")], observations)
        run(out, f"benchmark-{variant}", [*cli, "benchmark", str(manifest),
            "--out", str(out / f"{variant}-benchmark"), "--warmup", "1",
            "--repeats", "3", "--provenance-mode", policy], observations)
        run(out, f"query-{variant}", [*cli, "recording", "query",
            str(directory / "run.mcap"), "--channel", "events", "--field", "task",
            "--field", "index", "--field", "publication", "--from", "610000000",
            "--to", "611000000", "--max-results", "104", "--max-messages", "104",
            "--max-bytes", "65536"], observations)
        receipt = json.loads((out / f"query-{variant}.stdout").read_text())
        assert receipt["source"]["sha256"] == digest(directory / "run.mcap")
        assert receipt["scan"]["complete"] and receipt["output"]["complete"]
        assert len(receipt["results"]) == 104
        assert [row["fields"] for row in receipt["results"][-2:]] == [
            {"task": 51, "index": 610, "publication": 0},
            {"task": 51, "index": 610, "publication": 1}]
    run(out, "diff", [*cli, "profile", "diff", str(out / "base/profile.json"),
        str(out / "new/profile.json"), "--base-benchmark",
        str(out / "base-benchmark/summary.json"), "--new-benchmark",
        str(out / "new-benchmark/summary.json"), "--html", str(out / "diff.html")],
        observations)
    run(out, "heap", [*cli, "profile", str(manifests["base"]), "--out",
        str(out / "heap"), "--collector", "heap", "--runner"], observations)
    heap = json.loads((out / "heap/profile.json").read_text())
    site = [row for row in heap["capture"]["analysis"]["hotspots"]["self"]
            if row["symbol"] and "late_allocations" in row["symbol"]]
    assert len(site) == 1 and site[0]["allocations"] == 8
    assert site[0]["allocated_bytes"] == 8192
    copied = minimal(out / "heap", out / "heap-minimal")
    run(out, "heap-render", [*cli, "profile", "render", str(copied), "--html",
        str(out / "heap.html")], observations)
    assert digest(out / "heap/run.mcap") == digest(ordinary)
    return {"spec": 354, "ticket": 359, "host": platform.uname()._asdict(),
            "libc": platform.libc_ver(), "python": sys.version,
            "source_revision": os.environ.get("SIL_SOURCE_REVISION"),
            "expected": {"tasks": 52, "activations_per_task": 620,
                         "selected_task": "task51", "selected_index": 610,
                         "virtual_time_ns": 610_000_000,
                         "publications_in_query": 104,
                         "site_allocations": 8, "site_allocated_bytes": 8192},
            "selected": selections, "observations": observations,
            "rss_scope": "GNU time per-command maximum resident set size in KiB, including child execution",
            "limits": "Timings and sampled weights are observations, not significance or correctness thresholds."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    parser.add_argument("--include", type=Path, default=ROOT / "include")
    args = parser.parse_args()
    out = args.out.resolve()
    result = qualify(out, args.include.resolve())
    (out / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    files = [{"path": str(path.relative_to(out)), "bytes": path.stat().st_size,
              "sha256": digest(path)} for path in sorted(out.rglob("*")) if path.is_file()]
    (out / "digests.json").write_text(json.dumps(files, indent=2) + "\n")


if __name__ == "__main__":
    main()
