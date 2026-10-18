"""Structural validation of the diagnostics report contracts (#299, #300).

Two versioned JSON documents carry Run measurements:

- ``{"sil_run_diagnostics": 1, ...}``, written by ``sil-run-diagnostics``
  for one Run;
- ``{"sil_benchmark_report": 1, ...}``, written by ``sil benchmark`` for a
  campaign of repeated Runs, with every Run report embedded so the summary
  renders on its own.

docs/diagnostics-report.md defines every field. This module checks the parts
a consumer depends on: the discriminator and version, the required members,
counts that add up, no NaN or infinity, and that every null measurement names
its reason. It does not judge timing values.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

RUN_REPORT_KEY = "sil_run_diagnostics"
BENCHMARK_REPORT_KEY = "sil_benchmark_report"
SUPPORTED_VERSION = 1

RESERVOIR_CAPACITY = 8192
SLOWEST_RETAINED = 20

RUN_STATUSES = {"succeeded", "run_failure", "configuration_error", "interrupted"}
PARTICIPANT_KINDS = {"native", "process", "replay"}
TASK_KINDS = {"native_task", "process_step"}
PHASES = (
    "preflight_ns",
    "setup_ns",
    "scheduled_execution_ns",
    "shutdown_ns",
    "recording_finalization_ns",
)
RESOURCE_FIELDS = (
    "collector",
    "interval",
    "includes_waited_descendants",
    "user_cpu_ns",
    "system_cpu_ns",
    "peak_rss_bytes",
    "peak_rss_kind",
    "limitations",
)
RESOURCE_MEMBERS = {
    "runner", "process_participants", "native_participants", "cpu_total"
}
RUN_ROLES = {"warmup", "measured"}
AGGREGATE_STATISTICS = ("min", "median", "max", "mean", "stddev")


class ReportError(ValueError):
    """A report that is not valid JSON, not a known version, or malformed."""


def load_report(path: Path) -> dict[str, Any]:
    """Read and validate either report type; raise ReportError if invalid."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise ReportError(f"cannot read report '{path}': {error}") from error
    except UnicodeDecodeError as error:
        raise ReportError(f"report '{path}' is not UTF-8: {error}") from error
    return parse_report(text, source=str(path))


def parse_report(text: str, *, source: str = "report") -> dict[str, Any]:
    """Parse and validate either report type; raise ReportError if invalid."""

    def reject_constant(name: str) -> None:
        raise ReportError(f"{source}: JSON contains {name}")

    try:
        document = json.loads(text, parse_constant=reject_constant)
    except json.JSONDecodeError as error:
        raise ReportError(f"{source}: not valid JSON: {error}") from error
    if isinstance(document, dict) and BENCHMARK_REPORT_KEY in document:
        errors = validate_benchmark_report(document)
    else:
        errors = validate_run_report(document)
    if errors:
        raise ReportError(f"{source}: " + "; ".join(errors))
    return document


def validate_run_report(document: Any) -> list[str]:
    """Return every contract violation in a Run report; empty when valid."""
    errors: list[str] = []
    if not _check_version(document, RUN_REPORT_KEY, errors):
        return errors
    for key in ("collector", "identity", "host", "configuration", "run",
                "completeness", "collection_errors", "execution",
                "observational", "artifacts"):
        if key not in document:
            errors.append(f"missing member '{key}'")
    if errors:
        return errors
    _check_finite(document, "$", errors)
    _check_reasons(document, "$", errors)
    _check_run(document, errors)
    tasks = _check_execution(document["execution"], errors)
    _check_observational(document["observational"], tasks, errors)
    return errors


