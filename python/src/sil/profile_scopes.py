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
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from sil.build_info import __version__ as SIL_VERSION
from sil.diagnostics_report import write_new_file
from sil.diagnostics_run import (
    EXIT_FAILURE, EXIT_NOT_FOUND, EXIT_USAGE, ToolMissing, command_error,
    execute, find_native_tools,
)
from sil.profile_common import (
    PROG, CaptureRun, RunTarget, artifact_rows, report_launcher_error,
    resolve_exit, run_record,
)
from sil.profile_storage import RESERVE_BYTES, free_bytes
from sil.source_scope_decode import (
    CREATED, HEADER, MAGIC, MAX_RECORDS, RECORD, VERSION, CaptureFailed,
    decode,
)
from sil.source_scope_map import (
    OBSERVER_SONAME, LibraryScan, MapError, file_identity, file_sha256,
    read_library,
)
from sil.source_scope_report import (
    encode_calls, encode_map, encode_report, map_document, scope_report,
    capture_record,
)

SESSION_VARIABLE = "SIL_SOURCE_SCOPE_SESSION"
MAX_SCOPE_RECORDS = 65536
MAX_ACTIVATION_BYTES = 64 << 20
MCAP_MAGIC = b"\x89MCAP0\r\n"
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
        return self.out / "scopes"

    @property
    def map(self) -> Path:
        return self.scopes / "map.json"

    @property
    def events(self) -> Path:
        return self.scopes / "events.bin"

    @property
    def calls(self) -> Path:
        return self.scopes / "calls.json"

    @property
    def activation(self) -> Path:
        return self.out / "activation"

    @property
    def observations(self) -> Path:
        return self.activation / "observations.jsonl"

    @property
    def diagnostics(self) -> Path:
        return self.out / "run.diagnostics.json"

    @property
    def report(self) -> Path:
        return self.out / "scopes.json"


class Usage(ValueError):
    """A preflight problem of the selected participant: exit 2."""


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
    """The scope map and identities, or Usage."""
    manifest_path = cwd / request.target.manifest
    try:
        manifest_bytes = manifest_path.read_bytes()
        participant = json.loads(manifest_bytes)["participants"].get(
            request.participant)
    except (OSError, ValueError, KeyError, AttributeError, TypeError) as error:
        raise Usage(f"cannot read the Manifest '{request.target.manifest}' "
                    f"to check --participant: {error}") from None
    if not isinstance(participant, dict):
        raise Usage(f"the Manifest has no participant '{request.participant}'")
    if participant.get("type") != "native" or \
            not isinstance(participant.get("library"), str):
        raise Usage(f"'{request.participant}' is a "
                    f"{participant.get('type')} participant, not a Native "
                    "participant: source scopes are captured in the Native "
                    "library of the runner")
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
        raise Usage(str(error)) from None
    return Preflight(library, scan, original, scope_map, map_bytes,
                     hashlib.sha256(manifest_bytes).hexdigest())


def host_problem() -> str | None:
    """Why this host cannot capture source scopes, or None."""
    system, machine = platform.system(), platform.machine()
    libc = platform.libc_ver()[0]
    if (system, machine, libc) != ("Linux", "x86_64", "glibc"):
        return (f"source scopes need Linux x86-64/glibc; this host is "
                f"{system} {machine} {libc or 'without glibc'}")
    return None


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


def _space_warning(layout: ScopesLayout,
                   request: ScopesRequest) -> str | None:
    needed = raw_size(request.max_scope_records) + \
        request.max_activation_bytes + RESERVE_BYTES
    free = free_bytes(layout.out)
    if free is None or free >= needed:
        return None
    return (f"'{layout.out}' has {free} bytes free, less than the raw file, "
            f"the observation limit of {request.max_activation_bytes} bytes "
            f"and the reserve of {RESERVE_BYTES} bytes")


# --- the Run ----------------------------------------------------------------


def _execute(request: ScopesRequest, layout: ScopesLayout, native,
             cwd: Path, session: str, descriptor: int) -> CaptureRun:
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
                   found: Preflight, capture_id: uuid.UUID) -> Decoded:
    """Decode the saved files; the calls document or the failure."""
    events = _read(layout.events, raw_size(MAX_RECORDS))
    observations = _read(layout.observations,
                         request.max_activation_bytes + (1 << 20))
    try:
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
        if not path.is_file() or (role == "recording"
                                  and not _is_recording(path)):
            rows.append({"role": role, "path": relative.as_posix(),
                         "reason": "not_written"})
    return rows


