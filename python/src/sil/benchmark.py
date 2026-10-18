"""`sil benchmark`: repeated fresh diagnostics Runs and one summary (#303).

Every warm-up and measured Run is its own `sil run --diagnostics` Run
(sil.diagnostics_run.execute): a fresh launcher, runner and Participants, in a
fresh directory under `--out`, one after the other. The original Manifest is
passed as it is, so its relative paths resolve as in any other Run.

`summary.json` is a `sil_benchmark_report` v1 (docs/diagnostics-report.md):
the policy, every attempted Run with its complete embedded Run report, and
scalar statistics over the successful measured Runs only. Warm-ups and
failed Runs never enter a statistic, and activations are never pooled across
Runs.

Exit codes: 0 when every Run succeeded and the summary was written; the
first failed Run's exit code (as `sil run --diagnostics` gives it); 1 when
the Runs succeeded but the summary could not be written; 2 for invalid usage
or an existing `--out`; 127 when a native tool is missing.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from sil import build_info
from sil.diagnostics_report import BENCHMARK_REPORT_KEY, SUPPORTED_VERSION
from sil.diagnostics_run import EXIT_OK, Guards, RunOutcome, resolve_exit

PROG = "sil benchmark"
NO_SAMPLE = "no_successful_measured_runs"
NULL_IN_A_RUN = "unavailable_in_a_measured_run"
NO_REPORT = "no_run_report"
STATISTICS = ("min", "median", "max", "mean", "stddev")

# (metric, unit, source): scalar members every Run report has.
RUN_METRICS = (
    ("run_wall_ns", "ns", "observational.run_wall_time.wall_ns"),
    *((phase, "ns", f"observational.phases.{phase}")
      for phase in ("preflight_ns", "setup_ns", "scheduled_execution_ns",
                    "shutdown_ns", "recording_finalization_ns",
                    "unattributed_ns")),
    ("scheduled_factor", "factor",
     "observational.virtual_time_speed.scheduled_factor"),
    ("end_to_end_factor", "factor",
     "observational.virtual_time_speed.end_to_end_factor"),
    ("native_activations_per_s", "1/s",
     "observational.throughput.native_activations_per_s"),
    ("process_steps_per_s", "1/s",
     "observational.throughput.process_steps_per_s"),
    ("runner_user_cpu_ns", "ns", "observational.resources.runner.user_cpu_ns"),
    ("runner_system_cpu_ns", "ns",
     "observational.resources.runner.system_cpu_ns"),
    ("runner_peak_rss_bytes", "bytes",
     "observational.resources.runner.peak_rss_bytes"),
    ("cpu_total_user_cpu_ns", "ns",
     "observational.resources.cpu_total.user_cpu_ns"),
    ("cpu_total_system_cpu_ns", "ns",
     "observational.resources.cpu_total.system_cpu_ns"),
)
# (metric, unit, member) of each Process participant's resource record.
PROCESS_METRICS = (
    ("process_user_cpu_ns", "ns", "user_cpu_ns"),
    ("process_system_cpu_ns", "ns", "system_cpu_ns"),
    ("process_peak_rss_bytes", "bytes", "peak_rss_bytes"),
)
PROCESS_SOURCE = "observational.resources.process_participants[participant]"


@dataclass(frozen=True)
class Policy:
    """What the user asked the campaign to do."""

    repeats: int
    warmup: int
    recording: bool
    guards: Guards


@dataclass(frozen=True)
class RunResult:
    """One attempted Run of the campaign and where its evidence is."""

    label: str  # warmup-001, run-001, ...
    role: str  # warmup or measured
    index: int  # one-based within its role
    directory: str  # relative to --out
    outcome: RunOutcome

    @property
    def succeeded(self) -> bool:
        return resolve_exit(self.outcome) == EXIT_OK


def summarize(policy: Policy, manifest: Path, runs: Sequence[RunResult],
              interrupt_signal: int | None = None) -> dict[str, Any]:
    """The `sil_benchmark_report` of the Runs attempted so far."""
    reports = [run.outcome.report for run in runs
               if run.outcome.report is not None]
    first = reports[0] if reports else None
    successful = [run for run in runs
                  if run.role == "measured" and run.succeeded]
    return {
        BENCHMARK_REPORT_KEY: SUPPORTED_VERSION,
        "generator": {"name": PROG, "sil_version": build_info.__version__},
        "measurement_mode": "diagnostics",
        "identity": _identity(manifest, first),
        "host": _host(first),
        "policy": _policy(policy, first),
        "status": _status(policy, runs, interrupt_signal),
        "counts": _counts(policy, runs),
        "runs": [_run_entry(run) for run in runs],
        "aggregates": {
            "scope": "successful_measured_runs",
            "sample_count": len(successful),
            "metrics": _metrics(successful),
        },
    }


def _identity(manifest: Path, report: dict | None) -> dict:
    identity = {"manifest_path": str(manifest)}
    keys = ("manifest_hash", "sil_version", "source_revision")
    if report is None:
        identity.update({key: None for key in keys})
        identity["diagnostics_collector_version"] = None
        identity["unavailable"] = {key: NO_REPORT for key in (
            *keys, "diagnostics_collector_version")}
        return identity
    identity.update({key: report["identity"][key] for key in keys})
    identity["diagnostics_collector_version"] = \
        report["collector"]["collector_version"]
    identity["unavailable"] = {}
    return identity


def _host(report: dict | None) -> dict:
    if report is not None:
        return report["host"]
    keys = ("os", "os_release", "architecture", "logical_cpu_count",
            "cpu_description")
    return {**{key: None for key in keys},
            "unavailable": {key: NO_REPORT for key in keys}}


def _policy(policy: Policy, report: dict | None) -> dict:
    """The guards in effect: the runner's values, else the requested ones."""
    guards = vars(policy.guards)
    if report is not None:
        guards = {key: report["configuration"][key] for key in guards}
    return {"repeats": policy.repeats, "warmup": policy.warmup,
            "recording": policy.recording,
            "execution": "serial_fresh_processes", **guards}