def validate_benchmark_report(document: Any) -> list[str]:
    """Return every contract violation in a benchmark report; empty if valid."""
    errors: list[str] = []
    if not _check_version(document, BENCHMARK_REPORT_KEY, errors):
        return errors
    for key in ("generator", "identity", "host", "policy", "status", "counts",
                "runs", "aggregates"):
        if key not in document:
            errors.append(f"missing member '{key}'")
    if errors:
        return errors
    _check_finite(document, "$", errors)
    runs = document["runs"]
    if not isinstance(runs, list):
        return errors + ["'runs' is not a list"]
    successful = 0
    for index, run in enumerate(runs):
        where = f"runs[{index}]"
        if run.get("role") not in RUN_ROLES:
            errors.append(f"{where}: unknown role {run.get('role')!r}")
        report = run.get("report")
        if report is None:
            if run.get("report_status") != "missing":
                errors.append(f"{where}: null report not marked missing")
        else:
            errors += [f"{where}.report: {e}" for e in validate_run_report(report)]
            if report.get("run", {}).get("exit_code") != run.get("run_exit_code"):
                errors.append(f"{where}: run_exit_code differs from its report")
        if (run.get("role") == "measured" and report is not None
                and run.get("run_exit_code") == 0):
            successful += 1
    _check_counts(document["counts"], runs, errors)
    _check_aggregates(document["aggregates"], successful, errors)
    _check_reasons(document["aggregates"], "$.aggregates", errors)
    return errors


def _check_version(document: Any, key: str, errors: list[str]) -> bool:
    if not isinstance(document, dict):
        errors.append("report is not a JSON object")
        return False
    if key not in document:
        errors.append(f"missing discriminator '{key}'")
        return False
    version = document[key]
    if type(version) is not int or version != SUPPORTED_VERSION:
        errors.append(f"unsupported {key} version {version!r}")
        return False
    return True


def _check_finite(value: Any, where: str, errors: list[str]) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        errors.append(f"{where}: non-finite number")
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_finite(item, f"{where}.{key}", errors)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _check_finite(item, f"{where}[{index}]", errors)


def _check_reasons(value: Any, where: str, errors: list[str]) -> None:
    """In an object with an `unavailable` map, null members and reasons match."""
    if isinstance(value, list):
        for index, item in enumerate(value):
            _check_reasons(item, f"{where}[{index}]", errors)
        return
    if not isinstance(value, dict):
        return
    if "unavailable" in value:
        reasons = value["unavailable"]
        if not isinstance(reasons, dict):
            errors.append(f"{where}.unavailable is not an object")
        else:
            nulls = {k for k, v in value.items() if v is None}
            for key in sorted(nulls - set(reasons)):
                errors.append(f"{where}.{key} is null without a reason")
            for key in sorted(set(reasons) - nulls):
                errors.append(f"{where}.unavailable names non-null '{key}'")
            for key, reason in reasons.items():
                if not isinstance(reason, str) or not reason:
                    errors.append(f"{where}.unavailable.{key} is not a reason")
    for key, item in value.items():
        if key != "unavailable":
            _check_reasons(item, f"{where}.{key}", errors)


def _is_count(value: Any) -> bool:
    return type(value) is int and value >= 0


def _check_run(document: dict, errors: list[str]) -> None:
    run = document["run"]
    exit_code = run.get("exit_code")
    if type(exit_code) is not int:
        errors.append("run.exit_code is not an integer")
        return
    if run.get("status") not in RUN_STATUSES:
        errors.append(f"run.status {run.get('status')!r} is unknown")
    if (exit_code == 0) != (run.get("status") == "succeeded"):
        errors.append("run.status disagrees with run.exit_code")
    if (exit_code == 0) != (run.get("failure") is None):
        errors.append("run.failure must be present exactly for a failed Run")
    if document["completeness"].get("run_complete") != (exit_code == 0):
        errors.append("completeness.run_complete disagrees with the exit code")
    if not isinstance(document["collection_errors"], list):
        errors.append("collection_errors is not a list")


def _check_work_counts(row: dict, where: str, errors: list[str]) -> None:
    counts = [row.get(k) for k in ("attempted", "completed", "failed",
                                   "interrupted")]
    if not all(_is_count(c) for c in counts):
        errors.append(f"{where}: counts are not non-negative integers")
    elif counts[0] != sum(counts[1:]):
        errors.append(f"{where}: attempted is not completed + failed + "
                      "interrupted")


def _check_execution(execution: dict, errors: list[str]) -> dict:
    tasks: dict[tuple[str, str], dict] = {}
    for p_index, participant in enumerate(execution.get("participants", [])):
        where = f"execution.participants[{p_index}]"
        if participant.get("kind") not in PARTICIPANT_KINDS:
            errors.append(f"{where}: unknown kind {participant.get('kind')!r}")
        for t_index, task in enumerate(participant.get("tasks", [])):
            task_where = f"{where}.tasks[{t_index}]"
            if task.get("kind") not in TASK_KINDS:
                errors.append(f"{task_where}: unknown kind {task.get('kind')!r}")
            _check_work_counts(task, task_where, errors)
            tasks[(participant.get("participant"), task.get("task"))] = task
        if participant.get("replay") is not None:
            _check_work_counts(participant["replay"], f"{where}.replay", errors)
    return tasks


