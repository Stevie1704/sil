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

`--html PATH` also writes the standalone HTML page of summary.json
(sil.diagnostics_html) after the summary; PATH may be inside `--out`.

Exit codes: 0 when every Run succeeded and the summary (and HTML report, if
asked for) was written; the first failed Run's exit code (as `sil run
--diagnostics` gives it); 1 when the Runs succeeded but the summary or the
HTML report could not be written; 2 for invalid usage
or an existing `--out`; 127 when a native tool is missing.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from sil import build_info
from sil.diagnostics_html import write_html
from sil.diagnostics_report import (
    BENCHMARK_REPORT_KEY, SUPPORTED_VERSION, new_path_problem,
    validate_benchmark_report, write_new_file,
)
from sil.diagnostics_run import (
    EXIT_FAILURE, EXIT_NOT_FOUND, EXIT_OK, EXIT_USAGE,
    FORWARDED_SIGNALS, GivenOnce, Guards, NativeTools, RunOutcome, RunRequest,
    ToolMissing, add_guard_arguments, command_error, default_report_path,
    describe_problems, execute, find_native_tools, format_duration_ns,
    format_value, guards_from, resolve_exit,
)

PROG = "sil benchmark"
SUMMARY = "summary.json"
RECORDING = "run.mcap"
DEFAULT_REPEATS = 5
DEFAULT_WARMUP = 1
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
        reason = _stopped_reason(failed)
    else:
        reason = None
    complete = reason is None and len(runs) == policy.repeats + policy.warmup
    return {"complete": complete, "exit_code": exit_code,
            "stopped_reason": reason}


def _stopped_reason(failed: RunResult) -> str:
    """A Run that exited 0 failed only for want of a valid report."""
    if failed.outcome.run_exit_code == EXIT_OK:
        return "report_failed"
    return "warmup_failed" if failed.role == "warmup" else "run_failed"


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
    """Every Process participant of any Run; one a Run lacks is null there."""
    return sorted({record["participant"] for _label, report in reports
                   for record in report["observational"]["resources"]
                   ["process_participants"]})


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


# --- the campaign -----------------------------------------------------------


def plan(policy: Policy) -> list[tuple[str, str, int]]:
    """(label, role, index) of every Run, warm-ups first."""
    return ([(f"warmup-{i:03d}", "warmup", i)
             for i in range(1, policy.warmup + 1)]
            + [(f"run-{i:03d}", "measured", i)
               for i in range(1, policy.repeats + 1)])


class _Interrupts:
    """Notes a signal that arrives between Runs, so no later Run starts.

    During a Run, sil.diagnostics_run.execute forwards signals to the Run
    instead and reports them as the outcome's `supervisor_signal`.
    """

    def __init__(self):
        self.signal: int | None = None
        self._previous: dict = {}

    def note(self, number: int | None) -> None:
        if self.signal is None:
            self.signal = number

    def __enter__(self) -> _Interrupts:
        self._previous = {
            number: signal.signal(number, lambda n, _f: self.note(n))
            for number in FORWARDED_SIGNALS}
        return self

    def __exit__(self, *_exc) -> None:
        for number, handler in self._previous.items():
            signal.signal(number, handler)


class _Tee:
    """Copies the Run's stderr to its log and to this command's stderr."""

    def __init__(self, *sinks):
        self.sinks = sinks

    def write(self, data: bytes) -> None:
        for sink in self.sinks:
            sink.write(data)

    def flush(self) -> None:
        for sink in self.sinks:
            sink.flush()


def run_campaign(policy: Policy, manifest: Path, out: Path,
                 tools: NativeTools) -> tuple[list[RunResult], int | None]:
    """Execute the Runs in order; stop at the first failure or signal."""
    results: list[RunResult] = []
    with _Interrupts() as interrupts:
        for label, role, index in plan(policy):
            if interrupts.signal is not None:
                break
            result = _run_one(policy, manifest, out, tools, label, role, index)
            results.append(result)
            interrupts.note(result.outcome.supervisor_signal)
            if not result.succeeded:
                for line in describe_problems(result.outcome):
                    sys.stderr.write(f"{PROG}: {label}: {line}\n")
                sys.stderr.write(
                    f"{PROG}: {label} failed (exit code "
                    f"{resolve_exit(result.outcome)}); no later Run starts\n")
                break
    return results, interrupts.signal


def _run_one(policy: Policy, manifest: Path, out: Path, tools: NativeTools,
             label: str, role: str, index: int) -> RunResult:
    directory = out / label
    directory.mkdir()
    recording = directory / RECORDING if policy.recording else None
    report = directory / default_report_path(
        Path(RECORDING) if policy.recording else None)
    request = RunRequest(manifest, report, recording, guards=policy.guards)
    with open(directory / "stdout.log", "wb") as stdout, \
            open(directory / "stderr.log", "wb") as stderr:
        outcome = execute(request, tools, cwd=directory, stdout=stdout,
                          stderr=_Tee(stderr, sys.stderr.buffer))
    return RunResult(label, role, index, label, outcome)


def summary_text(summary: dict) -> str:
    """The exact text of summary.json."""
    return json.dumps(summary, indent=2, allow_nan=False) + "\n"


def _write_summary(summary: dict, path: Path) -> str | None:
    """Write the summary; return why it could not be written, or None."""
    errors = validate_benchmark_report(summary)
    if errors:
        return "the summary is not a valid report: " + "; ".join(errors)
    try:
        write_new_file(path, summary_text(summary))
    except (OSError, ValueError) as error:
        return str(error)
    return None


# --- terminal summary -------------------------------------------------------


def format_summary(summary: dict, summary_path: Path) -> str:
    """A text summary of a benchmark report, with units and scope."""
    status = summary["status"]
    counts = summary["counts"]
    state = ("complete" if status["complete"]
             else f"incomplete ({status['stopped_reason'] or 'not all Runs'})")
    lines = [
        f"benchmark summary: {summary_path}",
        f"benchmark: {state}, exit code {status['exit_code']}; measurement "
        "mode diagnostics (instrumented Runs, not uninstrumented timings)",
        f"{counts['completed']} of {counts['requested']} measured Runs "
        f"succeeded ({counts['attempted']} attempted); "
        f"{counts['warmups_completed']} of {counts['warmups_requested']} "
        "warm-ups succeeded, never aggregated",
        "runs:",
    ]
    for run in summary["runs"]:
        lines.append(f"  {run['label']}: " + _run_state(run))
    aggregates = summary["aggregates"]
    lines.append(f"aggregates over {aggregates['sample_count']} successful "
                 "measured Runs (min / median / max / mean / population "
                 "stddev):")
    for metric in aggregates["metrics"]:
        name = metric["metric"]
        if "participant" in metric:
            name += f" ({metric['participant']})"
        if metric["unavailable"]:
            reason = next(iter(metric["unavailable"].values()))
            lines.append(f"  {name}: unavailable ({reason})")
            continue
        lines.append(f"  {name}: " + " / ".join(
            format_value(metric["unit"], metric[key]) for key in STATISTICS))
    return "\n".join(lines) + "\n"


def _run_state(run: dict) -> str:
    report = run["report"]
    if report is None:
        outcome = run["process_outcome"]
        ended = (f"signal {outcome['signal']}" if outcome["signal"]
                 is not None else f"exit code {outcome['exit_code']}")
        return f"{run['role']}, ended by {ended}, no diagnostics report"
    wall = report["observational"]["run_wall_time"]["wall_ns"]
    wall_text = "unavailable" if wall is None else format_duration_ns(wall)
    return (f"{run['role']}, {report['run']['status']}, exit code "
            f"{run['run_exit_code']}, run wall time {wall_text}")


# --- command line -----------------------------------------------------------


def _count(minimum: int):
    def parse(text: str) -> int:
        if not (text.isascii() and text.isdigit()) or int(text) < minimum:
            raise argparse.ArgumentTypeError(
                f"{text!r} is not an integer of at least {minimum}")
        return int(text)
    return parse


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Execute a Manifest repeatedly with the diagnostics "
                    "runner, each Run in fresh processes and a fresh "
                    "directory under --out, and write summary.json: a "
                    "sil_benchmark_report with every Run report and "
                    "statistics over the successful measured Runs.",
        allow_abbrev=False,
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--repeats", type=_count(1), action=GivenOnce,
                        metavar="N",
                        help=f"measured Runs (default {DEFAULT_REPEATS})")
    parser.add_argument("--warmup", type=_count(0), action=GivenOnce,
                        metavar="N",
                        help="warm-up Runs, kept but never aggregated "
                             f"(default {DEFAULT_WARMUP})")
    parser.add_argument("--out", type=Path, action=GivenOnce, metavar="DIR",
                        required=True,
                        help="the new directory for every Run and the summary")
    parser.add_argument("--no-recording", action="store_true",
                        help="execute every Run without a Recording")
    add_guard_arguments(parser)
    parser.add_argument("--html", type=Path, action=GivenOnce,
                        metavar="PATH",
                        help="also write a standalone HTML report of "
                             "summary.json; it may be inside DIR")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    policy = Policy(
        repeats=DEFAULT_REPEATS if args.repeats is None else args.repeats,
        warmup=DEFAULT_WARMUP if args.warmup is None else args.warmup,
        recording=not args.no_recording, guards=guards_from(args))
    # Every Run has its own cwd, so the Manifest path the user gave is made
    # absolute here; the Manifest itself is never copied or rewritten.
    manifest = Path.cwd() / args.manifest
    out: Path = args.out
    problem = new_path_problem("--out", out)
    if problem is None and args.html is not None:
        problem = _html_problem(args.html, out, policy)
    if problem is not None:
        return command_error(PROG, problem, EXIT_USAGE)
    try:
        tools = find_native_tools()
    except ToolMissing as error:
        return command_error(PROG, str(error), EXIT_NOT_FOUND)
    try:
        out.mkdir()
    except FileExistsError:
        return command_error(PROG, f"--out '{out}' already exists",
                             EXIT_USAGE)
    except OSError as error:
        return command_error(PROG, f"cannot create --out '{out}': {error}",
                             EXIT_FAILURE)
    results, interrupt_signal = run_campaign(policy, manifest, out.absolute(),
                                             tools)
    summary = summarize(policy, manifest, results, interrupt_signal)
    exit_code = summary["status"]["exit_code"]
    problem = _write_summary(summary, out / SUMMARY)
    if problem is not None:
        sys.stderr.write(f"{PROG}: cannot write the benchmark summary "
                         f"'{out / SUMMARY}': {problem}\n")
        if args.html is not None:
            sys.stderr.write(f"{PROG}: cannot write the HTML report "
                             f"'{args.html}': it presents only a written "
                             "summary\n")
        return exit_code or EXIT_FAILURE
    sys.stdout.write(format_summary(summary, out / SUMMARY))
    if args.html is not None:
        problem = write_html(summary_text(summary), args.html)
        if problem is not None:
            sys.stdout.flush()
            sys.stderr.write(f"{PROG}: cannot write the HTML report "
                             f"'{args.html}': {problem}\n")
            return exit_code or EXIT_FAILURE
        sys.stdout.write(f"HTML report: {args.html}\n")
    sys.stdout.flush()
    return exit_code


def _html_problem(html: Path, out: Path, policy: Policy) -> str | None:
    """Why `html` cannot be the new HTML report; it may be inside --out.

    `out` does not exist yet, so a path directly inside it is new.
    """
    target = Path(os.path.realpath(html))
    out_directory = Path(os.path.realpath(out))
    if target == out_directory:
        return f"HTML report '{html}' is the --out directory"
    if target.parent == out_directory:
        if target.name == SUMMARY:
            return f"HTML report '{html}' is also the benchmark summary"
        if target.name in {label for label, _role, _i in plan(policy)}:
            return f"HTML report '{html}' is also a Run directory"
        return None
    return new_path_problem("HTML report", html)


if __name__ == "__main__":
    sys.exit(main())
