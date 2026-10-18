"""`sil run --diagnostics`: one measured Run and its terminal summary (#302).

`sil run` without a diagnostics option replaces itself with `sil-run`
(sil.cli). With `--diagnostics` or `--diagnostics-out` the Run goes through
this module instead: it checks the output paths, starts
`sil-run-diagnostics` through `sil-diagnostics-launcher`, reads the
`sil_run_diagnostics` report and prints a summary of it on stdout after the
Run.

The module is also the seam that `sil benchmark` and the HTML output reuse:
`preflight` checks new-file destinations, `execute` runs one diagnostics Run
and returns a `RunOutcome`, and `resolve_exit` turns an outcome plus any
failed requested artifacts into the command's exit code.

Exit codes: the Run's own exit code when it failed; 2 for invalid usage or a
rejected output path; 127 when a native tool is missing; 128+N when the
runner ended by signal N; 1 when the Run succeeded but a requested artifact
(the report, or a later one) could not be produced, or the launcher failed.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterable, Sequence

from sil.diagnostics_report import PHASES, ReportError, load_report

PROG = "sil run"
RUNNER = "sil-run-diagnostics"
LAUNCHER = "sil-diagnostics-launcher"
LAUNCHER_PREFIX = b"sil-diagnostics-launcher: "
DEFAULT_RECORDING = "out.mcap"

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 127

FORWARDED_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
# Time the stderr copy may take after the runner ended. A descendant that
# escaped the runner's cleanup can hold the pipe open forever.
STDERR_DRAIN_S = 2.0

# The runner's ceilings: std::chrono::milliseconds::max() and SIZE_MAX.
MAX_MILLISECONDS = 2**63 - 1
MAX_SIZE = 2**64 - 1


@dataclass(frozen=True)
class Guards:
    """The timeout and Step-protocol guards a Run forwards; None = default."""

    participant_timeout_ms: int | None = None
    max_protocol_line_bytes: int | None = None
    max_step_output_messages: int | None = None
    max_step_inline_payload_bytes: int | None = None

    def arguments(self) -> list[str]:
        result: list[str] = []
        for name, value in vars(self).items():
            if value is not None:
                result += ["--" + name.replace("_", "-"), str(value)]
        return result


@dataclass(frozen=True)
class RunRequest:
    """One diagnostics Run. Relative paths resolve against the Run's cwd."""

    manifest: Path
    report: Path
    recording: Path | None = Path(DEFAULT_RECORDING)  # None: --no-recording
    provenance: Path | None = None  # None: beside the Recording path
    guards: Guards = field(default_factory=Guards)

    @property
    def recording_path(self) -> Path:
        """The path the runner derives side-cars from, also without one."""
        return self.recording or Path(DEFAULT_RECORDING)

    def runner_arguments(self) -> list[str]:
        result = [str(self.manifest)]
        result += (["--no-recording"] if self.recording is None
                   else ["-o", str(self.recording)])
        if self.provenance is not None:
            result += ["--provenance", str(self.provenance)]
        result += self.guards.arguments()
        return result + ["--diagnostics-out", str(self.report)]


def default_report_path(recording: Path | None) -> Path:
    """`<recording>.diagnostics.json`; `out.mcap.diagnostics.json` without."""
    return Path(f"{recording or DEFAULT_RECORDING}.diagnostics.json")


@dataclass(frozen=True)
class NativeTools:
    runner: Path
    launcher: Path


class ToolMissing(RuntimeError):
    """A native diagnostics tool is not on PATH."""


def find_native_tools() -> NativeTools:
    """Locate the installed diagnostics runner and launcher on PATH."""
    found = {name: shutil.which(name) for name in (RUNNER, LAUNCHER)}
    missing = [name for name, path in found.items() if path is None]
    if missing:
        raise ToolMissing(
            f"cannot find {' and '.join(missing)} on PATH; diagnostics need "
            f"the native SiL installation that contains {RUNNER}")
    return NativeTools(Path(found[RUNNER]).absolute(),
                       Path(found[LAUNCHER]).absolute())


