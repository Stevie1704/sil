"""The saved files of a source-scope capture, read for presentation (#373).

`sil profile render DIR/scopes.json` reads `scopes.json` and the files that
its `artifacts` list names, and checks them before any output
(docs/proposals/native-source-scopes.md, "Identity checks" and
"Presentation"): each listed file must be inside `DIR`, have its recorded
size and SHA-256 and a known contract version. A file in `missing` is not
read. It never opens the instrumented library. A failed check is a
ProfileSetError, exit 2 of the command.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from sil.diagnostics_shape import (
    COUNT, INTEGER, NUMBER, TEXT, Shape, items, maybe, members, one_of,
    shape_errors,
)
from sil.profile_navigation import check_selection, selected, selection_label
from sil.profile_set import ProfileSetError, decimal_text, read_bounded
from sil.recording import MCAP_MAGIC
from sil.source_scope_decode import (
    ACTIVATION_REASONS, CALLS_KEY, LOSS_KEYS, MAGIC, MAP_KEY,
    OBSERVATIONS_KEY,
)
from sil.source_scope_report import (
    CALLS_PATH, DIAGNOSTICS_PATH, EVENTS_PATH, MAP_PATH, OBSERVATIONS_PATH,
    REPORT_KEY,
)

CAPTURE_STATUSES = ("complete", "partial", "failed", "interrupted")
HASH_CHUNK = 1 << 20


@dataclass(frozen=True)
class ScopeSet:
    """A checked `scopes.json` and the decoded files it names."""

    report: dict
    source: str  # the exact text of scopes.json
    scope_map: dict | None
    calls: dict | None
    selection: tuple[int, ...] | None = None
    selection_label: str | None = None


def _counts(value: Any, where: str, errors: list) -> None:
    """An object of non-negative integer counts."""
    if type(value) is not dict:
        errors.append(f"{where} is not an object")
        return
    for key, count in value.items():
        COUNT(count, f"{where}.{key}", errors)


LIBRARY = members({"path": TEXT, "build_id": maybe(TEXT),
                   "sha256": maybe(TEXT)})
REPORT = members({
    "producer": members({"name": TEXT}),
    "configuration": members({}),
    "run": members({"status": TEXT, "exit_code": maybe(INTEGER),
                    "signal": maybe(COUNT)}),
    "capture": members({
        "status": one_of(CAPTURE_STATUSES), "reasons": items(TEXT),
        "capture_id": maybe(TEXT), "records": maybe(COUNT),
        "loss": maybe(members({key: COUNT for key in LOSS_KEYS}))}),
    "identities": members({}, {"instrumented": maybe(LIBRARY),
                               "original": maybe(LIBRARY)}),
    "artifacts": items(members({"role": TEXT, "path": TEXT, "bytes": COUNT,
                                "sha256": TEXT})),
    "missing": items(members({"role": TEXT, "path": TEXT, "reason": TEXT})),
    "summary": members({
        "tasks": items(members({
            "participant": TEXT, "task": TEXT, "activations": COUNT,
            "exact": COUNT, "unavailable": _counts})),
        "scopes": items(members({
            "id": COUNT, "name": TEXT, "attributed_calls": COUNT,
            "per_exact_activation": members({
                "min": maybe(COUNT), "median": maybe(NUMBER),
                "max": maybe(COUNT)})}))}),
    "limitations": items(TEXT)})
SCOPE_MAP = members({
    "library": LIBRARY, "original": maybe(LIBRARY),
    "scopes": items(members({"id": COUNT, "name": TEXT, "sites": items(
        members({"file": TEXT, "line": COUNT}))}))})
CALL = members({"scope": COUNT, "depth": COUNT, "parent": maybe(COUNT),
                "entry_ns": decimal_text, "return_ns": decimal_text,
                "elapsed_ns": decimal_text})
ACTIVATION = members({
    "participant": TEXT, "task": TEXT, "index": COUNT,
    "virtual_time_ns": INTEGER, "begin_ns": decimal_text, "end_ns": decimal_text,
    "outcome": TEXT, "status": one_of(("exact", "unavailable")),
    "reason": maybe(one_of(ACTIVATION_REASONS)), "withheld_calls": COUNT,
    "calls": items(CALL), "counts": maybe(_counts)})
CALLS = members({
    "capture_id": TEXT, "participant": TEXT, "status": TEXT,
    "problems": items(TEXT),
    "totals": members({key: COUNT for key in (
        "entries", "returns", "paired_calls", "attributed_calls",
        "withheld_calls", "unattributed")}),
    "activations": items(ACTIVATION),
    "unattributed": members({
        "reasons": _counts, "rows_remainder": COUNT,
        "rows": items(members({"scope": COUNT, "tid": COUNT,
                               "entry_ns": maybe(decimal_text),
                               "return_ns": maybe(decimal_text),
                               "reason": TEXT}))})})


def _json(data: bytes, name: str) -> Any:
    def reject_constant(constant: str) -> None:
        raise ProfileSetError(f"'{name}' contains {constant}")

    try:
        return json.loads(data.decode("utf-8"), parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ProfileSetError(f"'{name}' is not valid JSON: {error}") from error


def _versioned(document: Any, key: str, shape: Shape | None,
               name: str) -> dict:
    """`document` when it is version 1 of `key` and has `shape`."""
    if not isinstance(document, dict) or key not in document:
        raise ProfileSetError(f"'{name}' is not a {key} document")
    if type(document[key]) is not int or document[key] != 1:
        raise ProfileSetError(f"'{name}': unsupported {key} version "
                              f"{document[key]!r}")
    errors = shape_errors(shape, document) if shape is not None else []
    if errors:
        raise ProfileSetError(f"'{name}': " + "; ".join(errors[:5]))
    return document


def is_scope_report(path: Path) -> bool:
    """True when `path` holds a document with the scope report's key."""
    try:
        document = _json(read_bounded(Path(path), str(path)), str(path))
    except (OSError, ProfileSetError):
        return False
    return isinstance(document, dict) and REPORT_KEY in document


