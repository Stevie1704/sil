"""The documents of a source-scope capture and their budgets (#371).

docs/proposals/native-source-scopes.md, "Files" and "Resource and failure
budgets", is the contract. This module makes `sil_source_scope_map` v1,
encodes `sil_source_scope_calls` v1 and assembles `sil_source_scope_report`
v1 (`scopes.json`). The capture command (#372) writes the bytes; the
renderer (#373) reads them back.
"""

from __future__ import annotations

import json
import statistics

from sil.source_scope_decode import MAP_KEY, CaptureFailed, read_header
from sil.source_scope_map import LibraryScan, MapError

REPORT_KEY = "sil_source_scope_report"
PRODUCER = "sil profile"
MAX_MAP_BYTES = 2 << 20
MAX_CALLS = 131072
MAX_CALLS_BYTES = 256 << 20
MAX_REPORT_BYTES = 1 << 20
LIMITATIONS = (
    "Durations are inclusive elapsed host time of the instrumented build. "
    "They include observer cost, waiting and preemption. Nested durations "
    "overlap and must not be added. No observer cost is subtracted.",
    "The instrumented build can inline and optimize differently from the "
    "original build. Both identities are shown; durations do not transfer "
    "to the original build.",
    "Counts are exact only for exact Activations of a capture without loss.",
    "No CPU time, instruction trace, target ECU timing or WCET.",
)


def map_document(scan: LibraryScan, original: dict | None) -> dict:
    """`scopes/map.json` of the instrumented library and the optional
    `--original-library` identity."""
    return {MAP_KEY: 1, "library": scan.identity, "original": original,
            "scopes": scan.scopes}


def encode_map(document: dict) -> bytes:
    """The bytes of `map.json`; MapError above its 2 MiB budget, a
    preflight failure."""
    encoded = _encode(document)
    if len(encoded) > MAX_MAP_BYTES:
        raise MapError(f"the scope map has more than {MAX_MAP_BYTES} bytes")
    return encoded


def encode_calls(document: dict) -> bytes:
    """The bytes of `calls.json`; CaptureFailed `normalized_limit` above
    131,072 listed calls or 256 MiB."""
    listed = sum(len(a["calls"]) for a in document["activations"])
    if listed > MAX_CALLS:
        raise CaptureFailed("normalized_limit",
                            f"{listed} calls, more than {MAX_CALLS}")
    encoded = _encode(document)
    if len(encoded) > MAX_CALLS_BYTES:
        raise CaptureFailed("normalized_limit",
                            f"calls.json has more than {MAX_CALLS_BYTES} bytes")
    return encoded


def encode_report(document: dict) -> bytes:
    """The bytes of `scopes.json`; ValueError above its 1 MiB budget."""
    encoded = _encode(document)
    if len(encoded) > MAX_REPORT_BYTES:
        raise ValueError(f"scopes.json has more than {MAX_REPORT_BYTES} bytes")
    return encoded


def capture_record(events: bytes | None, calls: dict | None, *,
                   failure: CaptureFailed | None = None,
                   interrupted: bool = False) -> dict:
    """The `capture` member. `events` is the raw file, or None when it
    cannot be read; its header gives the capture ID and the loss counters.
    `calls` is the decoded document, or None with the decode `failure`. An
    interrupted command keeps the decoded problems."""
    if failure is not None:
        status, reasons = "failed", [failure.reason]
    else:
        status, reasons = calls["status"], list(calls["problems"])
        if interrupted:
            status, reasons = "interrupted", ["interrupted", *reasons]
    try:
        header = read_header(events) if events is not None else None
    except CaptureFailed:
        header = None
    return {
        "status": status, "reasons": reasons,
        "capture_id": header.capture_id if header else None,
        "loss": header.loss if header else None,
        "records": header.records if header else None}


def scope_report(*, sil_version: str, configuration: dict, run: dict,
                 capture: dict, identities: dict, artifacts: list,
                 missing: list, scope_map: dict, calls: dict | None) -> dict:
    """The `sil_source_scope_report` v1 document. `calls` is None when the
    decode failed; the summary is then empty."""
    return {
        REPORT_KEY: 1,
        "producer": {"name": PRODUCER, "sil_version": sil_version},
        "configuration": configuration,
        "run": run,
        "capture": capture,
        "identities": identities,
        "artifacts": artifacts,
        "missing": missing,
        "summary": _summary(calls, scope_map) if calls else
        {"tasks": [], "scopes": []},
        "limitations": list(LIMITATIONS),
    }


def _encode(document: dict) -> bytes:
    """ASCII JSON: every control and line-separator character is escaped."""
    return (json.dumps(document, indent=2) + "\n").encode("ascii")


def _summary(calls: dict, scope_map: dict) -> dict:
    tasks: dict[tuple, dict] = {}
    for a in calls["activations"]:
        task = tasks.setdefault((a["participant"], a["task"]), {
            "participant": a["participant"], "task": a["task"],
            "activations": 0, "exact": 0, "unavailable": {}})
        task["activations"] += 1
        if a["status"] == "exact":
            task["exact"] += 1
        else:
            task["unavailable"][a["reason"]] = \
                task["unavailable"].get(a["reason"], 0) + 1
    exact = [a["counts"] for a in calls["activations"]
             if a["status"] == "exact"]
    scopes = []
    for scope in scope_map["scopes"]:
        per = [counts.get(str(scope["id"]), 0) for counts in exact]
        scopes.append({
            "id": scope["id"], "name": scope["name"],
            "attributed_calls": sum(per),
            "per_exact_activation": {
                "min": min(per, default=None),
                "median": _number(statistics.median(per)) if per else None,
                "max": max(per, default=None)}})
    return {"tasks": list(tasks.values()), "scopes": scopes}


def _number(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value
