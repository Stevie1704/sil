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
import hashlib
import json
import os
import platform
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
    EXIT_FAILURE, EXIT_NOT_FOUND, EXIT_OK, EXIT_USAGE, FORWARDED_SIGNALS,
    STDERR_DRAIN_S, GivenOnce, Guards, RunRequest, ToolMissing,
    _read_status, _StderrCopy, add_guard_arguments, command_error,
    find_native_tools, guards_from,
)
from sil.profile_activation import correlate
from sil.profile_cpu import CpuAnalysis, ModuleFacts, normalize_cpu
from sil.profile_modules import ModuleLocator
from sil.profile_report import validate_profile_report
from sil.profile_summary import format_summary

PROG = "sil profile"
EVENT = "cpu-clock:u"
FREQUENCY_HZ = 999
STACK_BYTES = 8192
MAX_CAPTURE_BYTES = 1 << 30
HEAP_MAX_CAPTURE_BYTES = 256 << 20
SYMBOLIZATION_TIMEOUT_S = 600
MAX_ACTIVATION_BYTES = 64 << 20
POLL_S = 0.1
STDERR_TAIL_BYTES = 64 * 1024
RUN_STATUSES = {0: "succeeded", 1: "run_failure", 2: "configuration_error"}
LIMITATIONS = ["cpu_samples_estimate_on_cpu_time", "user_space_only",
               "scope_is_runner_and_descendants", "host_build_only"]
SCRIPT_FIELDS = ["-F", "comm,pid,tid,time,period,ip,sym,dso", "--ns",
                 "--no-inline", "--show-task-events", "--show-mmap-events",
                 "--show-lost-events"]
SRCLINE_FIELDS = ["-G", "-F", "ip,sym,srcline"]
PERMISSION_HINT = (
    "an unprivileged user needs kernel.perf_event_paranoid 2 or less "
    "(now {paranoid}); a container needs CAP_PERFMON (docker run "
    "--cap-add PERFMON). sil profile does not change host settings; see "
    "docs/profile-report.md")


@dataclass(frozen=True)
class ProfileRequest:
    manifest: Path
    out: Path
    recording: Path | None  # None: --no-recording
    provenance: Path | None
    guards: Guards
    frequency_hz: int = FREQUENCY_HZ
    stack_bytes: int = STACK_BYTES
    max_capture_bytes: int = MAX_CAPTURE_BYTES
    symbolization_timeout_s: int = SYMBOLIZATION_TIMEOUT_S
    modules_from: tuple[Path, ...] = ()
    activations: bool = False
    max_activation_bytes: int = MAX_ACTIVATION_BYTES


@dataclass(frozen=True)
class HeapRequest:
    """`--collector heap`: sil.profile_heap_run runs it."""

    manifest: Path
    out: Path
    recording: Path | None  # None: --no-recording
    provenance: Path | None
    guards: Guards
    participant: str | None  # None: --runner
    max_capture_bytes: int = HEAP_MAX_CAPTURE_BYTES
    interpreter_timeout_s: int = SYMBOLIZATION_TIMEOUT_S


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


def request_from_arguments(argv: list[str]) -> ProfileRequest | HeapRequest:
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
    return ProfileRequest(
        args.manifest, args.out, recording, provenance, guards_from(args),
        args.frequency_hz or FREQUENCY_HZ, args.stack_bytes or STACK_BYTES,
        args.max_capture_bytes or MAX_CAPTURE_BYTES,
        args.symbolization_timeout_s or SYMBOLIZATION_TIMEOUT_S,
        tuple(args.modules_from), args.activations,
        args.max_activation_bytes or MAX_ACTIVATION_BYTES)


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
        args.manifest, args.out, recording, provenance, guards_from(args),
        args.participant, args.max_capture_bytes or HEAP_MAX_CAPTURE_BYTES,
        args.symbolization_timeout_s or SYMBOLIZATION_TIMEOUT_S)


def preflight(request: ProfileRequest | HeapRequest,
              cwd: Path) -> list[str]:
    problems = []
    out = cwd / request.out
    if os.path.lexists(out):
        problems.append(f"the profile directory '{request.out}' already "
                        "exists; earlier evidence is never replaced")
    elif not Path(os.path.realpath(out)).parent.is_dir():
        problems.append(f"the profile directory '{request.out}': its parent "
                        "directory does not exist")
    for source in getattr(request, "modules_from", ()):
        if not (cwd / source).exists():
            problems.append(f"--modules-from '{source}' does not exist")
    if request.recording is not None and _same(
            cwd / request.recording, cwd / request.manifest):
        problems.append(f"the Recording '{request.recording}' is the "
                        "Manifest")
    return problems


