"""Structural validation of the `sil_profile_report` v1 contract (#315).

docs/profile-report.md defines every member. This module checks what a
consumer depends on: the discriminator and version, the type of every member
a consumer reads, the `kind` of each variant, enumerated values, no NaN or
infinity, null measurements with a reason, identities that resolve, and
totals that add up. It does not judge sample values.
"""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from sil.diagnostics_report import check_finite, check_reasons
from sil.diagnostics_shape import (
    COUNT, FLAG, INTEGER, TEXT, Shape, items, maybe, members, reasoned,
    shape_errors,
)

REPORT_KEY = "sil_profile_report"
SAMPLES_KEY = "sil_profile_cpu_samples"
SUPPORTED_VERSION = 1

RUN_STATUSES = {"succeeded", "run_failure", "configuration_error",
                "interrupted", "ended_by_signal", "not_started"}
CAPTURE_STATUSES = {"complete", "partial", "interrupted", "failed",
                    "not_started"}
CPU_REASONS = {"lost_samples", "capture_limit_reached", "interrupted",
               "backend_failed", "export_failed", "run_not_started"}
DEBUG_STATUSES = {"embedded", "separate_matching", "separate_mismatched",
                  "not_found", "unverifiable", "module_unavailable"}
# Only these debug states can give a source line (docs/profile-report.md).
SOURCE_DEBUG_STATUSES = {"embedded", "separate_matching"}
MATERIALS = {"host_file", "retained", "unavailable"}
PROCESS_ROLES = {"launcher", "runner", "descendant"}
SOURCE_LINE_STATUSES = {"available", "partial", "unavailable"}


class ProfileReportError(ValueError):
    """A profile report that is not valid JSON, not v1, or malformed."""


def _leaf(test: Callable[[Any], bool], name: str) -> Shape:
    def check(value: Any, where: str, errors: list) -> None:
        if not test(value):
            errors.append(f"{where} is not {name}")
    return check


def one_of(values: set[str]) -> Shape:
    def check(value: Any, where: str, errors: list) -> None:
        if value not in values:
            errors.append(f"{where}: unknown value {value!r}")
    return check


def variant(kinds: dict[str, Shape]) -> Shape:
    """An object whose `kind` member selects its shape."""
    def check(value: Any, where: str, errors: list) -> None:
        if type(value) is not dict or "kind" not in value:
            errors.append(f"missing member '{where}.kind'")
            return
        shape = kinds.get(value["kind"])
        if shape is None:
            errors.append(f"{where}.kind: unknown kind {value['kind']!r}")
            return
        shape(value, where, errors)
    return check


DECIMAL = _leaf(lambda v: type(v) is str and v.isascii() and v.isdigit(),
                "a decimal string")
HEX = _leaf(lambda v: type(v) is str
            and re.fullmatch(r"0x[0-9a-f]+", v) is not None,
            "a hexadecimal string")
STATUS_AND_REASONS = {"status": TEXT, "reasons": items(TEXT)}

SAMPLE_TOTALS = {"samples": COUNT, "weight_ns": COUNT}
SOURCE_LINE = members({"file": TEXT, "line": COUNT, "samples": COUNT})
HOTSPOT = members({"module": maybe(TEXT), "symbol": maybe(TEXT),
                   "self_samples": COUNT, "self_weight_ns": COUNT,
                   "inclusive_samples": COUNT, "inclusive_weight_ns": COUNT,
                   "source_lines": items(SOURCE_LINE)})
PROCESS = members({
    "id": TEXT, "pid": INTEGER, "parent": maybe(TEXT),
    "role": one_of(PROCESS_ROLES), "commands": items(TEXT), "execs": COUNT,
    "start_offset_ns": maybe(COUNT), "end_offset_ns": maybe(COUNT),
    "forked": FLAG, "exited": FLAG, **SAMPLE_TOTALS, "threads": COUNT})
THREAD = members({"id": TEXT, "process": TEXT, "tid": INTEGER,
                  "commands": items(TEXT), **SAMPLE_TOTALS})
