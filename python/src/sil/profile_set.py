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


class ProfileSetError(ValueError):
    """A profile set that is incomplete, corrupt or of another version."""


@dataclass(frozen=True)
class ProfileSet:
    """A valid `profile.json` and the normalized files it names."""

    report: dict
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
    "replays": items(members({"samples": COUNT, "weight_ns": COUNT})),
    "samples": items(_row(TEXT, _reference))})
ALLOCATIONS = members({
    "frames": items(FRAME), "stacks": items(items(COUNT)),
    "allocations": items(_row(COUNT, COUNT, COUNT, COUNT, COUNT))})


def load_profile_set(path: Path) -> ProfileSet:
    """Read and check the set of `path` (its `profile.json`)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
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
            _check_allocations(allocations, analysis, name)
        return ProfileSet(report, allocations=allocations)
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
        _check_correlation(correlation, samples, summary, name)
    return ProfileSet(report, samples=samples, correlation=correlation)


def _sidecar(directory: Path, name: str | None, key: str, shape: Shape,
             digests: dict) -> dict | None:
    if name is None:
        return None
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ProfileSetError(f"'{name}' is not inside the profile directory")
    path = directory / relative
    try:
        data = path.read_bytes()
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