def preflight(request: RunRequest,
              new_files: Sequence[tuple[str, Path]] = (),
              cwd: Path | None = None) -> list[str]:
    """Problems with the Run's destinations, found before anything starts.

    `new_files` are further (label, path) destinations that must be new, such
    as an HTML report. The diagnostics report is always one of them.
    """
    base = cwd or Path.cwd()

    def absolute(path: Path) -> Path:
        return Path(os.path.realpath(base / path))

    outputs = [("diagnostics report", request.report), *new_files]
    recording = request.recording_path
    provenance = request.provenance or Path(f"{recording}.provenance.json")
    others = [("Manifest", request.manifest),
              ("provenance record", provenance),
              ("failure report", Path(f"{recording}.failure.json"))]
    if request.recording is not None:
        others.append(("Recording", request.recording))
    problems: list[str] = []
    if request.recording is not None and _same(
            absolute(request.recording), absolute(request.manifest)):
        problems.append(f"the Recording '{request.recording}' is the Manifest")
    seen: list[tuple[str, Path]] = []
    for label, path in outputs:
        target = absolute(path)
        if os.path.lexists(base / path):
            problems.append(f"{label} '{path}' already exists; diagnostics "
                            "never replace an earlier file")
            continue
        if not target.parent.is_dir():
            problems.append(f"{label} '{path}': directory "
                            f"'{target.parent}' does not exist")
            continue
        for other_label, other in [*others, *seen]:
            if _same(target, absolute(other)):
                problems.append(f"{label} '{path}' is also the {other_label}")
                break
        seen.append((label, path))
    return problems


def _same(a: Path, b: Path) -> bool:
    if a == b:
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


@dataclass(frozen=True)
class RunOutcome:
    """What one diagnostics Run left: process result and report, if any."""

    exit_code: int | None  # the process exit code; None if ended by signal
    signal: int | None  # the signal that ended the runner
    launcher_error: str | None  # the launcher's own failure line, if any
    report: dict | None  # the valid report, or None
    report_problem: str | None  # why `report` is None
    report_path: Path
    supervisor_signal: int | None = None  # first signal this process got

    @property
    def run_exit_code(self) -> int | None:
        """The Run's own exit code, when a Run took place and ended."""
        if self.report is not None:
            return self.report["run"]["exit_code"]
        if self.launcher_error is not None or self.signal is not None:
            return None
        return self.exit_code


def execute(request: RunRequest, tools: NativeTools, *,
            cwd: Path | None = None,
            stdout: BinaryIO | int | None = None,
            stderr: BinaryIO | None = None) -> RunOutcome:
    """Run one diagnostics Run to its end and read its report.

    The runner's stdout goes to `stdout` (inherited when None). Its stderr is
    copied unchanged to `stderr` (this process's stderr when None) and read
    for the launcher's own failure line. SIGINT, SIGTERM and SIGHUP sent to
    this process are forwarded to the launcher, which forwards them to the
    runner; the Run then ends through its own cleanup.
    """
    sink = stderr if stderr is not None else sys.stderr.buffer
    command = [str(tools.launcher), str(tools.runner),
               *request.runner_arguments()]
    sys.stdout.flush()
    sys.stderr.flush()
    process = subprocess.Popen(command, cwd=cwd, stdout=stdout,
                               stderr=subprocess.PIPE, start_new_session=True)
    received: list[int] = []
    previous = _forward_signals(process, received)
    try:
        copier = _StderrCopy(process.stderr, sink)
        copier.start()
        status = _wait(process)
        copier.join(STDERR_DRAIN_S)
    finally:
        _restore_signals(previous)
    exit_code = status if status >= 0 else None
    ended_by = -status if status < 0 else None
    launcher_error = copier.launcher_error
    report, problem = _read_report(Path(cwd or ".") / request.report,
                                   exit_code)
    return RunOutcome(exit_code, ended_by, launcher_error, report, problem,
                      request.report,
                      received[0] if received else None)


def _wait(process: subprocess.Popen) -> int:
    while True:
        try:
            return process.wait()
        except KeyboardInterrupt:  # only if no handler could be installed
            process.send_signal(signal.SIGINT)


def _forward_signals(process: subprocess.Popen, received: list[int]):
    if threading.current_thread() is not threading.main_thread():
        return {}

    def forward(number, _frame):
        received.append(number)
        try:
            process.send_signal(number)
        except OSError:
            pass

    return {number: signal.signal(number, forward)
            for number in FORWARDED_SIGNALS}


def _restore_signals(previous) -> None:
    for number, handler in previous.items():
        signal.signal(number, handler)


class _StderrCopy(threading.Thread):
    """Copies the runner's stderr unchanged and notes the launcher's line."""

    def __init__(self, source: BinaryIO, sink: BinaryIO):
        super().__init__(daemon=True)
        self.source = source
        self.sink = sink
        self.launcher_error: str | None = None

    def run(self) -> None:
        for line in iter(self.source.readline, b""):
            if line.startswith(LAUNCHER_PREFIX) and self.launcher_error is None:
                self.launcher_error = line.decode(errors="replace").rstrip("\n")
            self.sink.write(line)
            self.sink.flush()


