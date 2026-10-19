"""`sil profile --collector heap`: one Heaptrack capture of one scope (#317).

docs/profile-report.md is the contract. In short: the command checks the
scope (the runner, or one Process participant without the Clock shim), its
outputs and the host's Heaptrack before anything starts. It writes a launch
adapter that preloads Heaptrack into one process and then execs it, so that
process keeps its PID, pipes, working directory, arguments and environment.
For the runner scope the launcher starts the adapter, which execs the
runner; for a Process participant the diagnostics runner starts that
participant through the adapter (`--launch-wrapper`). The Manifest does not
change. Heaptrack writes to a FIFO; `heaptrack_interpret` reads it while the
Run executes, in its own session outside the Run, so it resolves symbols
while the Run working directory exists. Its output, Heaptrack's interpreted
data file, is the raw evidence. The command then normalizes it and
publishes `profile.json` last.

The Run outcome and the capture outcome are separate, as for the CPU
collector (sil.profile_run).
"""

from __future__ import annotations

import json
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from sil.build_info import __version__ as SIL_VERSION
from sil.diagnostics_report import write_new_file
from sil.diagnostics_run import (
    EXIT_FAILURE, EXIT_NOT_FOUND, EXIT_USAGE, RunRequest, ToolMissing,
    command_error, execute, find_native_tools,
)
from sil.diagnostics_format import format_bytes
from sil.profile_heap import HeapAnalysis, HeapDecodeError, normalize_heap
from sil.profile_modules import ModuleLocator, elf_identity
from sil.profile_report import validate_profile_report
from sil.profile_run import (
    PROG, STDERR_TAIL_BYTES, CaptureRun, HeapRequest, _host, _identity,
    _tool_environment, artifact_rows, resolve_exit, run_record,
)

SHIM_FOLLOW_UP = "https://github.com/Stevie1704/sil/issues/330"
POLL_S = 0.1
CHUNK = 1 << 16
LIMITATIONS = ["heap_tracks_allocation_calls_only",
               "outstanding_is_not_a_leak_verdict",
               "allocation_origin_is_not_ownership",
               "activation_correlation_unavailable", "host_build_only"]
CORRELATION = {"status": "unavailable", "reason": "no_validated_clock_mapping"}
FAILING = {"backend_failed", "no_data", "export_failed"}


@dataclass(frozen=True)
class HeapLayout:
    """The paths of one heap profile directory, absolute."""

    out: Path

    @property
    def heap(self) -> Path:
        return self.out / "heap"

    @property
    def data(self) -> Path:
        return self.heap / "heaptrack.data"

    @property
    def fifo(self) -> Path:
        return self.heap / ".fifo"

    @property
    def adapter(self) -> Path:
        return self.heap / "launch-adapter.sh"

    @property
    def retained(self) -> Path:
        return self.heap / ".run-directory"

    @property
    def allocations(self) -> Path:
        return self.heap / "allocations.json"

    @property
    def interpreter_stderr(self) -> Path:
        return self.heap / "interpret.stderr.txt"

    @property
    def diagnostics(self) -> Path:
        return self.out / "run.diagnostics.json"

    @property
    def profile(self) -> Path:
        return self.out / "profile.json"


# --- scope ------------------------------------------------------------------


def scope_problem(request: HeapRequest, cwd: Path) -> str | None:
    """Why the requested scope cannot be captured, or None."""
    if request.participant is None:
        return None
    name = request.participant
    try:
        manifest = json.loads((cwd / request.manifest).read_text())
        participant = manifest["participants"].get(name)
    except (OSError, ValueError, KeyError, AttributeError, TypeError) as error:
        return (f"cannot read the Manifest '{request.manifest}' to check "
                f"--participant: {error}")
    if not isinstance(participant, dict):
        return f"the Manifest has no participant '{name}'"
    kind = participant.get("type")
    if kind == "native":
        return (f"'{name}' is a Native participant: it runs inside the "
                "runner process, so it cannot be a heap scope; use --runner "
                "and read its allocation sites by module and function")
    if kind != "process":
        return f"'{name}' is a {kind} participant, not a Process participant"
    if participant.get("shim") is True:
        return (f"'{name}' uses the Clock shim: Heaptrack in a shimmed "
                "participant is not qualified (it can stop the participant, "
                "removes the shim from its children and writes timestamps "
                "without bound; proofs/profiling/README.md). Use --runner, "
                f"or see {SHIM_FOLLOW_UP}")
    return None