MODULE = reasoned(
    {"id": TEXT, "path": TEXT, "material": one_of(MATERIALS),
     "debug": members({"status": one_of(DEBUG_STATUSES),
                       "path": maybe(TEXT)}),
     "mappings": items(members({"process": TEXT, "start": HEX, "size": HEX,
                                "file_offset": HEX})),
     "samples_leaf": COUNT, "samples_unknown_symbol": COUNT},
    {"build_id": TEXT})
CPU_CAPTURE = reasoned({
    "kind": TEXT, **STATUS_AND_REASONS,
    "backend": members({"name": TEXT, "version": TEXT,
                        "command": items(TEXT), "exit_code": maybe(INTEGER),
                        "signal": maybe(COUNT)}),
    "sampling": members({
        "event": TEXT, "event_type": one_of({"software", "hardware"}),
        "privilege": one_of({"user"}), "mode": one_of({"frequency"}),
        "frequency_hz": COUNT, "weight": one_of({"period"}),
        "weight_unit": one_of({"cpu_clock_ns"}),
        "clock": one_of({"host_monotonic"})}),
    "call_graph": members({"method": one_of({"dwarf", "fp"}),
                           "stack_bytes": COUNT}),
    "scope": members({"kind": one_of({"runner_and_descendants"}),
                      "inherit": FLAG, "pid_namespace": TEXT}),
    "limits": members({"max_capture_bytes": COUNT, "capture_bytes": COUNT,
                       "limit_reached": FLAG,
                       "symbolization_timeout_s": COUNT}),
    "totals": reasoned(
        {**SAMPLE_TOTALS, "samples_unknown_module": COUNT,
         "samples_unknown_symbol": COUNT, "samples_with_caller": COUNT,
         "lost_records": COUNT},
        {"lost_samples": COUNT}),
    "symbolization": members({
        "function_names": members({"samples_with_name": COUNT}),
        "call_frame_unwinding": members({"method": TEXT,
                                         "samples_with_caller": COUNT}),
        "source_lines": members({"status": one_of(SOURCE_LINE_STATUSES),
                                 "scope": TEXT,
                                 "samples_with_line": maybe(COUNT),
                                 "unavailable": members({})}),
        "inlined_frames": members({"status": TEXT})}),
    "processes": items(PROCESS),
    "threads": items(THREAD),
    "modules": items(MODULE),
    "hotspots": members({"limit": COUNT, "self": items(HOTSPOT),
                         "self_remainder": members(SAMPLE_TOTALS),
                         "inclusive": items(HOTSPOT)}),
}, {"time_origin_ns": DECIMAL, "samples_file": TEXT})
HEAP_SCOPE = variant({
    "runner": members({}),
    "process_participant": members({"participant": TEXT}),
    "unsupported": members({"requested": TEXT, "reason": TEXT}),
})
HEAP_CAPTURE = reasoned(
    {"kind": TEXT, **STATUS_AND_REASONS,
     "backend": reasoned({"name": TEXT}, {"version": TEXT}),
     "scope": HEAP_SCOPE},
    {"raw": TEXT, "analysis": members({})})
CORRELATION = variant({
    "not_requested": members({}),
    "activation": reasoned(
        STATUS_AND_REASONS,
        {"clock": TEXT, "observations": TEXT, "summary": members({})}),
})
REPORT = members({
    "producer": members({"name": TEXT, "sil_version": TEXT}),
    "identity": reasoned({"manifest_path": TEXT},
                         {"manifest_hash": TEXT, "sil_version": TEXT,
                          "source_repository": TEXT,
                          "source_revision": TEXT}),
    "host": reasoned({"os": TEXT, "kernel_release": TEXT,
                      "architecture": TEXT},
                     {"perf_event_paranoid": INTEGER}),
    "configuration": members({
        "collector": one_of({"cpu", "heap"}), "recording": maybe(TEXT),
        "participant_timeout_ms": maybe(COUNT),
        "max_protocol_line_bytes": maybe(COUNT),
        "max_step_output_messages": maybe(COUNT),
        "max_step_inline_payload_bytes": maybe(COUNT), "cwd": TEXT}),
    "run": members({
        "status": one_of(RUN_STATUSES), "exit_code": maybe(INTEGER),
        "signal": maybe(COUNT),
        "failure": maybe(members({"message": TEXT, "category": TEXT})),
        "diagnostics_report": maybe(TEXT),
        "supervisor_signal": maybe(COUNT)}),
    "capture": variant({"cpu": CPU_CAPTURE, "heap": HEAP_CAPTURE}),
    "activation_correlation": CORRELATION,
    "artifacts": items(members({"role": TEXT, "path": TEXT, "bytes": COUNT,
                                "sha256": TEXT})),
    "limitations": items(TEXT),
})