def _read_report(path: Path, exit_code: int | None):
    if not os.path.lexists(path):
        return None, "no diagnostics report was written"
    try:
        report = load_report(path)
    except ReportError as error:
        return None, f"the diagnostics report is not valid: {error}"
    if report["run"]["exit_code"] != exit_code:
        return None, (f"the diagnostics report names exit code "
                      f"{report['run']['exit_code']}, the runner exited "
                      f"{exit_code}")
    return report, None


def resolve_exit(outcome: RunOutcome,
                 failed_artifacts: Iterable[str] = ()) -> int:
    """The command's exit code: a Run failure is never replaced."""
    if outcome.launcher_error is not None:
        return EXIT_NOT_FOUND if outcome.exit_code == EXIT_NOT_FOUND \
            else EXIT_FAILURE
    if outcome.signal is not None:
        return 128 + outcome.signal
    if outcome.exit_code != EXIT_OK:
        return outcome.exit_code
    if outcome.report is None or list(failed_artifacts):
        return EXIT_FAILURE
    return EXIT_OK


def describe_problems(outcome: RunOutcome,
                      failed_artifacts: Iterable[str] = ()) -> list[str]:
    """The lines that explain an outcome which is not a complete success."""
    lines: list[str] = []
    if outcome.launcher_error is not None:
        lines.append(f"{LAUNCHER} failed, so no Run took place: "
                     f"{outcome.launcher_error.removeprefix(LAUNCHER + ': ')}")
        return lines
    if outcome.signal is not None:
        try:
            name = signal.Signals(outcome.signal).name
        except ValueError:
            name = str(outcome.signal)
        lines.append(f"the diagnostics runner ended by signal {name} "
                     f"({outcome.signal}); no Run exit code exists")
    if outcome.report is None:
        lines.append(f"{outcome.report_problem} ('{outcome.report_path}'); "
                     "no metrics are available for this Run")
    lines += [f"cannot write the {artifact}" for artifact in failed_artifacts]
    return lines


# --- terminal summary -------------------------------------------------------


def format_duration_ns(value: float) -> str:
    """Nanoseconds in the largest unit below the value, three decimals."""
    for unit, scale in (("s", 1e9), ("ms", 1e6), ("us", 1e3)):
        if abs(value) >= scale:
            return f"{value / scale:.3f} {unit}"
    return f"{value:.0f} ns"


def format_bytes(value: int) -> str:
    for unit, scale in (("GiB", 2**30), ("MiB", 2**20), ("KiB", 2**10)):
        if value >= scale:
            return f"{value / scale:.3f} {unit}"
    return f"{value} B"


def _value(record: dict, key: str, render) -> str:
    """A value with its unit, or `unavailable (reason)` for a null one."""
    if record.get(key) is None:
        reason = record.get("unavailable", {}).get(key, "not reported")
        return f"unavailable ({reason})"
    return render(record[key])


def format_factor(value: float) -> str:
    return f"{value:.2f}x"


def format_rate(value: float) -> str:
    return f"{value:.2f}/s"


def format_value(unit: str, value) -> str:
    """A value of a benchmark metric unit: `ns`, `bytes`, `factor`, `1/s`."""
    if unit == "ns":
        return format_duration_ns(value)
    if unit == "bytes":
        return format_bytes(value)
    if unit == "factor":
        return format_factor(value)
    return format_rate(value)


def format_summary(report: dict, report_path: Path) -> str:
    """A concise text summary of one Run report, with units and scopes."""
    lines = [f"diagnostics report: {report_path}", *_run_lines(report)]
    observational = report["observational"]
    wall = observational["run_wall_time"]
    lines.append(f"run wall time: {_value(wall, 'wall_ns', format_duration_ns)}"
                 f" ({wall['boundary']})")
    phases = observational["phases"]
    lines.append("phases: " + ", ".join(
        f"{name.removesuffix('_ns').replace('_', ' ')} "
        f"{_value(phases, name, format_duration_ns)}"
        for name in (*PHASES, "unattributed_ns")))
    speed = observational["virtual_time_speed"]
    lines.append(f"virtual-time speed: scheduled "
                 f"{_value(speed, 'scheduled_factor', format_factor)}, end-to-end "
                 f"{_value(speed, 'end_to_end_factor', format_factor)}")
    lines.append(_throughput_line(observational["throughput"]))
    lines += _task_lines(report)
    lines += _resource_lines(observational["resources"])
    for error in report["collection_errors"]:
        lines.append(f"collection error: {error['scope']}: {error['message']}")
    return "\n".join(lines) + "\n"


