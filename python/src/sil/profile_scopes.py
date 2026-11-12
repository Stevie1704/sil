"""`sil profile --collector scopes`: one source-scope capture (#372).

docs/proposals/native-source-scopes.md is the contract: the command, its
lifecycle, its files and its Run and capture outcomes. In short: preflight
reads the scope map from the selected Native participant's instrumented
library; the command writes `scopes/map.json`, creates and preallocates
`scopes/events.bin`, and starts `sil-diagnostics-launcher` and
`sil-run-diagnostics` with Activation observations and the session variable
SIL_SOURCE_SCOPE_SESSION. Only the runner's observer becomes active and fills
the mapped file. After the Run the command decodes the saved files
(sil.source_scope_decode), then writes `scopes/calls.json` and, last,
`scopes.json`.

The Run outcome and the capture outcome are separate. A capture problem never
replaces a nonzero Run exit code; after a successful Run it gives exit 1.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from sil import manifest_json
from sil.build_info import __version__ as SIL_VERSION
from sil.diagnostics_report import write_new_file
from sil.diagnostics_run import (
    EXIT_FAILURE, EXIT_NOT_FOUND, EXIT_USAGE, NativeTools, ToolMissing,
    command_error, execute, find_native_tools,
)
from sil.profile_common import (
    PROG, CaptureRun, RunTarget, artifact_rows, report_launcher_error,
    resolve_exit, run_record,
)
from sil.profile_storage import RESERVE_BYTES, free_bytes
from sil.recording import MCAP_MAGIC
from sil.source_scope_decode import (
    ACTIVE, CREATED, HEADER, MAGIC, MAX_RECORDS, RECORD, VERSION, CaptureFailed,
    decode, read_header,
)
from sil.source_scope_map import (
    OBSERVER_SONAME, LibraryScan, MapError, file_identity, file_sha256,
    read_library,
)
from sil.source_scope_report import (
    CALLS_PATH, DIAGNOSTICS_PATH, EVENTS_PATH, MAP_PATH, OBSERVATIONS_PATH,
    REPORT_PATH, capture_record, encode_calls, encode_map, encode_report,
    map_document, scope_report,
)
from sil.source_scope_set import ScopeSet
from sil.source_scope_summary import format_scope_summary

SESSION_VARIABLE = "SIL_SOURCE_SCOPE_SESSION"
MAX_SCOPE_RECORDS = 65536
MAX_ACTIVATION_BYTES = 64 << 20
# The observer ABI that wrote the raw file: soname, symbol version and raw
# file version.
OBSERVER_VERSION = f"{OBSERVER_SONAME} sil_source_scope_v1 raw {VERSION}"


@dataclass(frozen=True)
class ScopesRequest:
    """`--collector scopes`: the source scopes of one Native participant."""

    target: RunTarget
    participant: str
    original_library: Path | None = None
    max_scope_records: int = MAX_SCOPE_RECORDS
    max_activation_bytes: int = MAX_ACTIVATION_BYTES

    def problems(self, cwd: Path) -> list[str]:
        """No option problem: run_scopes reads the Manifest and library."""
        return []

    def capture(self, cwd: Path) -> int:
        return run_scopes(self, cwd)


@dataclass(frozen=True)
class ScopesLayout:
    """The paths of one scopes directory, absolute."""

    out: Path

    @property
    def scopes(self) -> Path:
        return self.map.parent

    @property
    def map(self) -> Path:
        return self.out / MAP_PATH

    @property
    def events(self) -> Path:
        return self.out / EVENTS_PATH

    @property
    def calls(self) -> Path:
        return self.out / CALLS_PATH

    @property
    def activation(self) -> Path:
        return self.observations.parent

    @property
    def observations(self) -> Path:
        return self.out / OBSERVATIONS_PATH

    @property
    def diagnostics(self) -> Path:
        return self.out / DIAGNOSTICS_PATH

    @property
    def report(self) -> Path:
        return self.out / REPORT_PATH


class Refusal(Exception):
    """The command stops before the Run, with this message and exit code."""

    def __init__(self, message: str, exit_code: int):
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class Preflight:
    """What the command knows before it makes the directory."""

    library: Path
    scan: LibraryScan
    original: dict | None
    scope_map: dict
    map_bytes: bytes
    manifest_sha256: str


# --- preflight --------------------------------------------------------------


def preflight(request: ScopesRequest, cwd: Path) -> Preflight:
    """The scope map and identities; Refusal with exit 2 otherwise."""
    manifest_path = cwd / request.target.manifest
    try:
        manifest_bytes = manifest_path.read_bytes()
        participant = manifest_json.loads(manifest_bytes)["participants"].get(
            request.participant)
    except (OSError, ValueError, KeyError, AttributeError, TypeError) as error:
        raise Refusal(f"cannot read the Manifest '{request.target.manifest}' "
                      f"to check --participant: {error}", EXIT_USAGE) from None
    if not isinstance(participant, dict):
        raise Refusal(f"the Manifest has no participant "
                      f"'{request.participant}'", EXIT_USAGE)
    if participant.get("type") != "native" or \
            not isinstance(participant.get("library"), str):
        raise Refusal(f"'{request.participant}' is a "
                      f"{participant.get('type')} participant, not a Native "
                      "participant: source scopes are captured in the Native "
                      "library of the runner", EXIT_USAGE)
    # The runner resolves a relative library path against the Manifest's
    # directory.
    library = Path(os.path.realpath(
        manifest_path.parent / participant["library"]))
    try:
        scan = read_library(library)
        original = None if request.original_library is None else \
            file_identity(cwd / request.original_library)
        scope_map = map_document(scan, original)
        map_bytes = encode_map(scope_map)
    except MapError as error:
        raise Refusal(str(error), EXIT_USAGE) from None
    return Preflight(library, scan, original, scope_map, map_bytes,
                     hashlib.sha256(manifest_bytes).hexdigest())


def host_tools() -> NativeTools:
    """The runner and launcher on a host that can capture source scopes;
    Refusal otherwise."""
    system, machine = platform.system(), platform.machine()
    libc = platform.libc_ver()[0]
    if (system, machine, libc) != ("Linux", "x86_64", "glibc"):
        raise Refusal(f"source scopes need Linux x86-64/glibc; this host is "
                      f"{system} {machine} {libc or 'without glibc'}",
                      EXIT_FAILURE)
    try:
        return find_native_tools()
    except ToolMissing as error:
        raise Refusal(str(error), EXIT_NOT_FOUND) from None


# --- the raw file and the session -------------------------------------------


def raw_size(capacity: int) -> int:
    return HEADER.size + RECORD.size * capacity


def create_raw_file(path: Path, capture_id: uuid.UUID, capacity: int) -> int:
    """Create `events.bin` at its full size with the header in state
    `created`; its open descriptor. OSError when it cannot be created or
    allocated."""
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL
                         | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.posix_fallocate(descriptor, 0, raw_size(capacity))
        header = HEADER.pack(MAGIC, VERSION, HEADER.size, RECORD.size, CREATED,
                             capture_id.bytes, 0, 0, capacity, 0, 0, 0, 0,
                             0, 0, 0, 0, 0, 0, b"")
        if os.pwrite(descriptor, header, 0) != len(header):
            raise OSError("short write of the raw header")
    except OSError:
        os.close(descriptor)
        raise
    return descriptor


def session_value(capture_id: uuid.UUID, capacity: int,
                  descriptor: int) -> str:
    """SIL_SOURCE_SCOPE_SESSION; the launcher adds `,pid=` of the runner."""
    return f"capture={capture_id.hex},capacity={capacity},fd={descriptor}"


def _space_warning(out: Path, raw_bytes: int,
                   observation_limit: int) -> str | None:
    """Before the raw file is allocated: its size, the observation limit and
    the reserve against the free space of DIR."""
    needed = raw_bytes + observation_limit + RESERVE_BYTES
    free = free_bytes(out)
    if free is None or free >= needed:
        return None
    return (f"'{out}' has {free} bytes free, less than the raw file of "
            f"{raw_bytes} bytes, the observation limit of {observation_limit} "
            f"bytes and the reserve of {RESERVE_BYTES} bytes")


def create_capture_files(layout: ScopesLayout, map_bytes: bytes,
                         capture_id: uuid.UUID, capacity: int,
                         observation_limit: int) -> int:
    """Make DIR with `scopes/map.json` and the allocated raw file; the raw
    file's open descriptor. Refusal (exit 1) when a file cannot be written.
    A failed `map.json` leaves DIR as found: the command removes it."""
    try:
        layout.out.mkdir()
    except OSError as error:
        raise Refusal(f"cannot make '{layout.out}': {error}",
                      EXIT_FAILURE) from None
    try:
        layout.scopes.mkdir()
        write_new_file(layout.map, map_bytes.decode("ascii"))
    except OSError as error:
        shutil.rmtree(layout.out, ignore_errors=True)
        raise Refusal(f"cannot write '{layout.map}': {error}",
                      EXIT_FAILURE) from None
    warning = _space_warning(layout.out, raw_size(capacity),
                             observation_limit)
    if warning is not None:
        sys.stderr.write(f"{PROG}: warning: {warning}\n")
    try:
        return create_raw_file(layout.events, capture_id, capacity)
    except OSError as error:
        raise Refusal(f"cannot create '{layout.events}': {error}; the Run "
                      "did not start", EXIT_FAILURE) from None


# --- the Run ----------------------------------------------------------------


def run_with_session(request: ScopesRequest, layout: ScopesLayout,
                     native: NativeTools, cwd: Path, capture_id: uuid.UUID,
                     descriptor: int) -> CaptureRun:
    """The Run with the session of the open raw file; closes `descriptor`."""
    try:
        layout.activation.mkdir()
        session = session_value(capture_id, request.max_scope_records,
                                descriptor)
        return _execute(request, layout, native, cwd, session, descriptor)
    finally:
        os.close(descriptor)


def _execute(request: ScopesRequest, layout: ScopesLayout,
             native: NativeTools, cwd: Path, session: str,
             descriptor: int) -> CaptureRun:
    run = request.target.run_request(layout.diagnostics)
    extra = ("--activation-observations", str(layout.observations),
             "--activation-observations-limit-bytes",
             str(request.max_activation_bytes))
    outcome = execute(run, native, cwd=cwd, extra_arguments=extra,
                      environment={**os.environ, SESSION_VARIABLE: session},
                      pass_fds=(descriptor,))
    status = outcome.exit_code if outcome.exit_code is not None else (
        -outcome.signal if outcome.signal else EXIT_FAILURE)
    command = [str(native.launcher), str(native.runner),
               *run.runner_arguments(), *extra]
    return CaptureRun(command, status,
                      report_launcher_error(outcome.launcher_error),
                      outcome.supervisor_signal, False, outcome.report)


# --- decode and the report ---------------------------------------------------


def _read(path: Path, limit: int) -> bytes | None:
    try:
        with open(path, "rb") as stream:
            data = stream.read(limit + 1)
    except OSError:
        return None
    return data if len(data) <= limit else None


@dataclass(frozen=True)
class Decoded:
    events: bytes | None
    calls: dict | None
    calls_bytes: bytes | None
    failure: CaptureFailed | None


def decode_capture(request: ScopesRequest, layout: ScopesLayout,
                   found: Preflight, capture_id: uuid.UUID,
                   capture_run: CaptureRun) -> Decoded:
    """Decode the saved files; the calls document or the failure."""
    events = _read(layout.events, raw_size(MAX_RECORDS))
    observations = _read(layout.observations,
                         request.max_activation_bytes + (1 << 20))
    try:
        # A created file normally means a successful zero-event capture.
        # A signaled runner did not finalize, even if no scope activated it
        # and the observation header was never written.
        if run_record(capture_run)["signal"] is not None and \
                read_header(events or b"").state in (CREATED, ACTIVE):
            raise CaptureFailed("raw_incomplete", "the runner did not finalize")
        calls = decode(events or b"",
                       (observations or b"").decode("utf-8", "replace"),
                       found.scope_map, participant=request.participant,
                       capture_id=str(capture_id),
                       library_sha256=file_sha256(found.library))
        calls_bytes = encode_calls(calls)  # within its budget
    except CaptureFailed as failure:
        return Decoded(events, None, None, failure)
    return Decoded(events, calls, calls_bytes, None)


def _configuration(request: ScopesRequest, found: Preflight,
                   cwd: Path) -> dict:
    guards = request.target.guards
    return {
        "manifest_path": str(request.target.manifest),
        "manifest_sha256": found.manifest_sha256,
        "participant": request.participant,
        "capacity": request.max_scope_records,
        "max_activation_bytes": request.max_activation_bytes,
        "recording": None if request.target.recording is None
        else str(request.target.recording),
        "provenance_mode": request.target.provenance_mode,
        "guards": {
            "participant_timeout_ms": guards.participant_timeout_ms,
            "max_protocol_line_bytes": guards.max_protocol_line_bytes,
            "max_step_output_messages": guards.max_step_output_messages,
            "max_step_inline_payload_bytes":
                guards.max_step_inline_payload_bytes},
        "cwd": str(cwd)}


def _files(layout: ScopesLayout) -> list[tuple[str, Path]]:
    return [("activation_observations", layout.observations),
            ("scope_map", layout.map), ("scope_events", layout.events),
            ("scope_calls", layout.calls)]


def _missing(request: ScopesRequest, layout: ScopesLayout,
             cwd: Path) -> list[dict]:
    expected = [("diagnostics_report", layout.diagnostics), *_files(layout)]
    rows = []
    if request.target.recording is None:
        rows.append({"role": "recording", "path": "run.mcap",
                     "reason": "not_requested"})
    else:
        expected.append(("recording", cwd / request.target.recording))
    for role, path in expected:
        try:
            relative = Path(os.path.realpath(path)).relative_to(layout.out)
        except ValueError:
            continue  # outside the directory: not one of its files
        if not path.is_file() or not _readable(role, path):
            rows.append({"role": role, "path": relative.as_posix(),
                         "reason": "not_written"})
    return rows


def _readable(role: str, path: Path) -> bool:
    """False for a file that a killed runner left without even the start
    of its contract: a Recording without the MCAP magic, an observation
    stream without its header line. It is `not_written`, not an artifact."""
    try:
        with path.open("rb") as stream:
            if role == "recording":
                return stream.read(len(MCAP_MAGIC)) == MCAP_MAGIC
            if role == "activation_observations":
                first = json.loads(stream.readline(1 << 16))
                return isinstance(first, dict) and \
                    first.get("sil_activation_observations") == 1
    except (OSError, ValueError):
        return False
    return True


def _artifacts(request: ScopesRequest, layout: ScopesLayout,
               cwd: Path) -> list[dict]:
    return [row for row in artifact_rows(request.target, cwd, _files(layout))
            if _readable(row["role"], layout.out / row["path"])]


def build_report(request: ScopesRequest, layout: ScopesLayout, cwd: Path,
                 found: Preflight, capture_run: CaptureRun,
                 decoded: Decoded) -> dict:
    capture = capture_record(
        decoded.events, decoded.calls, failure=decoded.failure,
        interrupted=capture_run.supervisor_signal is not None)
    return scope_report(
        sil_version=SIL_VERSION,
        configuration=_configuration(request, found, cwd),
        run=run_record(capture_run), capture=capture,
        identities={"instrumented": found.scan.identity,
                    "original": found.original,
                    "observer_version": OBSERVER_VERSION,
                    "compiler": found.scan.compiler},
        artifacts=_artifacts(request, layout, cwd),
        missing=_missing(request, layout, cwd),
        scope_map=found.scope_map, calls=decoded.calls)


def publish(request: ScopesRequest, layout: ScopesLayout, cwd: Path,
            found: Preflight, capture_run: CaptureRun,
            decoded: Decoded) -> ScopeSet | None:
    """Write calls.json, then scopes.json last; the saved capture, or None
    when a file cannot be written."""
    try:
        if decoded.calls_bytes is not None:
            write_new_file(layout.calls, decoded.calls_bytes.decode("ascii"))
        report = build_report(request, layout, cwd, found, capture_run,
                              decoded)
        source = encode_report(report).decode("ascii")
        write_new_file(layout.report, source)
    except (OSError, ValueError) as error:
        sys.stderr.write(f"{PROG}: cannot write the scopes report: {error}\n")
        return None
    return ScopeSet(report, source, found.scope_map, decoded.calls)


# --- main -------------------------------------------------------------------


def run_scopes(request: ScopesRequest, cwd: Path) -> int:
    """The scopes collector after the option checks."""
    layout = ScopesLayout(Path(os.path.realpath(cwd / request.target.out)))
    capture_id = uuid.uuid4()
    try:
        found = preflight(request, cwd)
        native = host_tools()
        descriptor = create_capture_files(
            layout, found.map_bytes, capture_id, request.max_scope_records,
            request.max_activation_bytes)
    except Refusal as refusal:
        return command_error(PROG, str(refusal), refusal.exit_code)
    capture_run = run_with_session(request, layout, native, cwd, capture_id,
                                   descriptor)
    decoded = decode_capture(request, layout, found, capture_id, capture_run)
    saved = publish(request, layout, cwd, found, capture_run, decoded)
    if saved is None:
        return resolve_exit(None, capture_run)
    sys.stdout.write(format_scope_summary(
        saved, request.target.out / REPORT_PATH))
    sys.stdout.flush()
    return resolve_exit(saved.report, capture_run)