# --- Heaptrack --------------------------------------------------------------


class HeaptrackUnavailable(RuntimeError):
    """Heaptrack, its preload library or its interpreter is missing."""


@dataclass(frozen=True)
class Heaptrack:
    executable: Path
    preload: Path
    interpreter: Path
    version: str | None


def _relative_paths(script: Path) -> dict[str, str]:
    """LIB_REL_PATH and LIBEXEC_REL_PATH of the `heaptrack` script."""
    try:
        text = script.read_text(errors="replace")
    except OSError:
        return {}
    return dict(re.findall(r'^\s*(LIB_REL_PATH|LIBEXEC_REL_PATH)="?([^"\n]*)"?',
                           text, re.MULTILINE))


def _first(candidates: list[Path]) -> Path | None:
    return next((c for c in candidates if c.is_file()), None)


def find_heaptrack() -> Heaptrack:
    if platform.system() != "Linux":
        raise HeaptrackUnavailable(
            "the heap collector needs Linux: Heaptrack preloads into a "
            "Linux process")
    found = shutil.which("heaptrack")
    if found is None:
        raise HeaptrackUnavailable(
            "cannot find heaptrack on PATH; install Heaptrack from the "
            "distribution (Debian and Ubuntu: heaptrack)")
    executable = Path(os.path.realpath(found))
    base = executable.parent
    relative = _relative_paths(executable)
    libraries = [base / relative["LIB_REL_PATH"]] if "LIB_REL_PATH" in \
        relative else []
    libraries += [base.parent / "lib" / "heaptrack",
                  *sorted(base.parent.glob("lib/*/heaptrack"))]
    libexec = [base / relative["LIBEXEC_REL_PATH"]] if "LIBEXEC_REL_PATH" in \
        relative else []
    libexec += [d / "libexec" for d in libraries] + [base]
    preload = _first([d / "libheaptrack_preload.so" for d in libraries])
    interpreter = _first([d / "heaptrack_interpret" for d in libexec])
    if preload is None or interpreter is None:
        raise HeaptrackUnavailable(
            f"found {executable}, but not its "
            + ("preload library" if preload is None else "interpreter")
            + " (libheaptrack_preload.so, heaptrack_interpret)")
    return Heaptrack(executable, Path(os.path.realpath(preload)),
                     Path(os.path.realpath(interpreter)),
                     _version(executable))