def _run_lines(report: dict) -> list[str]:
    run = report["run"]
    completeness = report["completeness"]
    state = ("complete" if completeness["run_complete"] else
             "partial (" + ", ".join(completeness["reasons"]) + ")")
    lines = [f"run: {run['status']}, exit code {run['exit_code']}, {state}"]
    failure = run["failure"]
    if failure is not None:
        context = [failure["category"]]
        if failure.get("participant"):
            context.append(f"Participant {failure['participant']}")
        if failure.get("virtual_time_ns") is not None:
            context.append("virtual time "
                           + format_duration_ns(failure["virtual_time_ns"]))
        lines.append(f"failure: {failure['message']} ({', '.join(context)})")
    if not completeness["run_complete"]:
        execution = report["execution"]
        lines.append(
            "last virtual time: "
            f"{_value(execution, 'last_virtual_time_ns', format_duration_ns)}"
            f" of Duration "
            f"{_value(execution, 'duration_ns', format_duration_ns)}")
    return lines


def _throughput_line(throughput: dict) -> str:
    denominator = _value(throughput, "scheduled_execution_ns",
                         format_duration_ns)
    return (f"throughput per second of scheduled execution ({denominator}): "
            f"{throughput['completed_native_activations']} native "
            f"activations, "
            f"{_value(throughput, 'native_activations_per_s', format_rate)}; "
            f"{throughput['completed_process_steps']} process steps, "
            f"{_value(throughput, 'process_steps_per_s', format_rate)}")


def _task_lines(report: dict) -> list[str]:
    timings = {(row["participant"], row["task"]): row["timing"]
               for row in report["observational"]["tasks"]}
    replays = {row["participant"]: row["timing"]
               for row in report["observational"]["replay"]}
    lines: list[str] = []
    for participant in report["execution"]["participants"]:
        name = participant["participant"]
        for task in participant["tasks"]:
            lines.append(
                f"  {name}/{task['task']} {task['kind']}: "
                + _counts(task) + _timing(timings[(name, task["task"])]))
        if participant["replay"] is not None:
            replay = participant["replay"]
            lines.append(
                f"  {name} replay: {replay['publications']} Messages; "
                "publication Slots " + _counts(replay)
                + _timing(replays[name]))
    return ["tasks (inclusive wall time of completed activations):", *lines] \
        if lines else []


def _counts(counts: dict) -> str:
    text = f"{counts['completed']} of {counts['attempted']} completed"
    for outcome in ("failed", "interrupted"):
        if counts[outcome]:
            text += f", {counts[outcome]} {outcome}"
    first = counts.get("failed_activation")
    if first is not None:
        text += (f" (first at activation {first['activation_index']}, "
                 f"virtual time {format_duration_ns(first['virtual_time_ns'])})")
    return text


def _timing(timing: dict) -> str:
    if timing["completed"] == 0:
        return (", timing unavailable ("
                + timing["unavailable"].get("mean_ns", "not reported") + ")")
    parts = [f"{label} {_value(timing, key, format_duration_ns)}"
             for label, key in (("mean", "mean_ns"), ("p50", "p50_ns"),
                                ("p95", "p95_ns"), ("p99", "p99_ns"),
                                ("max", "max_ns"))]
    quantiles = timing["quantiles"]
    if quantiles["approximate"]:
        parts.append(f"percentiles approximate from "
                     f"{quantiles['retained_count']} of "
                     f"{quantiles['sample_count']} samples")
    return ", " + ", ".join(parts)


def _resource_lines(resources: dict) -> list[str]:
    lines = ["resources (peak RSS is per process and never summed):"]
    lines.append("  runner: " + _resource(
        resources["runner"], "Native participants run in the runner"))
    for record in resources["process_participants"]:
        lines.append(f"  process {record['participant']}: " + _resource(
            record, "includes waited descendants"
            if record["includes_waited_descendants"] else None))
    total = resources["cpu_total"]
    lines.append("  CPU total of runner and Process participants: user "
                 f"{_value(total, 'user_cpu_ns', format_duration_ns)}, system "
                 f"{_value(total, 'system_cpu_ns', format_duration_ns)}")
    limited = [(name, record["limitations"]) for name, record in
               [("runner", resources["runner"]),
                *((r["participant"], r)
                  for r in resources["process_participants"])]
               if record["limitations"]]
    if limited:
        lines.append("  limitations: " + "; ".join(
            f"{name} {', '.join(codes)}" for name, codes in limited))
    return lines