def _same(a: Path, b: Path) -> bool:
    if os.path.realpath(a) == os.path.realpath(b):
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


# --- perf -------------------------------------------------------------------


class PerfUnavailable(RuntimeError):
    """perf, its event or the permission to use it is missing."""


def _sampling_options(request: ProfileRequest) -> list[str]:
    return ["-e", EVENT, "-F", str(request.frequency_hz), "-k", "monotonic",
            "--call-graph", f"dwarf,{request.stack_bytes}", "--buildid-mmap",
            "-N", "-q"]


def _paranoid() -> int | None:
    try:
        return int(Path("/proc/sys/kernel/perf_event_paranoid").read_text())
    except (OSError, ValueError):
        return None


def find_perf() -> Path:
    found = shutil.which("perf")
    if found is None:
        raise PerfUnavailable(
            "cannot find perf on PATH; install the perf of the running "
            "kernel (Debian: linux-perf, Ubuntu: linux-tools-$(uname -r))")
    return Path(found)


def probe_perf(perf: Path, request: ProfileRequest) -> None:
    """Record `true` with the exact event and options, or raise."""
    true = shutil.which("true") or "/bin/true"
    with tempfile.TemporaryDirectory(prefix="sil-profile-probe-") as probe:
        data = Path(probe) / "probe.data"
        try:
            done = subprocess.run(
                [str(perf), "record", *_sampling_options(request), "-o",
                 str(data), "--", true], capture_output=True, text=True,
                timeout=60, env=_tool_environment())
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
    paranoid = _paranoid()
    return PERMISSION_HINT.format(
        paranoid="unknown" if paranoid is None else paranoid)


def perf_version(perf: Path) -> str:
    done = subprocess.run([str(perf), "--version"], capture_output=True,
                          text=True, env=_tool_environment())
    return done.stdout.strip() or "unknown"


def _tool_environment() -> dict:
    """The environment of perf's own analysis commands, not of the Run:
    no debuginfod network lookup."""
    env = dict(os.environ)
    env.pop("DEBUGINFOD_URLS", None)
    return env


def _analysis(perf: Path, cache: Path, *arguments: str) -> list[str]:
    """A perf analysis command on the profile's own build-ID cache. perf
    reads the cache location from --buildid-dir only; it sets
    PERF_BUILDID_DIR for its children but does not read it."""
    return [str(perf), "--buildid-dir", str(cache), *arguments]


# --- the Run ----------------------------------------------------------------


@dataclass(frozen=True)
class CaptureRun:
    """What perf, the launcher and the runner left."""

    command: list[str]
    perf_status: int  # perf's exit code, or -N when signal N ended it
    launcher_error: str | None
    supervisor_signal: int | None
    limit_reached: bool
    report: dict | None


def record_command(perf: Path, request: ProfileRequest, layout: Layout,
                   status_fd: int, environment_fd: int) -> list[str]:
    tools = find_native_tools()
    run = RunRequest(request.manifest, layout.diagnostics, request.recording,
                     request.provenance, request.guards)
    return [str(perf), "record", *_sampling_options(request), "--control",
            f"fifo:{layout.control}", "-o", str(layout.perf_data), "--",
            str(tools.launcher), "--status-fd", str(status_fd),
            "--environment-fd", str(environment_fd),
            str(tools.runner), *run.runner_arguments(),
            "--retain-run-directory", str(layout.retained),
            *_activation_arguments(request, layout)]


def _activation_arguments(request: ProfileRequest, layout: Layout) -> list:
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