def _version(executable: Path) -> str | None:
    try:
        done = subprocess.run([str(executable), "--version"],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (done.stdout or done.stderr).strip()
    return text.splitlines()[0] if text else None


def probe_heaptrack(tools: Heaptrack) -> None:
    """Preload Heaptrack into `true` and check that it wrote data."""
    true = shutil.which("true") or "/bin/true"
    with tempfile.TemporaryDirectory(prefix="sil-profile-probe-") as probe:
        raw = Path(probe) / "probe.raw"
        env = {**_tool_environment(), "LD_PRELOAD": str(tools.preload),
               "DUMP_HEAPTRACK_OUTPUT": str(raw)}
        try:
            done = subprocess.run([true], env=env, capture_output=True,
                                  text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise HeaptrackUnavailable(f"cannot run the probe: {error}") \
                from error
        if done.returncode == 0 and raw.is_file() and raw.stat().st_size:
            return
    detail = " ".join(done.stderr.split())[-400:]
    raise HeaptrackUnavailable(
        f"Heaptrack's preload library {tools.preload} wrote no data into a "
        f"probe process: {detail or 'no message'}")


def adapter_text(tools: Heaptrack, fifo: Path) -> str:
    return (
        "#!/bin/sh\n"
        "# sil profile --collector heap (#317): preload Heaptrack into this\n"
        "# one process, then exec the command. The process keeps its PID,\n"
        "# pipes, working directory, arguments and other environment.\n"
        f"DUMP_HEAPTRACK_OUTPUT={shlex.quote(str(fifo))}\n"
        "export DUMP_HEAPTRACK_OUTPUT\n"
        f"LD_PRELOAD={shlex.quote(str(tools.preload))}"
        "${LD_PRELOAD:+:$LD_PRELOAD}\n"
        "export LD_PRELOAD\n"
        'exec "$@"\n')


# --- the interpreter --------------------------------------------------------


class _Copy(threading.Thread):
    """Copies a stream into a file up to a limit; it reads on after the
    limit and drops the rest, so the writer never blocks on the reader."""

    def __init__(self, source, sink: Path, limit: int, keep_tail: bool):
        super().__init__(daemon=True)
        self.source, self.sink = source, sink
        self.limit, self.keep_tail = limit, keep_tail
        self.written = 0
        self.limit_reached = False

    def run(self) -> None:
        tail = b""
        with open(self.sink, "wb") as sink:
            for block in iter(lambda: self.source.read1(CHUNK), b""):
                if self.keep_tail:
                    tail = (tail + block)[-self.limit:]
                    continue
                room = self.limit - self.written
                if len(block) > room:
                    self.limit_reached = True
                    block = block[:room]
                if block:
                    sink.write(block)
                    self.written += len(block)
            if self.keep_tail:
                sink.write(tail)


@dataclass(frozen=True)
class InterpreterResult:
    exit_code: int | None  # None: killed after the timeout or a signal
    limit_reached: bool


class Interpreter:
    """`heaptrack_interpret` on the FIFO, in a session of its own."""

    def __init__(self, tools: Heaptrack, layout: HeapLayout, limit: int):
        os.mkfifo(layout.fifo, 0o600)
        self.layout = layout
        # The shell, not this process, blocks in open() until a writer opens
        # the FIFO.
        self.process = subprocess.Popen(
            ["sh", "-c", 'exec "$0" < "$1"', str(tools.interpreter),
             str(layout.fifo)], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True,
            env=_tool_environment())
        self.data = _Copy(self.process.stdout, layout.data, limit, False)
        self.errors = _Copy(self.process.stderr, layout.interpreter_stderr,
                            STDERR_TAIL_BYTES, True)
        self.data.start()
        self.errors.start()

    def finish(self, timeout_s: int, received: list[int]) -> InterpreterResult:
        """Wait for the end of the data, at most `timeout_s`; a signal that
        arrives while it waits stops it."""
        _release_reader(self.layout.fifo)
        signals = len(received)
        waited = 0.0
        killed = False
        while True:
            try:
                self.process.wait(timeout=POLL_S)
                break
            except subprocess.TimeoutExpired:
                waited += POLL_S
            if waited >= timeout_s or len(received) > signals:
                _kill_session(self.process)
                killed = True
        self.data.join()
        self.errors.join()
        self.layout.fifo.unlink(missing_ok=True)
        return InterpreterResult(None if killed else self.process.returncode,
                                 self.data.limit_reached)


def _release_reader(fifo: Path) -> None:
    """A scope process that never started leaves the reader in open(); a
    writer that opens and closes gives it the end of the data."""
    try:
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
    except OSError:
        pass  # the reader is gone or already has a writer


def _kill_session(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        pass
    process.wait()


# --- the Run ----------------------------------------------------------------


def _run_request(request: HeapRequest, layout: HeapLayout) -> RunRequest:
    return RunRequest(request.manifest, layout.diagnostics, request.recording,
                      request.provenance, request.guards)


def run_heap(request: HeapRequest, cwd: Path) -> int:
    problem = scope_problem(request, cwd)
    if problem is not None:
        return command_error(PROG, problem, EXIT_USAGE)
    try:
        native = find_native_tools()
    except ToolMissing as error:
        return command_error(PROG, str(error), EXIT_NOT_FOUND)
    if os.environ.get("LD_PRELOAD"):
        return command_error(
            PROG, "LD_PRELOAD is set: Heaptrack removes it from the "
            "process it traces, so the Run's processes would start with "
            "another environment; unset it for the heap collector",
            EXIT_FAILURE)
    try:
        tools = find_heaptrack()
        probe_heaptrack(tools)
    except HeaptrackUnavailable as error:
        return command_error(PROG, str(error), EXIT_FAILURE)
    layout = HeapLayout(Path(os.path.realpath(cwd / request.out)))
    layout.heap.mkdir(parents=True)
    layout.adapter.write_text(adapter_text(tools, layout.fifo))
    layout.adapter.chmod(0o700)
    interpreter = Interpreter(tools, layout, request.max_capture_bytes)
    try:
        capture_run = _execute(request, layout, native, cwd)
    finally:
        result = _finish_interpreter(interpreter, request)
    report = _publish(request, layout, cwd, tools, capture_run, result)
    if report is not None:
        sys.stdout.write(format_summary(report, request.out / "profile.json"))
        sys.stdout.flush()
    return resolve_exit(report, capture_run)


def _finish_interpreter(interpreter: Interpreter,
                        request: HeapRequest) -> InterpreterResult:
    """Wait for the interpreter with the command's signals held: a signal
    then stops the interpreter, not this command."""
    received: list[int] = []
    previous = {number: signal.signal(number,
                                      lambda n, _f: received.append(n))
                for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        return interpreter.finish(request.interpreter_timeout_s, received)
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _execute(request: HeapRequest, layout: HeapLayout, native,
             cwd: Path) -> CaptureRun:
    adapter = str(layout.adapter)
    if request.participant is None:
        prefix, extra = (adapter,), ()
    else:
        prefix, extra = (), ("--launch-wrapper", request.participant, adapter)
    run = _run_request(request, layout)
    outcome = execute(run, native, cwd=cwd, launch_prefix=prefix,
                      extra_arguments=("--retain-run-directory",
                                       str(layout.retained), *extra))
    status = outcome.exit_code if outcome.exit_code is not None else (
        -outcome.signal if outcome.signal else EXIT_FAILURE)
    command = [str(native.launcher), *prefix, str(native.runner),
               *run.runner_arguments(), "--retain-run-directory",
               str(layout.retained), *extra]
    return CaptureRun(command, status, outcome.launcher_error,
                      outcome.supervisor_signal, False, outcome.report)


# --- normalization and the report -------------------------------------------


def _build_ids(cwd: Path, retained: Path):
    """The build ID of each module file after the Run: at its path, or in
    the retained Run working directory."""
    locator = ModuleLocator(cwd=cwd, retained=retained)

    def build_id(path: str):
        for candidate in (Path(path), locator.retained_copy(path)):
            identity = elf_identity(candidate) if candidate else None
            if identity is not None:
                return (identity.build_id,
                        None if identity.build_id else "no_build_id")
        return None, "module_unavailable"
    return build_id


def _normalize(layout: HeapLayout, cwd: Path):
    """The analysis, or None with a reason."""
    try:
        data = layout.data.read_bytes()
    except OSError:
        return None, "no_data"
    if not data:
        return None, "no_data"
    try:
        return normalize_heap(data, build_id=_build_ids(
            cwd, layout.retained)), None
    except HeapDecodeError as error:
        sys.stderr.write(f"{PROG}: cannot decode '{layout.data}': {error}\n")
        return None, "export_failed"


def _scope_may_have_ended_early(run: dict, process_scope: bool) -> bool:
    """Heaptrack writes its last data at normal process exit. A runner
    that a signal ended, or a Process participant of a Run that did not
    succeed (the runner stops it), may have ended without it."""
    if process_scope:
        return run["status"] != "succeeded"
    return run["status"] == "ended_by_signal"


def capture_status(run: dict, result: InterpreterResult,
                   analysis: HeapAnalysis | None, problem: str | None,
                   supervisor_signal: int | None,
                   process_scope: bool) -> tuple[str, list[str]]:
    if run["status"] == "not_started":
        return "not_started", ["run_not_started"]
    reasons = []
    if result.exit_code != 0:
        reasons.append("backend_failed")
    if problem is not None:
        reasons.append(problem)
    if supervisor_signal is not None:
        reasons.append("interrupted")
    if result.limit_reached:
        reasons.append("capture_limit_reached")
    if analysis is not None and analysis.record["coverage"]["truncated"]:
        reasons.append("data_truncated")
    if _scope_may_have_ended_early(run, process_scope):
        reasons.append("data_may_be_incomplete")
    if FAILING & set(reasons):
        return "failed", reasons
    if "interrupted" in reasons:
        return "interrupted", reasons
    return ("partial" if reasons else "complete"), reasons


def build_report(request: HeapRequest, layout: HeapLayout, cwd: Path,
                 tools: Heaptrack, capture_run: CaptureRun,
                 result: InterpreterResult, analysis: HeapAnalysis | None,
                 problem: str | None) -> dict:
    run = run_record(capture_run)
    status, reasons = capture_status(run, result, analysis, problem,
                                     capture_run.supervisor_signal,
                                     request.participant is not None)
    unavailable = {}
    raw = "heap/heaptrack.data" if layout.data.is_file() and \
        layout.data.stat().st_size else None
    if raw is None:
        unavailable["raw"] = reasons[0] if reasons else "no_data"
    record = None
    if analysis is not None:
        record = {**analysis.record,
                  "allocations_file": "heap/allocations.json"
                  if layout.allocations.is_file() else None}
        if record["allocations_file"] is None:
            record["unavailable"] = {**record["unavailable"],
                                     "allocations_file": "export_failed"}
    else:
        unavailable["analysis"] = problem or (reasons[0] if reasons
                                              else "no_data")
    guards = request.guards
    scope = {"kind": "runner"} if request.participant is None else {
        "kind": "process_participant", "participant": request.participant}
    return {
        "sil_profile_report": 1,
        "producer": {"name": PROG, "sil_version": SIL_VERSION},
        "identity": _identity(request, capture_run.report),
        "host": _host(),
        "configuration": {
            "collector": "heap",
            "recording": None if request.recording is None
            else str(request.recording),
            "participant_timeout_ms": guards.participant_timeout_ms,
            "max_protocol_line_bytes": guards.max_protocol_line_bytes,
            "max_step_output_messages": guards.max_step_output_messages,
            "max_step_inline_payload_bytes":
                guards.max_step_inline_payload_bytes,
            "cwd": str(cwd)},
        "run": run,
        "capture": {
            "kind": "heap", "status": status, "reasons": reasons,
            "backend": {"name": "heaptrack", "version": tools.version,
                        "preload": str(tools.preload),
                        "interpreter": str(tools.interpreter),
                        "interpreter_exit_code": result.exit_code,
                        "unavailable": _backend_unavailable(tools, result)},
            "scope": scope,
            "launch": {"method": "preload_exec_adapter",
                       "adapter": "heap/launch-adapter.sh",
                       "wrapped": scope["kind"]},
            "limits": {"max_capture_bytes": request.max_capture_bytes,
                       "capture_bytes": _size(layout.data),
                       "limit_reached": result.limit_reached,
                       "interpreter_timeout_s":
                           request.interpreter_timeout_s},
            "activation_correlation": dict(CORRELATION),
            "raw": raw, "analysis": record, "unavailable": unavailable,
        },
        "activation_correlation": {"kind": "not_requested"},
        "artifacts": artifact_rows(layout.out, request, cwd, [
            ("heap_launch_adapter", layout.adapter),
            ("heaptrack_data", layout.data),
            ("heap_allocations", layout.allocations)]),
        "limitations": list(LIMITATIONS),
    }


def _backend_unavailable(tools: Heaptrack,
                         result: InterpreterResult) -> dict:
    unavailable = {}
    if tools.version is None:
        unavailable["version"] = "not_reported_by_backend"
    if result.exit_code is None:
        unavailable["interpreter_exit_code"] = "interpreter_stopped"
    return unavailable


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _publish(request: HeapRequest, layout: HeapLayout, cwd: Path,
             tools: Heaptrack, capture_run: CaptureRun,
             result: InterpreterResult) -> dict | None:
    """Normalize and publish; the report, or None if not written."""
    try:
        analysis, problem = _normalize(layout, cwd)
        if analysis is not None:
            write_new_file(layout.allocations, json.dumps(
                analysis.allocations_document, separators=(",", ":")) + "\n")
    except OSError as error:
        sys.stderr.write(f"{PROG}: cannot write '{layout.allocations}': "
                         f"{error}\n")
    finally:
        shutil.rmtree(layout.retained, ignore_errors=True)
    report = build_report(request, layout, cwd, tools, capture_run, result,
                          analysis, problem)
    errors = validate_profile_report(report)
    if errors:
        sys.stderr.write(f"{PROG}: the profile report is not valid, so it "
                         f"is not written: {'; '.join(errors)}\n")
        return None
    try:
        write_new_file(layout.profile,
                       json.dumps(report, indent=2, sort_keys=True) + "\n")
    except OSError as error:
        sys.stderr.write(f"{PROG}: cannot write '{layout.profile}': "
                         f"{error}\n")
        return None
    return report


# --- terminal summary -------------------------------------------------------


def format_summary(report: dict, path: Path) -> str:
    run = report["run"]
    capture = report["capture"]
    state = capture["status"] + (f" ({', '.join(capture['reasons'])})"
                                 if capture["reasons"] else "")
    exit_text = (f"exit code {run['exit_code']}" if run["exit_code"]
                 is not None else f"signal {run['signal']}"
                 if run["signal"] else "no exit code")
    scope = capture["scope"]
    where = ("the runner process (every Native participant)"
             if scope["kind"] == "runner"
             else f"Process participant '{scope['participant']}'")
    lines = [f"profile: {path}", f"run: {run['status']}, {exit_text}",
             f"capture: heap, {state}",
             f"scope: {where}, {capture['backend']['version'] or 'Heaptrack'}",
             "activation correlation: unavailable (Heaptrack's timestamps "
             "have no validated mapping to host time)"]
    analysis = capture["analysis"]
    if analysis is None:
        return "\n".join(lines) + "\n"
    totals = analysis["totals"]
    lines += [
        f"allocations: {totals['allocations']} calls, "
        f"{format_bytes(totals['allocated_bytes'])} allocated; "
        f"{totals['temporary_allocations']} temporary",
        f"at capture end: {totals['outstanding_allocations']} outstanding, "
        f"{format_bytes(totals['outstanding_bytes'])} (not a leak verdict); "
        f"peak tracked heap {format_bytes(totals['peak_tracked_bytes'])} "
        "(not RSS)"]
    coverage = analysis["coverage"]
    unknown = coverage["allocations_unknown_module"] + \
        coverage["allocations_unknown_symbol"]
    if unknown:
        lines.append(f"symbols: {unknown} of {totals['allocations']} "
                     "allocations have no function name")
    modules = {m["id"]: m["path"] for m in analysis["modules"]}
    rows = analysis["hotspots"]["self"][:10]
    if rows:
        lines.append("top allocation sites (innermost frame, bytes "
                     "allocated):")
        for row in rows:
            name = row["symbol"] or "[unknown function]"
            module = Path(modules.get(row["module"]) or "[unknown module]")
            lines.append(f"  {format_bytes(row['allocated_bytes']):>10}  "
                         f"{row['allocations']:>8} calls  {name} "
                         f"({module.name})")
    return "\n".join(lines) + "\n"