def _is_recording(path: Path) -> bool:
    """A runner that was killed can leave a Recording file without even
    the MCAP magic: it is not a Recording, so it is `not_written`."""
    try:
        with path.open("rb") as stream:
            return stream.read(len(MCAP_MAGIC)) == MCAP_MAGIC
    except OSError:
        return False


def _artifacts(request: ScopesRequest, layout: ScopesLayout,
               cwd: Path) -> list[dict]:
    return [row for row in artifact_rows(request.target, cwd, _files(layout))
            if row["role"] != "recording"
            or _is_recording(layout.out / row["path"])]


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
            decoded: Decoded) -> dict | None:
    """Write calls.json, then scopes.json last; the report, or None when a
    file cannot be written."""
    try:
        if decoded.calls_bytes is not None:
            write_new_file(layout.calls, decoded.calls_bytes.decode("ascii"))
        report = build_report(request, layout, cwd, found, capture_run,
                              decoded)
        write_new_file(layout.report, encode_report(report).decode("ascii"))
    except (OSError, ValueError) as error:
        sys.stderr.write(f"{PROG}: cannot write the scopes report: {error}\n")
        return None
    return report


def format_summary(report: dict, path: Path) -> str:
    run, capture = report["run"], report["capture"]
    lines = [f"scopes: {path}",
             f"run: {run['status']}" + (
                 f" (exit {run['exit_code']})" if run["exit_code"] is not None
                 else ""),
             f"capture: {capture['status']}" + (
                 f" ({', '.join(capture['reasons'])})" if capture["reasons"]
                 else "")]
    for task in report["summary"]["tasks"]:
        unavailable = ", ".join(f"{reason} {count}" for reason, count in
                                sorted(task["unavailable"].items()))
        lines.append(f"task {task['participant']}/{task['task']}: "
                     f"{task['activations']} Activations, {task['exact']} "
                     "exact" + (f", unavailable: {unavailable}"
                                if unavailable else ""))
    for scope in report["summary"]["scopes"]:
        per = scope["per_exact_activation"]
        lines.append(f"scope {scope['id']} {scope['name']}: "
                     f"{scope['attributed_calls']} calls; per exact "
                     f"Activation min {per['min']}, median {per['median']}, "
                     f"max {per['max']}")
    return "\n".join(lines) + "\n"


# --- main -------------------------------------------------------------------


def run_scopes(request: ScopesRequest, cwd: Path) -> int:
    """The scopes collector after the option checks."""
    try:
        found = preflight(request, cwd)
    except Usage as error:
        return command_error(PROG, str(error), EXIT_USAGE)
    problem = host_problem()
    if problem is not None:
        return command_error(PROG, problem, EXIT_FAILURE)
    try:
        native = find_native_tools()
    except ToolMissing as error:
        return command_error(PROG, str(error), EXIT_NOT_FOUND)
    layout = ScopesLayout(Path(os.path.realpath(cwd / request.target.out)))
    capture_id = uuid.uuid4()
    try:
        layout.scopes.mkdir(parents=True)
        write_new_file(layout.map, found.map_bytes.decode("ascii"))
    except OSError as error:
        return command_error(PROG, f"cannot write '{layout.map}': {error}",
                             EXIT_FAILURE)
    try:
        descriptor = create_raw_file(layout.events, capture_id,
                                     request.max_scope_records)
    except OSError as error:
        return command_error(
            PROG, f"cannot create '{layout.events}': {error}; the Run did "
            "not start", EXIT_FAILURE)
    try:
        warning = _space_warning(layout, request)
        if warning is not None:
            sys.stderr.write(f"{PROG}: warning: {warning}\n")
        layout.activation.mkdir()
        session = session_value(capture_id, request.max_scope_records,
                                descriptor)
        capture_run = _execute(request, layout, native, cwd, session,
                               descriptor)
    finally:
        os.close(descriptor)
    decoded = decode_capture(request, layout, found, capture_id)
    report = publish(request, layout, cwd, found, capture_run, decoded)
    if report is not None:
        sys.stdout.write(format_summary(report,
                                        request.target.out / "scopes.json"))
        sys.stdout.flush()
    return resolve_exit(report, capture_run)