def execute(perf: Path, request: ProfileRequest, layout: Layout,
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
        limit_reached = _wait_within_limit(process, layout,
                                           request.max_capture_bytes)
        copier.finish(STDERR_DRAIN_S)
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
    launcher_error = report_launcher_error(_read_status(status_read))
    return CaptureRun(command, process.returncode, launcher_error,
                      received[0] if received else None, limit_reached,
                      _diagnostics_report(layout.diagnostics))


def report_launcher_error(error: str | None) -> str | None:
    """Say on stderr that the launcher failed and no Run took place."""
    if error is not None:
        sys.stderr.write(f"{PROG}: sil-diagnostics-launcher failed, so no "
                         f"Run took place: "
                         f"{error.removeprefix('sil-diagnostics-launcher: ')}"
                         "\n")
    return error


def _wait_within_limit(process: subprocess.Popen, layout: Layout,
                       limit: int) -> bool:
    """Wait for perf; at the limit, disable sampling once. The Run goes on."""
    reached = False
    while True:
        try:
            process.wait(timeout=POLL_S)
            return reached
        except subprocess.TimeoutExpired:
            pass
        if not reached and _size(layout.perf_data) > limit:
            reached = True
            _send_control(layout.control, b"disable\n")


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


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


def run_record(run: CaptureRun) -> dict:
    """The `run` member: the Run's own outcome, never the capture's."""
    record = {"status": None, "exit_code": None, "signal": None,
              "failure": None, "diagnostics_report": None,
              "supervisor_signal": run.supervisor_signal}
    if run.report is not None:
        outcome = run.report["run"]
        failure = outcome["failure"]
        record.update(status=outcome["status"],
                      exit_code=outcome["exit_code"],
                      diagnostics_report="run.diagnostics.json",
                      failure=None if failure is None else {
                          "message": failure["message"],
                          "category": failure["category"]})
    elif run.launcher_error is not None:
        record["status"] = "not_started"
    elif run.perf_status < 0:
        record.update(status="ended_by_signal", signal=-run.perf_status)
    elif run.perf_status in RUN_STATUSES:
        # No report: the runner could not write it; perf ends as the Run.
        record.update(status=RUN_STATUSES[run.perf_status],
                      exit_code=run.perf_status)
    else:
        record["status"] = "not_started"  # perf failed before the Run
    return record


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
                stderr=errors, cwd=cwd, env=_tool_environment())
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
            env=_tool_environment())
    except (OSError, subprocess.TimeoutExpired) as error:
        log.write(f"{copy}: {error}\n".encode())
        return False
    log.write(done.stdout[-STDERR_TAIL_BYTES:] + done.stderr[-STDERR_TAIL_BYTES:])
    return done.returncode == 0


def _lost_sample_records(stats: str | None) -> int:
    match = re.search(r"LOST_SAMPLES events:\s*(\d+)", stats or "")
    return int(match[1]) if match else 0


def _module_sources(request: ProfileRequest, layout: Layout,
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


def export_limit(request: ProfileRequest) -> int:
    """The output limit of each perf analysis pass: twice the capture
    limit. perf's text of a sample is smaller than its stack copy in
    perf.data, so the limit only stops an export out of proportion."""
    return 2 * request.max_capture_bytes


def export(perf: Path, request: ProfileRequest, layout: Layout,
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
                   exported: Export | None) -> tuple[str, list[str]]:
    if run["status"] == "not_started":
        return "not_started", ["run_not_started"]
    reasons = []
    expected = (-run["signal"] if run["status"] == "ended_by_signal"
                else run["exit_code"])
    if _size(layout.perf_data) == 0 or (
            capture_run.supervisor_signal is None
            and capture_run.perf_status != expected):
        reasons.append("backend_failed")
    if exported is None or exported.problem is not None:
        reasons.append("export_failed")
    if capture_run.supervisor_signal is not None:
        reasons.append("interrupted")
    if exported is not None and exported.analysis is not None:
        totals = exported.analysis.totals
        if totals["lost_samples"] is None or totals["lost_samples"] > 0:
            reasons.append("lost_samples")
    if capture_run.limit_reached:
        reasons.append("capture_limit_reached")
    if {"backend_failed", "export_failed"} & set(reasons):
        return "failed", reasons
    if "interrupted" in reasons:
        return "interrupted", reasons
    return ("partial" if reasons else "complete"), reasons


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _artifacts(layout: Layout, request: ProfileRequest, cwd: Path) -> list:
    return artifact_rows(layout.out, request, cwd, [
        ("perf_data", layout.perf_data), ("cpu_samples", layout.samples),
        ("activation_observations", layout.observations),
        ("activation_correlation", layout.correlation)])


def artifact_rows(out: Path, request, cwd: Path, collected: list) -> list:
    """`{role, path, bytes, sha256}` of every existing file in `out`: the
    diagnostics report, the Recording and the collector's files."""
    candidates = [("diagnostics_report", out / "run.diagnostics.json")]
    if request.recording is not None:
        candidates.append(("recording", cwd / request.recording))
    candidates += collected
    rows = []
    for role, path in candidates:
        try:
            relative = Path(os.path.realpath(path)).relative_to(out)
        except ValueError:
            continue  # outside the profile directory: not part of it
        if path.is_file():
            rows.append({"role": role, "path": relative.as_posix(),
                         "bytes": path.stat().st_size,
                         "sha256": _sha256(path)})
    return rows


def _identity(request: ProfileRequest, report: dict | None) -> dict:
    keys = ("manifest_hash", "sil_version", "source_repository",
            "source_revision")
    if report is None:
        return {"manifest_path": str(request.manifest),
                **{key: None for key in keys},
                "unavailable": {key: "no_diagnostics_report" for key in keys}}
    source = report["identity"]
    identity = {"manifest_path": str(request.manifest),
                **{key: source.get(key) for key in keys}}
    identity["unavailable"] = {key: source.get("unavailable", {}).get(
        key, "not_reported") for key in keys if identity[key] is None}
    return identity


def _host() -> dict:
    paranoid = _paranoid()
    uname = platform.uname()
    return {"os": uname.system, "kernel_release": uname.release,
            "architecture": uname.machine, "perf_event_paranoid": paranoid,
            "unavailable": {} if paranoid is not None else {
                "perf_event_paranoid": "not_reported_by_os"}}


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
            "inlined_frames": {"status": "not_requested"}},
        "processes": [], "threads": [], "modules": [],
        "hotspots": {"limit": 50, "self": [], "inclusive": [],
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


def build_report(request: ProfileRequest, layout: Layout, cwd: Path,
                 capture_run: CaptureRun, exported: Export | None,
                 version: str, samples_file: str | None) -> dict:
    run = run_record(capture_run)
    status, reasons = capture_status(run, capture_run, layout, exported)
    if exported is not None and exported.analysis is not None:
        members = _analysis_members(exported.analysis, samples_file,
                                    "export_failed")
    else:
        members = _empty_analysis("run_not_started" if status == "not_started"
                                  else "export_failed")
    perf_status = capture_run.perf_status
    guards = request.guards
    return {
        "sil_profile_report": 1,
        "producer": {"name": PROG, "sil_version": SIL_VERSION},
        "identity": _identity(request, capture_run.report),
        "host": _host(),
        "configuration": {
            "collector": "cpu",
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
                       "capture_bytes": _size(layout.perf_data),
                       "limit_reached": capture_run.limit_reached,
                       "symbolization_timeout_s":
                           request.symbolization_timeout_s},
            **members,
        },
        "activation_correlation": {"kind": "not_requested"},
        "artifacts": _artifacts(layout, request, cwd),
        "limitations": list(LIMITATIONS),
    }