def _status(policy: Policy, runs: Sequence[RunResult],
            interrupt_signal: int | None) -> dict:
    failed = next((run for run in runs if not run.succeeded), None)
    exit_code = resolve_exit(failed.outcome) if failed else EXIT_OK
    if interrupt_signal is not None:
        reason = "interrupted"
        if exit_code == EXIT_OK:
            exit_code = 128 + interrupt_signal
    elif failed is not None:
        reason = "warmup_failed" if failed.role == "warmup" else "run_failed"
    else:
        reason = None
    complete = reason is None and len(runs) == policy.repeats + policy.warmup
    return {"complete": complete, "exit_code": exit_code,
            "stopped_reason": reason}


def _counts(policy: Policy, runs: Sequence[RunResult]) -> dict:
    counts = {}
    for role, prefix, requested in (("measured", "", policy.repeats),
                                    ("warmup", "warmups_", policy.warmup)):
        attempted = [run for run in runs if run.role == role]
        completed = sum(1 for run in attempted if run.succeeded)
        counts.update({f"{prefix}requested": requested,
                       f"{prefix}attempted": len(attempted),
                       f"{prefix}completed": completed,
                       f"{prefix}failed": len(attempted) - completed})
    return counts


def _run_entry(run: RunResult) -> dict:
    outcome = run.outcome
    return {
        "label": run.label,
        "role": run.role,
        "index": run.index,
        "directory": run.directory,
        "run_exit_code": outcome.run_exit_code,
        "process_outcome": {"exit_code": outcome.exit_code,
                            "signal": outcome.signal},
        "report_status": "present" if outcome.report is not None
        else "missing",
        "report_path": f"{run.directory}/{outcome.report_path.name}",
        "report": outcome.report,
    }


def _metrics(successful: Sequence[RunResult]) -> list[dict]:
    reports = [(run.label, run.outcome.report) for run in successful]
    metrics = [_metric(name, unit, source,
                       [(label, _member(report, source))
                        for label, report in reports])
               for name, unit, source in RUN_METRICS]
    for participant in _process_participants(reports):
        for name, unit, member in PROCESS_METRICS:
            values = [(label, _process_record(report, participant)
                       .get(member)) for label, report in reports]
            metrics.append({"participant": participant, **_metric(
                name, unit, f"{PROCESS_SOURCE}.{member}", values)})
    return metrics


def _member(report: dict, source: str) -> Any:
    value: Any = report
    for key in source.split("."):
        value = value[key]
    return value


def _process_participants(reports) -> list[str]:
    if not reports:
        return []
    return sorted(record["participant"] for record in
                  reports[0][1]["observational"]["resources"]
                  ["process_participants"])


def _process_record(report: dict, participant: str) -> dict:
    for record in report["observational"]["resources"]["process_participants"]:
        if record["participant"] == participant:
            return record
    return {}


def _metric(name: str, unit: str, source: str,
            values: list[tuple[str, Any]]) -> dict:
    metric = {"metric": name, "unit": unit, "source": source,
              "per_run": [{"label": label, "value": value}
                          for label, value in values]}
    numbers = [value for _label, value in values]
    if not numbers or None in numbers:
        reason = NULL_IN_A_RUN if numbers else NO_SAMPLE
        metric.update({key: None for key in STATISTICS})
        metric["unavailable"] = {key: reason for key in STATISTICS}
        return metric
    metric.update({"min": min(numbers), "median": statistics.median(numbers),
                   "max": max(numbers), "mean": statistics.fmean(numbers),
                   "stddev": statistics.pstdev(numbers), "unavailable": {}})
    return metric
