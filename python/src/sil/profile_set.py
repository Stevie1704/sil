"""The portable artifact set of a saved profile (#318).

`sil profile render` reads `profile.json` and the normalized files that it
names, and nothing else (docs/profile-report.md, "Offline report"):

- CPU: `cpu/samples.json`; with a correlation summary also
  `activation/correlation.json`;
- heap: `heap/allocations.json`.

`load_profile_set` reads and checks them. A named file must exist, have the
size and SHA-256 that `artifacts` records, have its version 1, refer only to
identities of `profile.json`, and add up to its totals. Otherwise the set is
not one this version can present, and ProfileSetError says why. It never
opens `perf.data`, `heaptrack.data`, a module or a source file.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from sil.diagnostics_shape import (
    COUNT, INTEGER, TEXT, Shape, items, maybe, members, shape_errors,
)
from sil.profile_activation import CLASSES, CORRELATION_KEY
from sil.profile_report import (
    SAMPLES_KEY, ProfileReportError, parse_profile_report,
)

ALLOCATIONS_KEY = "sil_profile_heap_allocations"
SUPPORTED_VERSION = 1
# The render path holds each file and its decoded document in memory, so each
# file of the set has a fixed size limit. The default capture limits give
# files far below it (docs/profile-report.md, "Offline report").
MAX_INPUT_BYTES = 256 * 1024 * 1024


class ProfileSetError(ValueError):
    """A profile set that is incomplete, corrupt or of another version."""


@dataclass(frozen=True)
class ProfileSet:
    """A valid `profile.json` and the normalized files it names."""

    report: dict
    source: str  # the exact text of profile.json, as validated
    samples: dict | None = None
    correlation: dict | None = None
    allocations: dict | None = None


def _decimal(value: Any, where: str, errors: list) -> None:
    if not (type(value) is str and value.isascii() and value.isdigit()):
        errors.append(f"{where} is not a decimal string")


def _row(*shapes: Shape) -> Shape:
    """A list with one item of each shape, in order."""
    def check(value: Any, where: str, errors: list) -> None:
        if type(value) is not list or len(value) != len(shapes):
            errors.append(f"{where} is not a list of {len(shapes)} items")
            return
        for index, (shape, item) in enumerate(zip(shapes, value)):
            shape(item, f"{where}[{index}]", errors)
    return check


def _reference(value: Any, where: str, errors: list) -> None:
    if not (type(value) is int or type(value) is str):
        errors.append(f"{where} is not an index or a name")


SOURCE = maybe(members({"file": TEXT, "line": COUNT}))
FRAME = members({"module": maybe(TEXT), "symbol": maybe(TEXT),
                 "source": SOURCE})
SAMPLES = members({
    "time_origin_ns": maybe(_decimal), "frames": items(FRAME),
    "stacks": items(items(COUNT)),
    "samples": items(_row(INTEGER, TEXT, TEXT, COUNT, COUNT))})
CORRELATION = members({
    "activations": items(members({
        "participant": TEXT, "task": TEXT, "kind": TEXT, "index": COUNT,
        "virtual_time_ns": INTEGER, "begin_ns": _decimal, "end_ns": _decimal,
        "outcome": TEXT, "samples": COUNT, "weight_ns": COUNT})),
    "replays": items(members({"participant": TEXT, "index": COUNT,
        "virtual_time_ns": INTEGER, "begin_ns": _decimal, "end_ns": _decimal,
        "outcome": TEXT, "samples": COUNT, "weight_ns": COUNT})),
    "phases": items(members({"phase": TEXT, "begin_ns": _decimal,
                             "end_ns": _decimal})),
    "samples": items(_row(TEXT, _reference))})
ALLOCATIONS = members({
    "frames": items(FRAME), "stacks": items(items(COUNT)),
    "allocations": items(_row(COUNT, COUNT, COUNT, COUNT, COUNT))})


def load_profile_set(path: Path) -> ProfileSet:
    """Read and check the set of `path` (its `profile.json`)."""
    try:
        text = _read_bounded(Path(path), str(path)).decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ProfileSetError(f"cannot read '{path}': {error}") from error
    try:
        report = parse_profile_report(text, source=str(path))
    except ProfileReportError as error:
        raise ProfileSetError(str(error)) from error
    directory = Path(path).parent
    digests = {a["path"]: a for a in report["artifacts"]}
    capture = report["capture"]
    if capture["kind"] == "heap":
        analysis = capture["analysis"]
        name = analysis["allocations_file"] if analysis else None
        allocations = _sidecar(directory, name, ALLOCATIONS_KEY, ALLOCATIONS,
                               digests)
        if allocations is not None:
            assert name is not None
            _check_allocations(allocations, analysis, name)
        return ProfileSet(report, text, allocations=allocations)
    name = capture["samples_file"]
    samples = _sidecar(directory, name, SAMPLES_KEY, SAMPLES, digests)
    if samples is not None:
        _check_samples(samples, report, name)
    summary = report["activation_correlation"].get("summary")
    correlation = None
    if summary is not None:
        if samples is None:
            raise ProfileSetError(
                "the correlation summary needs the samples file, which "
                "profile.json does not name")
        name = summary["correlation_file"]
        correlation = _sidecar(directory, name, CORRELATION_KEY, CORRELATION,
                               digests)
        if correlation is None:
            raise ProfileSetError("the summary needs a named correlation file")
        _check_correlation(correlation, samples, summary, name)
    return ProfileSet(report, text, samples=samples,
                      correlation=correlation)


def _sidecar(directory: Path, name: str | None, key: str, shape: Shape,
             digests: dict) -> dict | None:
    if name is None:
        return None
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ProfileSetError(f"'{name}' is not inside the profile directory")
    path = directory / relative
    try:
        data = _read_bounded(path, name)
    except OSError as error:
        raise ProfileSetError(
            f"profile.json names '{name}', which cannot be read ({error}); "
            "copy it with profile.json (docs/profile-report.md, "
            "\"Offline report\")") from error
    listed = digests.get(name)
    if listed is not None and (listed["bytes"] != len(data) or
                               listed["sha256"] !=
                               hashlib.sha256(data).hexdigest()):
        raise ProfileSetError(
            f"'{name}' does not have the size and SHA-256 that profile.json "
            "records: it is not the file of this profile")

    def reject_constant(constant: str) -> None:
        raise ProfileSetError(f"'{name}' contains {constant}")

    try:
        document = json.loads(data.decode("utf-8"),
                              parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProfileSetError(f"'{name}' is not valid JSON: {error}") \
            from error
    if not isinstance(document, dict) or key not in document:
        raise ProfileSetError(f"'{name}' is not a {key} document")
    if type(document[key]) is not int or \
            document[key] != SUPPORTED_VERSION:
        raise ProfileSetError(
            f"'{name}': unsupported {key} version {document[key]!r}")
    errors = shape_errors(shape, document)
    if errors:
        raise ProfileSetError(f"'{name}': " + "; ".join(errors[:5]))
    return document


def _read_bounded(path: Path, name: str) -> bytes:
    """The bytes of one file of the set; a file above MAX_INPUT_BYTES is
    refused before it is read."""
    with path.open("rb") as file:
        size = os.fstat(file.fileno()).st_size
        data = b"" if size > MAX_INPUT_BYTES else file.read(MAX_INPUT_BYTES + 1)
    if size > MAX_INPUT_BYTES or len(data) > MAX_INPUT_BYTES:
        raise ProfileSetError(
            f"'{name}' has {max(size, len(data))} bytes, larger than the "
            f"render limit of {MAX_INPUT_BYTES} bytes")
    return data


def _fail(name: str, problem: str) -> None:
    raise ProfileSetError(f"'{name}': {problem}")


def _check_stacks(document: dict, modules: set[str], name: str) -> None:
    frames = len(document["frames"])
    for index, frame in enumerate(document["frames"]):
        if frame["module"] is not None and frame["module"] not in modules:
            _fail(name, f"frames[{index}]: unknown module "
                        f"{frame['module']!r}")
    if any(f >= frames for stack in document["stacks"] for f in stack):
        _fail(name, "a stack names a frame that is not listed")


def _check_samples(samples: dict, report: dict, name: str) -> None:
    capture = report["capture"]
    if samples["time_origin_ns"] != capture["time_origin_ns"]:
        _fail(name, "time_origin_ns is not the one of profile.json")
    _check_stacks(samples, {m["id"] for m in capture["modules"]}, name)
    processes = {p["id"] for p in capture["processes"]}
    threads = {t["id"]: t["process"] for t in capture["threads"]}
    stacks = len(samples["stacks"])
    for index, (_, process, thread, _, stack) in enumerate(
            samples["samples"]):
        if threads.get(thread) != process or process not in processes:
            _fail(name, f"samples[{index}]: unknown process or thread")
        if stack >= stacks:
            _fail(name, f"samples[{index}]: unknown stack {stack}")
    totals = capture["totals"]
    rows = samples["samples"]
    weight = sum(row[3] for row in rows)
    if len(rows) != totals["samples"] or weight != totals["weight_ns"]:
        _fail(name, f"{len(rows)} samples of weight {weight} ns; "
                    f"profile.json has {totals['samples']} of "
                    f"{totals['weight_ns']} ns")


def _check_correlation(correlation: dict, samples: dict, summary: dict,
                       name: str) -> None:
    rows = correlation["samples"]
    if len(rows) != len(samples["samples"]):
        _fail(name, f"{len(rows)} sample classes for "
                    f"{len(samples['samples'])} samples")
    activations = len(correlation["activations"])
    replays = len(correlation["replays"])
    for index, (kind, ref) in enumerate(rows):
        bound = {"execution_context": activations,
                 "window_associated": activations, "replay": replays}
        if kind not in CLASSES:
            _fail(name, f"samples[{index}]: unknown class {kind!r}")
        if kind in bound and not (type(ref) is int and 0 <= ref < bound[kind]):
            _fail(name, f"samples[{index}]: unknown interval {ref!r}")
    intervals = summary["intervals"]
    if activations != intervals["activations"] + intervals["process_steps"]:
        _fail(name, f"{activations} Activations; the summary keeps "
                    f"{intervals['activations'] + intervals['process_steps']}")
    for group in ("activations", "replays", "phases"):
        for row in correlation[group]:
            if int(row["end_ns"]) < int(row["begin_ns"]):
                _fail(name, f"{group}: host interval ends before it begins")
    phases = {p["phase"] for p in correlation["phases"]}
    tasks = {(t["participant"], t["task"]) for t in summary["tasks"]}
    if {(a["participant"], a["task"]) for a in correlation["activations"]} != tasks:
        _fail(name, "Activation Tasks do not match the summary")
    if any(cls == "lifecycle" and ref not in phases for cls, ref in rows):
        _fail(name, "lifecycle sample names a phase that is not listed")
    class_count: Counter = Counter()
    class_weight: Counter = Counter()
    counts: Counter = Counter()
    weights: Counter = Counter()
    origin = int(samples["time_origin_ns"] or 0)
    for (cls, ref), sample in zip(rows, samples["samples"]):
        if cls in ("execution_context", "window_associated", "replay", "lifecycle"):
            if cls == "lifecycle":
                candidates = [p for p in correlation["phases"] if p["phase"] == ref]
            else:
                group = "replays" if cls == "replay" else "activations"
                candidates = [correlation[group][ref]]
            time = origin + sample[0]
            if not any(int(p["begin_ns"]) <= time < int(p["end_ns"]) for p in candidates):
                _fail(name, f"{cls} sample is outside its half-open host interval")
        class_count[cls] += 1
        class_weight[cls] += sample[3]
        counts[cls, ref] += 1
        weights[cls, ref] += sample[3]
    for cls in CLASSES:
        if (class_count[cls] != summary["samples"][cls]["samples"] or
                class_weight[cls] != summary["samples"][cls]["weight_ns"]):
            _fail(name, f"{cls} totals do not match the summary")
    for group, classes in (("activations", ("execution_context", "window_associated")),
                            ("replays", ("replay",))):
        for index, row in enumerate(correlation[group]):
            if (row["samples"] != sum(counts[cls, index] for cls in classes) or
                    row["weight_ns"] != sum(weights[cls, index] for cls in classes)):
                _fail(name, f"{group}[{index}] counters do not match its samples")
    task_counts: Counter = Counter()
    for index, row in enumerate(correlation["activations"]):
        key = (row["participant"], row["task"])
        task_counts[key, "activations"] += 1
        task_counts[key, row["outcome"]] += 1
        task_counts[key, "with_samples" if row["samples"] else "without_samples"] += 1
        for cls in ("execution_context", "window_associated"):
            task_counts[key, cls, "samples"] += counts[cls, index]
            task_counts[key, cls, "weight_ns"] += weights[cls, index]
    for task in summary["tasks"]:
        key = (task["participant"], task["task"])
        for field in ("activations", "completed", "failed", "interrupted",
                      "with_samples", "without_samples"):
            if task_counts[key, field] != task[field]:
                _fail(name, f"Task {key!r} {field} differs from its Activations")
        for cls in ("execution_context", "window_associated"):
            for field in ("samples", "weight_ns"):
                if task_counts[key, cls, field] != task[cls][field]:
                    _fail(name, f"Task {key!r} {cls} differs from its samples")


def _check_allocations(allocations: dict, analysis: dict, name: str) -> None:
    _check_stacks(allocations, {m["id"] for m in analysis["modules"]}, name)
    stacks = len(allocations["stacks"])
    if any(row[0] >= stacks for row in allocations["allocations"]):
        _fail(name, "an allocation names a stack that is not listed")
    totals = analysis["totals"]
    count = sum(row[2] for row in allocations["allocations"])
    size = sum(row[1] * row[2] for row in allocations["allocations"])
    if count != totals["allocations"] or size != totals["allocated_bytes"]:
        _fail(name, f"{count} allocations of {size} bytes; profile.json has "
                    f"{totals['allocations']} of {totals['allocated_bytes']}")
    for metric, actual in (
            ("temporary_allocations", sum(r[3] for r in allocations["allocations"])),
            ("outstanding_allocations", sum(r[4] for r in allocations["allocations"])),
            ("outstanding_bytes", sum(r[1] * r[4] for r in allocations["allocations"]))):
        if actual != totals[metric]:
            _fail(name, f"{metric} does not match the summary")