def load_profile_report(path: Path) -> dict[str, Any]:
    """Read and validate a profile report; raise ProfileReportError."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ProfileReportError(
            f"cannot read profile report '{path}': {error}") from error
    return parse_profile_report(text, source=str(path))


def parse_profile_report(text: str, *,
                         source: str = "profile report") -> dict[str, Any]:
    """Parse and validate a profile report; raise ProfileReportError."""

    def reject_constant(name: str) -> None:
        raise ProfileReportError(f"{source}: JSON contains {name}")

    try:
        document = json.loads(text, parse_constant=reject_constant)
    except json.JSONDecodeError as error:
        raise ProfileReportError(
            f"{source}: not valid JSON: {error}") from error
    errors = validate_profile_report(document)
    if errors:
        raise ProfileReportError(f"{source}: " + "; ".join(errors))
    return document


def validate_profile_report(document: Any) -> list[str]:
    """Every contract violation of a profile report; empty when valid."""
    if not isinstance(document, dict) or REPORT_KEY not in document:
        return [f"not a {REPORT_KEY} document"]
    version = document[REPORT_KEY]
    if type(version) is not int or version != SUPPORTED_VERSION:
        return [f"unsupported {REPORT_KEY} version {version!r}"]
    errors = shape_errors(REPORT, document)
    if errors:
        return errors
    raw: list[str] = []
    check_finite(document, "$", raw)
    check_reasons(document, "$", raw)
    errors += [error.removeprefix("$.") for error in raw]
    _check_run(document["run"], errors)
    _check_artifacts(document["artifacts"], errors)
    capture = document["capture"]
    _check_status(capture, "capture", errors)
    if capture["kind"] == "cpu":
        _check_cpu(capture, errors)
    elif capture["analysis"] is not None:
        errors.append("capture.analysis is reserved and must be null in "
                      "version 1")
    correlation = document["activation_correlation"]
    if correlation["kind"] == "activation":
        _check_status(correlation, "activation_correlation", errors)
        if correlation["summary"] is not None:
            errors.append("activation_correlation.summary is reserved and "
                          "must be null in version 1")
    return errors


def _check_status(record: dict, where: str, errors: list[str]) -> None:
    status, reasons = record["status"], record["reasons"]
    if status == "complete" and reasons:
        errors.append(f"{where}: status 'complete' with reasons")
    elif status != "complete" and not reasons:
        errors.append(f"{where}: status {status!r} without a reason")


def _check_run(run: dict, errors: list[str]) -> None:
    status, code = run["status"], run["exit_code"]
    if status == "succeeded" and code != 0:
        errors.append(f"run: status 'succeeded' with exit code {code}")
    if status in ("ended_by_signal", "not_started"):
        if code is not None:
            errors.append(f"run: status {status!r} with an exit code")
    elif code is None:
        errors.append(f"run: status {status!r} without an exit code")
    if (status == "ended_by_signal") != (run["signal"] is not None):
        errors.append("run: a signal is given exactly for 'ended_by_signal'")


def _check_artifacts(artifacts: list, errors: list[str]) -> None:
    seen: set[str] = set()
    for index, artifact in enumerate(artifacts):
        path = PurePosixPath(artifact["path"])
        if path.is_absolute() or ".." in path.parts or not path.parts:
            errors.append(f"artifacts[{index}].path: not a relative path "
                          "inside the profile directory")
        if artifact["path"] in seen:
            errors.append(f"artifacts[{index}].path: listed twice")
        seen.add(artifact["path"])


def _check_cpu(capture: dict, errors: list[str]) -> None:
    unknown = set(capture["reasons"]) - CPU_REASONS
    if unknown:
        errors.append(f"capture.reasons: unknown reasons {sorted(unknown)}")
    totals = capture["totals"]
    processes = _identities(capture["processes"], "processes", errors)
    _identities(capture["threads"], "threads", errors)
    modules = {m["id"]: m for m in capture["modules"]}
    for index, process in enumerate(capture["processes"]):
        parent = process["parent"]
        if parent is not None and parent not in processes:
            errors.append(f"capture.processes[{index}].parent: unknown "
                          f"process {parent!r}")
    for index, thread in enumerate(capture["threads"]):
        if thread["process"] not in processes:
            errors.append(f"capture.threads[{index}].process: unknown "
                          f"process {thread['process']!r}")
    for index, module in enumerate(capture["modules"]):
        for mapping in module["mappings"]:
            if mapping["process"] not in processes:
                errors.append(f"capture.modules[{index}].mappings: unknown "
                              f"process {mapping['process']!r}")
    for name in ("processes", "threads"):
        _check_sum(capture[name], f"capture.{name}", totals, errors)
    leaf = (sum(m["samples_leaf"] for m in capture["modules"])
            + totals["samples_unknown_module"])
    if leaf != totals["samples"]:
        errors.append(f"capture.modules: leaf samples add up to {leaf}, "
                      f"totals.samples is {totals['samples']}")
    _check_hotspots(capture["hotspots"], totals, modules, errors)


def _identities(rows: list, name: str, errors: list[str]) -> set[str]:
    ids = [row["id"] for row in rows]
    if len(set(ids)) != len(ids):
        errors.append(f"capture.{name}: an identity is listed twice")
    return set(ids)


def _check_sum(rows: list, where: str, totals: dict,
               errors: list[str]) -> None:
    for key in ("samples", "weight_ns"):
        total = sum(row[key] for row in rows)
        if total != totals[key]:
            errors.append(f"{where}: {key} add up to {total}, "
                          f"totals.{key} is {totals[key]}")


def _check_hotspots(hotspots: dict, totals: dict, modules: dict,
                    errors: list[str]) -> None:
    remainder = hotspots["self_remainder"]
    for key in ("samples", "weight_ns"):
        unit = "samples" if key == "samples" else "ns of weight"
        total = (sum(row[f"self_{key}"] for row in hotspots["self"])
                 + remainder[key])
        if total != totals[key]:
            errors.append(f"capture.hotspots: self rows and self_remainder "
                          f"add up to {total} {unit}, totals.{key} is "
                          f"{totals[key]}")
    for name in ("self", "inclusive"):
        rows = hotspots[name]
        if len(rows) > hotspots["limit"]:
            errors.append(f"capture.hotspots.{name}: more rows than limit")
        for index, row in enumerate(rows):
            _check_hotspot(row, f"capture.hotspots.{name}[{index}]", totals,
                           modules, errors)


def _check_hotspot(row: dict, where: str, totals: dict, modules: dict,
                   errors: list[str]) -> None:
    module = row["module"]
    if module is not None and module not in modules:
        errors.append(f"{where}.module: unknown module {module!r}")
        return
    if row["inclusive_samples"] < row["self_samples"]:
        errors.append(f"{where}: inclusive samples below self samples")
    if row["inclusive_samples"] > totals["samples"]:
        errors.append(f"{where}: inclusive samples above totals.samples")
    if row["source_lines"]:
        status = (modules[module]["debug"]["status"] if module is not None
                  else None)
        if status not in SOURCE_DEBUG_STATUSES:
            errors.append(f"{where}: source lines from module {module!r} "
                          f"whose debug status is {status!r}")