def _resource(record: dict, scope_note: str | None) -> str:
    text = (f"user CPU {_value(record, 'user_cpu_ns', format_duration_ns)}, "
            f"system CPU "
            f"{_value(record, 'system_cpu_ns', format_duration_ns)}, "
            f"peak RSS {_value(record, 'peak_rss_bytes', format_bytes)}")
    if record["peak_rss_kind"] is not None:
        notes = [record["peak_rss_kind"]] + ([scope_note] if scope_note else [])
        text += f" ({'; '.join(notes)})"
    return text


# --- command line -----------------------------------------------------------


def _positive(ceiling: int):
    def parse(text: str) -> int:
        if not (text.isascii() and text.isdigit()) or not (
                0 < int(text) <= ceiling):
            raise argparse.ArgumentTypeError(
                f"{text!r} is not a positive integer of at most {ceiling}")
        return int(text)
    return parse


class GivenOnce(argparse.Action):
    """The runner rejects a repeated option; so does this command."""

    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest) is not None:
            parser.error(f"{option_string} may be given only once")
        setattr(namespace, self.dest, values)


def add_guard_arguments(parser: argparse.ArgumentParser) -> None:
    """The timeout and Step-protocol guard options a Run forwards."""
    parser.add_argument("--participant-timeout-ms", action=GivenOnce,
                        type=_positive(MAX_MILLISECONDS), metavar="N")
    for name in ("max-protocol-line-bytes", "max-step-output-messages",
                 "max-step-inline-payload-bytes"):
        parser.add_argument(f"--{name}", action=GivenOnce,
                            type=_positive(MAX_SIZE), metavar="N")


def guards_from(args: argparse.Namespace) -> Guards:
    return Guards(args.participant_timeout_ms, args.max_protocol_line_bytes,
                  args.max_step_output_messages,
                  args.max_step_inline_payload_bytes)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Execute a Manifest with the diagnostics runner, write "
                    "its sil_run_diagnostics report and print a summary. "
                    "Without --diagnostics or --diagnostics-out, 'sil run' "
                    "is the ordinary sil-run.",
        allow_abbrev=False,
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("-o", dest="recording", type=Path, action=GivenOnce,
                        metavar="OUT.mcap",
                        help=f"the Recording (default {DEFAULT_RECORDING})")
    parser.add_argument("--no-recording", action="store_true",
                        help="execute without writing a Recording")
    parser.add_argument("--provenance", type=Path, action=GivenOnce,
                        metavar="PATH", help="the provenance record")
    add_guard_arguments(parser)
    parser.add_argument("--diagnostics", action="store_true",
                        help="write <recording>.diagnostics.json (also "
                             f"{DEFAULT_RECORDING}.diagnostics.json under "
                             "--no-recording) and print a summary")
    parser.add_argument("--diagnostics-out", type=Path, action=GivenOnce,
                        metavar="PATH",
                        help="write the report to PATH; implies --diagnostics")
    parser.add_argument("--html", type=Path, action=GivenOnce,
                        metavar="PATH",
                        help="not available in this version")
    return parser


def request_from_arguments(argv: list[str]) -> RunRequest:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.html is not None:
        parser.error("--html is not available in this version: the HTML "
                     "renderer is not installed yet; use --diagnostics and "
                     "read the JSON report")
    if args.no_recording and args.recording is not None:
        parser.error("-o and --no-recording exclude each other")
    recording = None if args.no_recording else (
        args.recording or Path(DEFAULT_RECORDING))
    report = args.diagnostics_out or default_report_path(recording)
    return RunRequest(args.manifest, report, recording, args.provenance,
                      guards_from(args))


def main(argv: list[str] | None = None) -> int:
    request = request_from_arguments(sys.argv[1:] if argv is None else argv)
    problems = preflight(request)
    if problems:
        for problem in problems:
            sys.stderr.write(f"{PROG}: error: {problem}\n")
        return EXIT_USAGE
    try:
        tools = find_native_tools()
    except ToolMissing as error:
        sys.stderr.write(f"{PROG}: error: {error}\n")
        return EXIT_NOT_FOUND
    outcome = execute(request, tools)
    if outcome.report is not None:
        sys.stdout.write(format_summary(outcome.report, request.report))
        sys.stdout.flush()
    for line in describe_problems(outcome):
        sys.stderr.write(f"{PROG}: {line}\n")
    return resolve_exit(outcome)


if __name__ == "__main__":
    sys.exit(main())
