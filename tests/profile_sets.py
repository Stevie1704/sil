"""Portable profile artifact sets for the `sil profile render` tests (#318).

A set is a directory with `profile.json` and the normalized files it names:
`cpu/samples.json`, `activation/correlation.json` or `heap/allocations.json`
(docs/profile-report.md, "Offline report"). `artifact_set` copies a frozen
`profile.json` from tests/fixtures/profile/ and writes sidecars whose counts
and sums agree with it, so a test needs only the fixture name. The sidecar
values are synthetic: the stacks give each self hotspot row of the fixture
its own leaf frame under one root, and the Activation rows come from the
summary's Task counts.

`edit` changes a sidecar or the report afterwards and writes the digests of
`artifacts` again, as `sil profile` would have recorded them.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from conftest import ROOT

FIXTURES = ROOT / "tests" / "fixtures" / "profile"
SAMPLES = "cpu/samples.json"
CORRELATION = "activation/correlation.json"
ALLOCATIONS = "heap/allocations.json"
VIRTUAL_STEP_NS = 10_000_000


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _split(total: int, parts: int) -> list[int]:
    if parts == 0:
        return []
    share, rest = divmod(total, parts)
    return [share + (1 if index < rest else 0) for index in range(parts)]


def samples_document(report: dict) -> dict:
    """Samples with the fixture's totals: one leaf per self hotspot row."""
    capture = report["capture"]
    threads = [t for t in capture["threads"] if t["samples"]] or \
        capture["threads"]
    frames = [{"module": None, "symbol": "main", "address": "0x1000",
               "module_offset": None, "source": None}]
    stacks: list[list[int]] = []
    rows: list[list] = []
    groups = [(row["module"], row["symbol"], row["self_samples"],
               row["self_weight_ns"]) for row in capture["hotspots"]["self"]]
    remainder = capture["hotspots"]["self_remainder"]
    groups.append((None, None, remainder["samples"], remainder["weight_ns"]))
    for module, symbol, count, weight in groups:
        if count == 0:
            continue
        frames.append({"module": module, "symbol": symbol,
                       "address": f"0x{0x2000 + len(frames):x}",
                       "module_offset": None, "source": None})
        stacks.append([len(frames) - 1, 0])
        for part in _split(weight, count):
            thread = threads[len(rows) % len(threads)]
            rows.append([len(rows) * 1000, thread["process"], thread["id"],
                         part, len(stacks) - 1])
    return {"sil_profile_cpu_samples": 1,
            "time_origin_ns": capture["time_origin_ns"],
            "frames": frames, "stacks": stacks or [[]], "samples": rows}


def correlation_document(report: dict, samples: dict) -> dict:
    """Activation rows and sample classes that match the summary."""
    summary = report["activation_correlation"]["summary"]
    origin = int(samples["time_origin_ns"] or 0)
    activations, classes = [], []
    for task in summary["tasks"]:
        outcomes = (["completed"] * task["completed"] + ["failed"] *
                    task["failed"] + ["interrupted"] * task["interrupted"])
        sampled = _split(task["execution_context"]["samples"],
                         task["with_samples"])
        window = _split(task["window_associated"]["samples"],
                        task["with_samples"])
        for index, outcome in enumerate(outcomes):
            begin = origin + 1_000_000 * (len(activations) + 1)
            row = {"participant": task["participant"], "task": task["task"],
                   "kind": task["kind"], "index": index,
                   "virtual_time_ns": index * VIRTUAL_STEP_NS,
                   "begin_ns": str(begin), "end_ns": str(begin + 500_000),
                   "outcome": outcome, "samples": 0, "weight_ns": 0}
            ref = len(activations)
            activations.append(row)
            if index < task["with_samples"]:
                classes += [["execution_context", ref]] * sampled[index]
                classes += [["window_associated", ref]] * window[index]
    for phase in summary["lifecycle"]:
        classes += [["lifecycle", phase["phase"]]] * phase["samples"]
    classes += [["replay", 0]] * summary["samples"]["replay"]["samples"]
    for reason in summary["samples"]["unattributed"]["reasons"]:
        classes += [["unattributed", reason["reason"]]] * reason["samples"]
    for (kind, ref), row in zip(classes, samples["samples"]):
        if kind in ("execution_context", "window_associated"):
            activations[ref]["samples"] += 1
            activations[ref]["weight_ns"] += row[3]
    phases = [{"phase": p["phase"], "begin_ns": str(origin),
               "end_ns": str(origin + 1)} for p in summary["lifecycle"]]
    return {"sil_profile_activation_correlation": 1,
            "activations": activations, "replays": [], "phases": phases,
            "samples": classes}


