"""`sil profile`: one CPU profile of one fresh Run (#315).

The heap collector (#317) is sil.profile_heap_run; this module parses the
command line of both and runs the CPU collector.

docs/profile-report.md is the contract: the command, its exit codes, the
profile directory and `sil_profile_report` v1. In short: the command checks
its outputs and the host's perf before anything starts, runs
`perf record ... -- sil-diagnostics-launcher ... sil-run-diagnostics ...` in
a new session, keeps `perf.data` within its limit by disabling sampling (the
Run continues), captures the modules from the Run working directory that the
runner retained, exports and normalizes the samples, and publishes
`profile.json` last.

The Run outcome and the capture outcome are separate. A capture problem never
replaces a nonzero Run exit code; after a successful Run it gives exit 1.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path

from sil.build_info import __version__ as SIL_VERSION
from sil.diagnostics_report import ReportError, load_report, write_new_file
from sil.diagnostics_run import (
    EXIT_FAILURE, EXIT_NOT_FOUND, EXIT_USAGE, FORWARDED_SIGNALS,
    STDERR_DRAIN_S, GivenOnce, ToolMissing,
    _read_status, _StderrCopy, add_guard_arguments, command_error,
    find_native_tools, guards_from,
)
from sil.profile_activation import correlate
from sil.profile_common import (
    PROG, POLL_S, STDERR_TAIL_BYTES, SYMBOLIZATION_TIMEOUT_S, CaptureRun,
    RunTarget, artifact_rows, capture_outcome, file_size, host, identity,
    perf_event_paranoid, report_launcher_error, resolve_exit, run_record,
    preflight, tool_environment,
)
from sil.profile_cpu import CpuAnalysis, ModuleFacts, normalize_cpu
from sil.profile_heap_run import HEAP_MAX_CAPTURE_BYTES, HeapRequest
from sil.profile_modules import ModuleLocator
from sil.profile_report import HOTSPOT_LIMIT, validate_profile_report
from sil.profile_storage import reserve_reached, space_warning
from sil.profile_summary import format_summary

EVENT = "cpu-clock:u"
FREQUENCY_HZ = 999
STACK_BYTES = 8192
MAX_CAPTURE_BYTES = 1 << 30
MAX_ACTIVATION_BYTES = 64 << 20
LIMITATIONS = ["cpu_samples_estimate_on_cpu_time", "user_space_only",
               "scope_is_runner_and_descendants", "host_build_only"]
SCRIPT_FIELDS = ["-F", "comm,pid,tid,time,period,ip,sym,dso", "--ns",
                 "--inline", "--show-task-events", "--show-mmap-events",
                 "--show-lost-events"]
# The leaf line pass prints no inlined frame, so it stays one pair of lines
# per sample.
SRCLINE_FIELDS = ["-G", "-F", "ip,sym,srcline", "--no-inline"]
PERMISSION_HINT = (
    "an unprivileged user needs kernel.perf_event_paranoid 2 or less "
    "(now {paranoid}); a container needs CAP_PERFMON (docker run "
    "--cap-add PERFMON). sil profile does not change host settings; see "
    "docs/profile-report.md")


@dataclass(frozen=True)
class CpuRequest:
    """`--collector cpu`: one perf capture of the runner and all its
    descendants."""

    target: RunTarget
    frequency_hz: int = FREQUENCY_HZ
    stack_bytes: int = STACK_BYTES
    max_capture_bytes: int = MAX_CAPTURE_BYTES
    symbolization_timeout_s: int = SYMBOLIZATION_TIMEOUT_S
    modules_from: tuple[Path, ...] = ()
    activations: bool = False
    max_activation_bytes: int = MAX_ACTIVATION_BYTES

    def problems(self, cwd: Path) -> list[str]:
        """The usage problems of the CPU options."""
        return [f"--modules-from '{source}' does not exist"
                for source in self.modules_from
                if not (cwd / source).exists()]

    def capture(self, cwd: Path) -> int:
        return run_cpu(self, cwd)


@dataclass(frozen=True)
class Layout:
    """The paths of one profile directory, absolute."""

    out: Path

    @property
    def cpu(self) -> Path:
        return self.out / "cpu"

    @property
    def perf_data(self) -> Path:
        return self.cpu / "perf.data"

    @property
    def control(self) -> Path:
        return self.cpu / ".control"

    @property
    def retained(self) -> Path:
        return self.cpu / ".run-directory"

    @property
    def unpacked(self) -> Path:
        return self.cpu / ".modules-from"

    @property
    def cache(self) -> Path:
        return self.cpu / "buildid-cache"

    @property
    def samples(self) -> Path:
        return self.cpu / "samples.json"

    @property
    def activation(self) -> Path:
        return self.out / "activation"

    @property
    def observations(self) -> Path:
        return self.activation / "observations.jsonl"

    @property
    def correlation(self) -> Path:
        return self.activation / "correlation.json"

    @property
    def diagnostics(self) -> Path:
        return self.out / "run.diagnostics.json"

    @property
    def profile(self) -> Path:
        return self.out / "profile.json"


# --- command line -----------------------------------------------------------


def _positive(text: str) -> int:
    if not (text.isascii() and text.isdigit()) or int(text) == 0:
        raise argparse.ArgumentTypeError(f"{text!r} is not a positive integer")
    return int(text)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG, allow_abbrev=False,
        description="Capture one CPU profile (Linux perf) or one heap "
                    "profile (Heaptrack) of one fresh Run into a new "
                    "directory (docs/profile-report.md). "
                    "'sil profile render DIR/profile.json [--html FILE]' "
                    "presents a saved profile offline.")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--out", type=Path, required=True, action=GivenOnce,
                        metavar="DIR", help="the new profile directory")
    parser.add_argument("--collector", required=True, action=GivenOnce,
                        help="cpu or heap")
    parser.add_argument("--participant", action=GivenOnce, metavar="NAME",
                        help="heap scope: one Process participant that "
                             "does not use the Clock shim")
    parser.add_argument("--runner", action="store_true",
                        help="heap scope: the runner process, with every "
                             "Native participant")
    parser.add_argument("-o", dest="recording", type=Path, action=GivenOnce,
                        metavar="OUT.mcap",
                        help="the Recording (default DIR/run.mcap)")
    parser.add_argument("--no-recording", action="store_true")
    parser.add_argument("--provenance", type=Path, action=GivenOnce,
                        metavar="PATH")
    add_guard_arguments(parser)
    parser.add_argument("--frequency-hz", type=_positive, action=GivenOnce,
                        metavar="N", help=f"default {FREQUENCY_HZ}")
    parser.add_argument("--stack-bytes", type=_positive, action=GivenOnce,
                        metavar="N", help=f"default {STACK_BYTES}")
    parser.add_argument("--max-capture-bytes", type=_positive,
                        action=GivenOnce, metavar="N",
                        help=f"default {MAX_CAPTURE_BYTES} (cpu), "
                             f"{HEAP_MAX_CAPTURE_BYTES} (heap)")
    parser.add_argument("--symbolization-timeout-s", type=_positive,
                        action=GivenOnce, metavar="N",
                        help=f"default {SYMBOLIZATION_TIMEOUT_S}")
    parser.add_argument("--modules-from", type=Path, action="append",
                        default=[], metavar="PATH",
                        help="a directory, ELF file or FMU/zip archive with "
                             "copies of modules that are gone after the Run "
                             "(repeatable)")
    parser.add_argument("--activations", action="store_true",
                        help="also record Activation intervals and join "
                             "them with the samples")
    parser.add_argument("--max-activation-bytes", type=_positive,
                        action=GivenOnce, metavar="N",
                        help=f"default {MAX_ACTIVATION_BYTES}")
    return parser


CPU_ONLY = ("frequency_hz", "stack_bytes", "modules_from",
            "max_activation_bytes")


def request_from_arguments(argv: list[str]) -> CpuRequest | HeapRequest:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.collector == "heap":
        return _heap_request(parser, args)
    if args.collector != "cpu":
        parser.error(f"unknown collector {args.collector!r}; use cpu or heap")
    if args.runner:
        parser.error("--runner selects a heap scope; the cpu collector "
                     "samples the runner and all its descendants")
    if args.participant is not None:
        parser.error("--participant selects a heap scope; the cpu collector "
                     "samples the runner and all its descendants")
    if args.max_activation_bytes is not None and not args.activations:
        parser.error("--max-activation-bytes needs --activations")
    try:
        recording, provenance = _outputs(args)
    except ValueError as error:
        parser.error(str(error))
    return CpuRequest(
        _target(args, recording, provenance),
        args.frequency_hz or FREQUENCY_HZ, args.stack_bytes or STACK_BYTES,
        args.max_capture_bytes or MAX_CAPTURE_BYTES,
        args.symbolization_timeout_s or SYMBOLIZATION_TIMEOUT_S,
        tuple(args.modules_from), args.activations,
        args.max_activation_bytes or MAX_ACTIVATION_BYTES)


def _target(args, recording: Path | None,
            provenance: Path | None) -> RunTarget:
    return RunTarget(args.manifest, args.out, recording, provenance,
                     guards_from(args))


def _outputs(args) -> tuple[Path | None, Path | None]:
    if args.no_recording and args.recording is not None:
        raise ValueError("-o and --no-recording exclude each other")
    recording = None if args.no_recording else (
        args.recording or args.out / "run.mcap")
    provenance = args.provenance
    if provenance is None and args.no_recording:
        provenance = args.out / "run.provenance.json"
    return recording, provenance


def _heap_request(parser: argparse.ArgumentParser, args) -> HeapRequest:
    if args.activations:
        parser.error("Activation correlation is unavailable for the heap "
                     "collector: Heaptrack's timestamps have no validated "
                     "mapping to host time (docs/profile-report.md)")
    for name in CPU_ONLY:
        if getattr(args, name):
            option = "--" + name.replace("_", "-")
            parser.error(f"{option} is an option of the cpu collector")
    if args.runner and args.participant is not None:
        parser.error("--runner and --participant exclude each other")
    if not args.runner and args.participant is None:
        parser.error("--collector heap needs one scope: --runner or "
                     "--participant NAME")
    try:
        recording, provenance = _outputs(args)
    except ValueError as error:
        parser.error(str(error))
    return HeapRequest(
        _target(args, recording, provenance), args.participant,
        args.max_capture_bytes or HEAP_MAX_CAPTURE_BYTES,
        args.symbolization_timeout_s or SYMBOLIZATION_TIMEOUT_S)


# --- perf -------------------------------------------------------------------


class PerfUnavailable(RuntimeError):
    """perf, its event or the permission to use it is missing."""


def _sampling_options(request: CpuRequest) -> list[str]:
    return ["-e", EVENT, "-F", str(request.frequency_hz), "-k", "monotonic",
            "--call-graph", f"dwarf,{request.stack_bytes}", "--buildid-mmap",
            "-N", "-q"]


def find_perf() -> Path:
    found = shutil.which("perf")
    if found is None:
        raise PerfUnavailable(
            "cannot find perf on PATH; install the perf of the running "
            "kernel (Debian: linux-perf, Ubuntu: linux-tools-$(uname -r))")
    return Path(found)


def probe_perf(perf: Path, request: CpuRequest) -> None:
    """Record `true` with the exact event and options, or raise."""
    true = shutil.which("true") or "/bin/true"
    with tempfile.TemporaryDirectory(prefix="sil-profile-probe-") as probe:
        data = Path(probe) / "probe.data"
        try:
            done = subprocess.run(
                [str(perf), "record", *_sampling_options(request), "-o",
                 str(data), "--", true], capture_output=True, text=True,
                timeout=60, env=tool_environment())
        except (OSError, subprocess.TimeoutExpired) as error:
            # A perf with the file capability CAP_PERFMON cannot start in a
            # container without it: exec gives EPERM.
            raise PerfUnavailable(f"cannot run perf: {error}; "
                                  + _permission_hint()) from error
        if done.returncode == 0 and data.is_file():
            return
    detail = " ".join(done.stderr.split())[-400:]
    raise PerfUnavailable(
        f"perf cannot sample {EVENT} with DWARF call graphs: {detail}; "
        + _permission_hint())


def _permission_hint() -> str:
    paranoid = perf_event_paranoid()
    return PERMISSION_HINT.format(
        paranoid="unknown" if paranoid is None else paranoid)


def perf_version(perf: Path) -> str:
    done = subprocess.run([str(perf), "--version"], capture_output=True,
                          text=True, env=tool_environment())
    return done.stdout.strip() or "unknown"


def _analysis(perf: Path, cache: Path, *arguments: str) -> list[str]:
    """A perf analysis command on the profile's own build-ID cache. perf
    reads the cache location from --buildid-dir only; it sets
    PERF_BUILDID_DIR for its children but does not read it."""
    return [str(perf), "--buildid-dir", str(cache), *arguments]


# --- the Run ----------------------------------------------------------------


def record_command(perf: Path, request: CpuRequest, layout: Layout,
                   status_fd: int, environment_fd: int) -> list[str]:
    tools = find_native_tools()
    run = request.target.run_request(layout.diagnostics)
    return [str(perf), "record", *_sampling_options(request), "--control",
            f"fifo:{layout.control}", "-o", str(layout.perf_data), "--",
            str(tools.launcher), "--status-fd", str(status_fd),
            "--environment-fd", str(environment_fd),
            str(tools.runner), *run.runner_arguments(),
            "--retain-run-directory", str(layout.retained),
            *_activation_arguments(request, layout)]


def _activation_arguments(request: CpuRequest, layout: Layout) -> list:
    if not request.activations:
        return []
    return ["--activation-observations", str(layout.observations),
            "--activation-observations-limit-bytes",
            str(request.max_activation_bytes)]


class _SessionForwarder:
    """Forwards termination signals to the Run's whole session.

    The runner records the first one and cleans up as without a profiler;
    perf stops sampling and finalizes its file. perf runs with SIGHUP
    ignored, so a hang-up cannot end it before the Run."""

    def __init__(self, received: list[int]):
        self._received = received
        self._sent = 0
        self._group: int | None = None

    def install(self) -> dict:
        return {number: signal.signal(number, self._on_signal)
                for number in FORWARDED_SIGNALS}

    def attach(self, process: subprocess.Popen) -> None:
        self._group = process.pid
        self._flush()

    def _on_signal(self, number, _frame) -> None:
        self._received.append(number)
        self._flush()

    def _flush(self) -> None:
        while self._group is not None and self._sent < len(self._received):
            number = self._received[self._sent]
            self._sent += 1
            try:
                os.killpg(self._group, number)
            except OSError:
                pass


def _environment_file():
    """This command's environment for the launcher (--environment-fd): perf
    changes the environment of the command it starts, and the Run must get
    the one an ordinary Run gets."""
    environment = tempfile.TemporaryFile()
    environment.write(b"".join(
        key + b"=" + value + b"\0" for key, value in os.environb.items()))
    environment.seek(0)
    return environment


def _ignore_hangup() -> None:
    signal.signal(signal.SIGHUP, signal.SIG_IGN)


def execute(perf: Path, request: CpuRequest, layout: Layout,
            cwd: Path) -> CaptureRun:
    os.mkfifo(layout.control, 0o600)
    environment = _environment_file()
    status_read, status_write = os.pipe()
    command = record_command(perf, request, layout, status_write,
                             environment.fileno())
    received: list[int] = []
    forwarder = _SessionForwarder(received)
    sys.stdout.flush()
    sys.stderr.flush()
    previous = forwarder.install()
    try:
        try:
            process = subprocess.Popen(
                command, cwd=cwd, stderr=subprocess.PIPE,
                pass_fds=(status_write, environment.fileno()),
                start_new_session=True, preexec_fn=_ignore_hangup)
        except BaseException:
            os.close(status_read)
            raise
        finally:
            os.close(status_write)
            environment.close()
        forwarder.attach(process)
        copier = _StderrCopy(process.stderr, sys.stderr.buffer)
        copier.start()
        stopped = _wait_within_limit(process, layout,
                                     request.max_capture_bytes)
        copier.finish(STDERR_DRAIN_S)
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
    launcher_error = report_launcher_error(_read_status(status_read))
    return CaptureRun(command, process.returncode, launcher_error,
                      received[0] if received else None, stopped.limit,
                      _diagnostics_report(layout.diagnostics), stopped.reserve)


@dataclass
class _Stop:
    """Why sampling was disabled: the size limit, the free-space reserve."""

    limit: bool = False
    reserve: bool = False


def _wait_within_limit(process: subprocess.Popen, layout: Layout,
                       limit: int) -> _Stop:
    """Wait for perf; at the size limit or the free-space reserve, disable
    sampling once. The Run goes on."""
    stop = _Stop()
    while True:
        try:
            process.wait(timeout=POLL_S)
            return stop
        except subprocess.TimeoutExpired:
            pass
        if stop.limit or stop.reserve:
            continue
        stop.limit = file_size(layout.perf_data) > limit
        stop.reserve = not stop.limit and reserve_reached(layout.cpu)
        if stop.limit or stop.reserve:
            _send_control(layout.control, b"disable\n")


def _send_control(fifo: Path, command: bytes) -> None:
    try:
        descriptor = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return  # perf has no reader open: it cannot be stopped this way
    try:
        os.write(descriptor, command)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _diagnostics_report(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return load_report(path)
    except ReportError:
        return None


# --- export and normalization -----------------------------------------------


@dataclass(frozen=True)
class Export:
    analysis: CpuAnalysis | None
    problem: str | None  # export_failed, or None


def _perf_text(perf: Path, arguments: list[str], cache: Path, cwd: Path,
               timeout: int, limit: int, stderr_file: Path | None = None):
    """stdout of one perf analysis command, or None with a reason. The
    output is read up to `limit` bytes; above it the command is stopped, so
    the export holds a bounded text in memory."""
    with tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(
                _analysis(perf, cache, *arguments), stdout=subprocess.PIPE,
                stderr=errors, cwd=cwd, env=tool_environment())
        except OSError:
            return None, "export_failed"
        reader = _BoundedRead(process, limit)
        reader.start()
        try:
            process.wait(timeout=timeout)
            problem = None
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            problem = "symbolization_timeout"
        reader.join()
        if reader.overflow:
            errors.write(f"\n{PROG}: the output is larger than the export "
                         f"limit of {limit} bytes; it was stopped\n".encode())
        if stderr_file is not None:
            errors.seek(max(0, errors.tell() - STDERR_TAIL_BYTES))
            stderr_file.write_bytes(errors.read())
    if problem is not None:
        return None, problem
    if reader.overflow or process.returncode != 0:
        return None, "export_failed"
    return b"".join(reader.chunks).decode(errors="replace"), None


class _BoundedRead(threading.Thread):
    """Reads a process's stdout up to a limit; at the limit it stops the
    process and drops the rest."""

    def __init__(self, process: subprocess.Popen, limit: int):
        super().__init__(daemon=True)
        self._process = process
        self._limit = limit
        self.chunks: list[bytes] = []
        self.overflow = False

    def run(self) -> None:
        size = 0
        stream = self._process.stdout
        for chunk in iter(lambda: stream.read(1 << 16), b""):
            size += len(chunk)
            if size > self._limit:
                self.overflow = True
                self.chunks.clear()
                self._process.kill()
                break
            self.chunks.append(chunk)
        stream.close()


def _add_to_cache(perf: Path, copy: Path, cache: Path, cwd: Path,
                  timeout: int, log) -> bool:
    """`perf buildid-cache -a`; its stderr goes to the log."""
    try:
        done = subprocess.run(
            _analysis(perf, cache, "buildid-cache", "-v", "-a", str(copy)),
            capture_output=True, cwd=cwd, timeout=timeout,
            env=tool_environment())
    except (OSError, subprocess.TimeoutExpired) as error:
        log.write(f"{copy}: {error}\n".encode())
        return False
    log.write(done.stdout[-STDERR_TAIL_BYTES:] + done.stderr[-STDERR_TAIL_BYTES:])
    return done.returncode == 0


def _lost_sample_records(stats: str | None) -> int:
    match = re.search(r"LOST_SAMPLES events:\s*(\d+)", stats or "")
    return int(match[1]) if match else 0


def _module_sources(request: CpuRequest, layout: Layout,
                    cwd: Path) -> tuple[Path, ...]:
    """The --modules-from paths; an archive is unpacked under the profile
    directory, and removed with the other temporary files."""
    sources = []
    for index, source in enumerate(request.modules_from):
        path = cwd / source
        if path.is_file() and zipfile.is_zipfile(path):
            target = layout.unpacked / str(index)
            try:
                with zipfile.ZipFile(path) as archive:
                    archive.extractall(target)  # strips absolute and ..
            except (OSError, zipfile.BadZipFile) as error:
                sys.stderr.write(f"{PROG}: cannot read --modules-from "
                                 f"'{source}': {error}\n")
                continue
            path = target
        sources.append(path)
    return tuple(sources)


def export_limit(request: CpuRequest) -> int:
    """The output limit of each perf analysis pass: twice the capture
    limit. perf's text of a sample is smaller than its stack copy in
    perf.data, so the limit only stops an export out of proportion."""
    return 2 * request.max_capture_bytes


def export(perf: Path, request: CpuRequest, layout: Layout,
           cwd: Path) -> Export:
    """Capture the retained modules, then export and normalize."""
    layout.cache.mkdir(exist_ok=True)
    timeout = request.symbolization_timeout_s
    limit = export_limit(request)
    data = ["-i", str(layout.perf_data)]
    locator = ModuleLocator(cwd=cwd, retained=layout.retained,
                            sources=_module_sources(request, layout, cwd))
    known: dict[tuple, ModuleFacts] = {}

    def facts(path: str, build_id: str | None) -> ModuleFacts:
        if (path, build_id) not in known:
            known[path, build_id] = locator.facts(path, build_id)
        return known[path, build_id]

    listing, _ = _perf_text(perf, ["buildid-list", *data], layout.cache, cwd,
                            timeout, limit)
    build_ids = {}
    for line in (listing or "").splitlines():
        build_id, _, path = line.strip().partition(" ")
        if path:
            build_ids[path] = build_id
            facts(path, build_id)
    cache_log = layout.cpu / "perf-buildid-cache.stderr.txt"
    with cache_log.open("ab") as log:
        for path, copy in locator.captured_files().items():
            if not _add_to_cache(perf, copy, layout.cache, cwd, timeout, log):
                # The copy is removed with the temporary files, so a module
                # that is not in the cache was not captured.
                key = (path, build_ids.get(path))
                known[key] = ModuleFacts("unavailable", "module_unavailable",
                                         None)
    stats, _ = _perf_text(perf, ["report", *data, "--stats"], layout.cache,
                          cwd, timeout, limit)
    script, problem = _perf_text(perf, ["script", *data, *SCRIPT_FIELDS],
                                 layout.cache, cwd, timeout, limit,
                                 layout.cpu / "perf-script.stderr.txt")
    srclines, srcline_problem = _perf_text(
        perf, ["script", *data, *SRCLINE_FIELDS], layout.cache, cwd, timeout,
        limit, layout.cpu / "perf-srcline.stderr.txt")
    if script is None:
        return Export(None, "export_failed")
    analysis = normalize_cpu(script, srclines=srclines, facts=facts,
                             lost_sample_records=_lost_sample_records(stats),
                             srcline_problem=srcline_problem,
                             build_ids=build_ids)
    return Export(analysis, problem)


# --- the report -------------------------------------------------------------


def capture_status(run: dict, capture_run: CaptureRun, layout: Layout,
                   exported: Export | None,
                   samples_file: str | None) -> tuple[str, list[str]]:
    if run["status"] == "not_started":
        return "not_started", ["run_not_started"]
    reasons = []
    expected = (-run["signal"] if run["status"] == "ended_by_signal"
                else run["exit_code"])
    if file_size(layout.perf_data) == 0 or (
            capture_run.supervisor_signal is None
            and capture_run.perf_status != expected):
        reasons.append("backend_failed")
    if exported is None or exported.problem is not None or (
            exported.analysis is not None and samples_file is None):
        # A capture without its samples file has lost its sample rows.
        reasons.append("export_failed")
    if capture_run.supervisor_signal is not None:
        reasons.append("interrupted")
    if exported is not None and exported.analysis is not None:
        totals = exported.analysis.totals
        if totals["lost_samples"] is None or totals["lost_samples"] > 0:
            reasons.append("lost_samples")
    if capture_run.limit_reached:
        reasons.append("capture_limit_reached")
    if capture_run.reserve_reached:
        reasons.append("storage_reserve_reached")
    return capture_outcome(reasons, {"backend_failed", "export_failed"})


def _artifacts(layout: Layout, request: CpuRequest, cwd: Path) -> list:
    return artifact_rows(request.target, cwd, [
        ("perf_data", layout.perf_data), ("cpu_samples", layout.samples),
        ("activation_observations", layout.observations),
        ("activation_correlation", layout.correlation)])


def _empty_analysis(reason: str) -> dict:
    return {
        "time_origin_ns": None, "samples_file": None,
        "totals": {"samples": 0, "weight_ns": 0, "samples_unknown_module": 0,
                   "samples_unknown_symbol": 0, "samples_with_caller": 0,
                   "lost_samples": None, "lost_records": 0,
                   "unavailable": {"lost_samples": reason}},
        "symbolization": {
            "function_names": {"samples_with_name": 0},
            "call_frame_unwinding": {"method": "dwarf",
                                     "samples_with_caller": 0},
            "source_lines": {"status": "unavailable", "scope": "leaf_frames",
                             "samples_with_line": None,
                             "unavailable": {"samples_with_line": reason}},
            "inlined_frames": {"status": "unavailable"}},
        "processes": [], "threads": [], "modules": [],
        "hotspots": {"limit": HOTSPOT_LIMIT, "self": [], "inclusive": [],
                     "self_remainder": {"samples": 0, "weight_ns": 0}},
        "unavailable": {"time_origin_ns": reason, "samples_file": reason},
    }


def _analysis_members(analysis: CpuAnalysis, samples_file: str | None,
                      reason: str) -> dict:
    unavailable = {}
    if analysis.time_origin_ns is None:
        unavailable["time_origin_ns"] = "no_samples"
    if samples_file is None:
        unavailable["samples_file"] = reason
    return {"time_origin_ns": analysis.time_origin_ns,
            "samples_file": samples_file, "totals": analysis.totals,
            "symbolization": analysis.symbolization,
            "processes": analysis.processes, "threads": analysis.threads,
            "modules": analysis.modules, "hotspots": analysis.hotspots,
            "unavailable": unavailable}


def build_report(request: CpuRequest, layout: Layout, cwd: Path,
                 capture_run: CaptureRun, exported: Export | None,
                 version: str, samples_file: str | None) -> dict:
    run = run_record(capture_run)
    status, reasons = capture_status(run, capture_run, layout, exported,
                                     samples_file)
    if exported is not None and exported.analysis is not None:
        members = _analysis_members(exported.analysis, samples_file,
                                    "export_failed")
    else:
        members = _empty_analysis("run_not_started" if status == "not_started"
                                  else "export_failed")
    perf_status = capture_run.perf_status
    guards = request.target.guards
    return {
        "sil_profile_report": 1,
        "producer": {"name": PROG, "sil_version": SIL_VERSION},
        "identity": identity(request.target, capture_run.report),
        "host": host(),
        "configuration": {
            "collector": "cpu",
            "recording": None if request.target.recording is None
            else str(request.target.recording),
            "participant_timeout_ms": guards.participant_timeout_ms,
            "max_protocol_line_bytes": guards.max_protocol_line_bytes,
            "max_step_output_messages": guards.max_step_output_messages,
            "max_step_inline_payload_bytes":
                guards.max_step_inline_payload_bytes,
            "cwd": str(cwd)},
        "run": run,
        "capture": {
            "kind": "cpu", "status": status, "reasons": reasons,
            "backend": {"name": "perf", "version": version,
                        "command": capture_run.command,
                        "exit_code": perf_status if perf_status >= 0
                        else None,
                        "signal": -perf_status if perf_status < 0 else None},
            "sampling": {"event": EVENT, "event_type": "software",
                         "privilege": "user", "mode": "frequency",
                         "frequency_hz": request.frequency_hz,
                         "weight": "period", "weight_unit": "cpu_clock_ns",
                         "clock": "host_monotonic"},
            "call_graph": {"method": "dwarf",
                           "stack_bytes": request.stack_bytes},
            "scope": {"kind": "runner_and_descendants", "inherit": True,
                      "pid_namespace": "collector"},
            "limits": {"max_capture_bytes": request.max_capture_bytes,
                       "capture_bytes": file_size(layout.perf_data),
                       "limit_reached": capture_run.limit_reached,
                       "symbolization_timeout_s":
                           request.symbolization_timeout_s},
            **members,
        },
        "activation_correlation": {"kind": "not_requested"},
        "artifacts": _artifacts(layout, request, cwd),
        "limitations": list(LIMITATIONS),
    }


def _pid_namespace() -> dict | None:
    """The PID namespace of this command, and so of perf (Linux)."""
    try:
        status = os.stat("/proc/self/ns/pid")
    except OSError:
        return None
    return {"device": status.st_dev, "inode": status.st_ino}


def _correlate(layout: Layout, report: dict,
               exported: Export | None) -> dict | None:
    """The `activation_correlation` member; writes correlation.json."""
    try:
        observations = layout.observations.read_text(encoding="utf-8",
                                                     errors="replace")
    except OSError:
        observations = None
    samples = (exported.analysis.samples_document
               if exported is not None and exported.analysis is not None
               else None)
    result = correlate(observations, report["capture"], samples,
                       pid_namespace=_pid_namespace())
    if result.document is not None:
        write_new_file(layout.correlation, json.dumps(
            result.document, separators=(",", ":")) + "\n")
    return result.record


# --- main -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    request = request_from_arguments(sys.argv[1:] if argv is None else argv)
    cwd = Path.cwd()
    problems = preflight(request.target, cwd) + request.problems(cwd)
    if problems:
        for problem in problems:
            command_error(PROG, problem, EXIT_USAGE)
        return EXIT_USAGE
    return request.capture(cwd)


def run_cpu(request: CpuRequest, cwd: Path) -> int:
    """The CPU collector after the usage checks."""
    try:
        find_native_tools()
    except ToolMissing as error:
        return command_error(PROG, str(error), EXIT_NOT_FOUND)
    try:
        perf = find_perf()
        probe_perf(perf, request)
    except PerfUnavailable as error:
        return command_error(PROG, str(error), EXIT_FAILURE)
    version = perf_version(perf)
    layout = Layout(Path(os.path.realpath(cwd / request.target.out)))
    layout.cpu.mkdir(parents=True)
    warning = space_warning(layout.cpu, request.max_capture_bytes)
    if warning is not None:
        sys.stderr.write(f"{PROG}: warning: {warning}\n")
    if request.activations:
        layout.activation.mkdir()
    capture_run = execute(perf, request, layout, cwd)
    report = _finish(perf, request, layout, cwd, capture_run, version)
    if report is not None:
        sys.stdout.write(format_summary(
            report, request.target.out / "profile.json"))
        sys.stdout.flush()
    return resolve_exit(report, capture_run)


def _write_samples(layout: Layout, analysis: CpuAnalysis) -> str | None:
    """Write cpu/samples.json; its name, or None when it cannot be written
    (a full disk): the report then says the file is unavailable."""
    try:
        write_new_file(layout.samples, json.dumps(
            analysis.samples_document, separators=(",", ":")) + "\n")
    except OSError as error:
        sys.stderr.write(f"{PROG}: cannot write '{layout.samples}': "
                         f"{error}\n")
        layout.samples.unlink(missing_ok=True)
        return None
    return "cpu/samples.json"


def _finish(perf: Path, request: CpuRequest, layout: Layout, cwd: Path,
            capture_run: CaptureRun, version: str) -> dict | None:
    """Export, normalize and publish; the report, or None if not written."""
    exported = None
    samples_file = None
    try:
        if run_record(capture_run)["status"] != "not_started" and \
                file_size(layout.perf_data) > 0:
            exported = export(perf, request, layout, cwd)
        if exported is not None and exported.analysis is not None:
            samples_file = _write_samples(layout, exported.analysis)
    finally:
        shutil.rmtree(layout.retained, ignore_errors=True)
        shutil.rmtree(layout.unpacked, ignore_errors=True)
        layout.control.unlink(missing_ok=True)
    report = build_report(request, layout, cwd, capture_run, exported,
                          version, samples_file)
    if request.activations:
        try:
            report["activation_correlation"] = _correlate(layout, report,
                                                          exported)
        except OSError as error:
            sys.stderr.write(f"{PROG}: cannot write '{layout.correlation}': "
                             f"{error}\n")
            return None
        report["artifacts"] = _artifacts(layout, request, cwd)
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


if __name__ == "__main__":
    sys.exit(main())