def load_scope_set(path: Path) -> ScopeSet:
    """Read and check `scopes.json` and every artifact it lists."""
    path = Path(path)
    try:
        data = read_bounded(path, str(path))
    except OSError as error:
        raise ProfileSetError(f"cannot read '{path}': {error}") from error
    report = _versioned(_json(data, str(path)), REPORT_KEY, REPORT,
                        str(path))
    documents = {}
    for artifact in report["artifacts"]:
        documents[artifact["path"]] = _check_artifact(path.parent, artifact)
    calls = documents.get(CALLS_PATH)
    if calls is not None and \
            calls["capture_id"] != report["capture"]["capture_id"]:
        raise ProfileSetError(f"'{CALLS_PATH}' belongs to another capture "
                              f"({calls['capture_id']})")
    return ScopeSet(report, data.decode("utf-8"), documents.get(MAP_PATH),
                    calls)


def select_scope_activations(scope_set: ScopeSet, *, participant=None,
                             task=None, index=None, start=None,
                             end=None) -> ScopeSet:
    """Select Activations of `calls.json` with the rules of `profile.json`
    (docs/profile-report.md, "Activation outliers and offline selection")."""
    options = {"participant": participant, "task": task, "index": index,
               "start": start, "end": end}
    if not check_selection(**options):
        return scope_set
    if scope_set.calls is None:
        raise ProfileSetError("offline Activation selection requires the "
                              "decoded calls file, which this capture lacks")
    candidates = [(i, a) for i, a in enumerate(scope_set.calls["activations"])
                  if (participant is None or a["participant"] == participant)
                  and (task is None or a["task"] == task)]
    if participant is not None and not candidates:
        raise ProfileSetError("Participant / Task is not present in the "
                              "retained evidence")
    refs = tuple(i for i, a in candidates
                 if selected(a, index=index, start=start, end=end))
    return replace(scope_set, selection=refs,
                   selection_label=selection_label(**options))


def _inside(directory: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ProfileSetError(f"'{name}' is not inside the capture directory")
    return directory / relative


def _check_artifact(directory: Path, artifact: dict) -> dict | None:
    """Check one listed file; return its document when the views read it."""
    name = artifact["path"]
    path = _inside(directory, name)
    if artifact["role"] == "recording" and name not in (
            CALLS_PATH, MAP_PATH, EVENTS_PATH, OBSERVATIONS_PATH,
            DIAGNOSTICS_PATH):
        _check_digest(name, artifact, *_hash_file(path, name))
        with path.open("rb") as file:
            if file.read(len(MCAP_MAGIC)) != MCAP_MAGIC:
                raise ProfileSetError(f"'{name}' is not an MCAP Recording")
        return None
    try:
        data = read_bounded(path, name)
    except OSError as error:
        raise ProfileSetError(f"scopes.json lists '{name}', which cannot be "
                              f"read ({error})") from error
    _check_digest(name, artifact, len(data), hashlib.sha256(data).hexdigest())
    if name == CALLS_PATH:
        return _check_calls(_versioned(_json(data, name), CALLS_KEY, CALLS,
                                       name), name)
    if name == MAP_PATH:
        return _versioned(_json(data, name), MAP_KEY, SCOPE_MAP, name)
    if name == EVENTS_PATH:
        if data[:len(MAGIC)] != MAGIC or data[8:10] != b"\x01\x00":
            raise ProfileSetError(f"'{name}' is not a version-1 raw file")
    elif name == OBSERVATIONS_PATH:
        _versioned(_json(data.split(b"\n", 1)[0], name), OBSERVATIONS_KEY,
                   None, name)
    elif name == DIAGNOSTICS_PATH:
        _versioned(_json(data, name), "sil_run_diagnostics", None, name)
    else:
        raise ProfileSetError(f"'{name}' is not a file of a source-scope "
                              "capture")
    return None


def _hash_file(path: Path, name: str) -> tuple[int, str]:
    digest, size = hashlib.sha256(), 0
    try:
        with path.open("rb") as file:
            while chunk := file.read(HASH_CHUNK):
                digest.update(chunk)
                size += len(chunk)
    except OSError as error:
        raise ProfileSetError(f"scopes.json lists '{name}', which cannot be "
                              f"read ({error})") from error
    return size, digest.hexdigest()


def _check_digest(name: str, artifact: dict, size: int, sha256: str) -> None:
    if (artifact["bytes"], artifact["sha256"]) != (size, sha256):
        raise ProfileSetError(
            f"'{name}' does not have the size and SHA-256 that scopes.json "
            "records: it is not the file of this capture")


def _check_calls(calls: dict, name: str) -> dict:
    """An exact Activation has counts and no reason; an unavailable one has
    a reason, no calls and no counts, so no view can show partial counts."""
    for index, activation in enumerate(calls["activations"]):
        exact = activation["status"] == "exact"
        if exact != (activation["reason"] is None) or \
                exact != (activation["counts"] is not None) or \
                (not exact and activation["calls"]):
            raise ProfileSetError(f"'{name}': activations[{index}] mixes an "
                                  "exact and an unavailable record")
    return calls