def allocations_document(report: dict) -> dict:
    """Allocation infos with the fixture's totals: one per self row."""
    analysis = report["capture"]["analysis"]
    frames = [{"module": None, "symbol": "main", "address": "0x1000",
               "source": None}]
    stacks, rows = [], []
    groups = [dict(row) for row in analysis["hotspots"]["self"]]
    groups.append({"module": None, "symbol": None,
                   **analysis["hotspots"]["self_remainder"]})
    for group in groups:
        if not group["allocations"]:
            continue
        frames.append({"module": group["module"], "symbol": group["symbol"],
                       "address": f"0x{0x2000 + len(frames):x}",
                       "source": None})
        stacks.append([len(frames) - 1, 0])
        # One size per allocation info: a rest goes to a second info.
        count, size = group["allocations"], group["allocated_bytes"]
        share, rest = divmod(size, count)
        parts = [(share, count)] if not rest else \
            [(share, count - 1), (share + rest, 1)]
        for index, (each, calls) in enumerate(p for p in parts if p[1]):
            rows.append([len(stacks) - 1, each, calls,
                         group["temporary_allocations"] if index == 0 else 0,
                         group["outstanding_allocations"] if index == 0
                         else 0])
    return {"sil_profile_heap_allocations": 1, "frames": frames,
            "stacks": stacks, "allocations": rows}


def _write(directory: Path, relative: str, document: dict) -> None:
    path = directory / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, separators=(",", ":")) + "\n")


def _sidecars(report: dict) -> dict[str, dict]:
    capture = report["capture"]
    found: dict[str, dict] = {}
    if capture["kind"] == "cpu" and capture["samples_file"]:
        found[SAMPLES] = samples_document(report)
        correlation = report["activation_correlation"]
        if correlation.get("summary"):
            found[CORRELATION] = correlation_document(report, found[SAMPLES])
    elif capture["kind"] == "heap" and capture.get("analysis") and \
            capture["analysis"]["allocations_file"]:
        found[ALLOCATIONS] = allocations_document(report)
    return found


def seal(directory: Path) -> Path:
    """Write the size and digest of each listed artifact that exists."""
    path = directory / "profile.json"
    report = json.loads(path.read_text())
    for artifact in report["artifacts"]:
        file = directory / artifact["path"]
        if file.is_file():
            data = file.read_bytes()
            artifact["bytes"] = len(data)
            artifact["sha256"] = hashlib.sha256(data).hexdigest()
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return path


def artifact_set(directory: Path, name: str, report: dict | None = None,
                 **documents: dict) -> Path:
    """A portable set in `directory`; returns its `profile.json`.

    `documents` replaces a generated sidecar: `samples`, `correlation`,
    `allocations`."""
    directory.mkdir(parents=True, exist_ok=True)
    report = report if report is not None else fixture(name)
    sidecars = _sidecars(report)
    for key, relative in (("samples", SAMPLES), ("correlation", CORRELATION),
                          ("allocations", ALLOCATIONS)):
        if key in documents:
            sidecars[relative] = documents[key]
    for relative, document in sidecars.items():
        _write(directory, relative, document)
    (directory / "profile.json").write_text(json.dumps(report, indent=2))
    return seal(directory)


def copy_set(source: Path, target: Path, files: list[str]) -> Path:
    target.mkdir(parents=True)
    for relative in files:
        (target / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target / relative)
    return target / "profile.json"