def _check_timing(timing: dict, completed: Any, where: str,
                  errors: list[str]) -> None:
    if timing.get("completed") != completed:
        errors.append(f"{where}: timing.completed differs from execution")
    quantiles = timing.get("quantiles", {})
    samples = timing.get("samples_ns", [])
    if quantiles.get("retained_count") != len(samples):
        errors.append(f"{where}: retained_count differs from samples_ns")
    if len(samples) > RESERVOIR_CAPACITY:
        errors.append(f"{where}: more than {RESERVOIR_CAPACITY} samples")
    if quantiles.get("approximate") != (
            quantiles.get("sample_count", 0) > RESERVOIR_CAPACITY):
        errors.append(f"{where}: approximate disagrees with sample_count")
    if len(timing.get("slowest", [])) > SLOWEST_RETAINED:
        errors.append(f"{where}: more than {SLOWEST_RETAINED} slow activations")


def _check_observational(observational: dict, tasks: dict,
                         errors: list[str]) -> None:
    for key in ("run_wall_time", "phases", "tasks", "replay", "throughput",
                "virtual_time_speed", "resources"):
        if key not in observational:
            errors.append(f"missing member 'observational.{key}'")
    if errors:
        return
    for phase in PHASES + ("unattributed_ns",):
        if phase not in observational["phases"]:
            errors.append(f"observational.phases.{phase} is missing")
    seen = set()
    for index, row in enumerate(observational["tasks"]):
        where = f"observational.tasks[{index}]"
        key = (row.get("participant"), row.get("task"))
        seen.add(key)
        if key not in tasks:
            errors.append(f"{where}: no such Task in execution")
            continue
        _check_timing(row.get("timing", {}), tasks[key].get("completed"),
                      where, errors)
    for key in sorted(set(tasks) - seen, key=str):
        errors.append(f"Task {key} has no observational timing")
    _check_resources(observational["resources"], errors)


def _check_resources(resources: dict, errors: list[str]) -> None:
    unknown = set(resources) - RESOURCE_MEMBERS
    if unknown:
        # Peaks of different processes need not coincide, so a member that
        # adds them up would describe memory no process tree ever used.
        errors.append("observational.resources has unknown members "
                      f"{sorted(unknown)}; no peak memory total exists")
    records = [resources.get("runner", {})] + list(
        resources.get("process_participants", []))
    for index, record in enumerate(records):
        for field in RESOURCE_FIELDS:
            if field not in record:
                errors.append(f"resource record {index} lacks '{field}'")
    total = resources.get("cpu_total")
    if not isinstance(total, dict):
        errors.append("observational.resources.cpu_total is missing")
        return
    for key in ("user_cpu_ns", "system_cpu_ns"):
        values = [record.get(key) for record in records]
        expected = None if None in values else sum(values)
        if total.get(key) != expected:
            errors.append(f"cpu_total.{key} is not the sum of the disjoint "
                          "resource records")


def _check_counts(counts: dict, runs: list, errors: list[str]) -> None:
    for role, prefix in (("measured", ""), ("warmup", "warmups_")):
        attempted = sum(1 for run in runs if run.get("role") == role)
        if counts.get(f"{prefix}attempted") != attempted:
            errors.append(f"counts.{prefix}attempted differs from runs")


def _check_aggregates(aggregates: dict, successful: int,
                      errors: list[str]) -> None:
    if aggregates.get("sample_count") != successful:
        errors.append("aggregates.sample_count is not the number of successful "
                      "measured Runs")
    for index, metric in enumerate(aggregates.get("metrics", [])):
        where = f"aggregates.metrics[{index}]"
        if len(metric.get("per_run", [])) != successful:
            errors.append(f"{where}: per_run does not list every successful "
                          "measured Run")
        for statistic in AGGREGATE_STATISTICS:
            if statistic not in metric:
                errors.append(f"{where}: missing '{statistic}'")
            elif successful == 0 and metric[statistic] is not None:
                errors.append(f"{where}: '{statistic}' without a sample")