def resolve_exit(report: dict | None, capture_run: CaptureRun) -> int:
    """A Run failure is never replaced; a capture problem gives 1, and so
    does a requested Activation correlation that is not complete."""
    run = run_record(capture_run)
    if run["status"] == "not_started":
        return EXIT_NOT_FOUND if capture_run.perf_status == EXIT_NOT_FOUND \
            else EXIT_FAILURE
    if run["status"] == "ended_by_signal":
        return 128 + run["signal"]
    if run["exit_code"] != EXIT_OK:
        return run["exit_code"]
    if report is None or report["capture"]["status"] != "complete":
        return EXIT_FAILURE
    correlation = report["activation_correlation"]
    if correlation["kind"] == "activation" and \
            correlation["status"] != "complete":
        return EXIT_FAILURE
    return EXIT_OK


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
    problems = preflight(request, cwd)
    if problems:
        for problem in problems:
            command_error(PROG, problem, EXIT_USAGE)
        return EXIT_USAGE
    if isinstance(request, HeapRequest):
        # Imported here: the heap collector reuses this module's helpers.
        from sil.profile_heap_run import run_heap
        return run_heap(request, cwd)
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
    layout = Layout(Path(os.path.realpath(cwd / request.out)))
    layout.cpu.mkdir(parents=True)
    if request.activations:
        layout.activation.mkdir()
    capture_run = execute(perf, request, layout, cwd)
    report = _finish(perf, request, layout, cwd, capture_run, version)
    if report is not None:
        sys.stdout.write(format_summary(report, request.out / "profile.json"))
        sys.stdout.flush()
    return resolve_exit(report, capture_run)


def _finish(perf: Path, request: ProfileRequest, layout: Layout, cwd: Path,
            capture_run: CaptureRun, version: str) -> dict | None:
    """Export, normalize and publish; the report, or None if not written."""
    exported = None
    samples_file = None
    try:
        if run_record(capture_run)["status"] != "not_started" and \
                _size(layout.perf_data) > 0:
            exported = export(perf, request, layout, cwd)
        if exported is not None and exported.analysis is not None:
            write_new_file(layout.samples, json.dumps(
                exported.analysis.samples_document, separators=(",", ":"))
                + "\n")
            samples_file = "cpu/samples.json"
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
