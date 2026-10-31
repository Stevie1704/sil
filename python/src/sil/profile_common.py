"""What the CPU and the heap collector of `sil profile` share (#313).

The Run outcome of a capture, the identity and host members of the
report, the artifact rows of the profile directory, the capture status
and the exit code. Each collector module adds its own backend, layout
and capture members.
"""

from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path

from sil.bundle import file_sha256
from sil.diagnostics_run import (
    EXIT_FAILURE, EXIT_NOT_FOUND, EXIT_OK, Guards, RunRequest, alias_problems,
)

PROG = "sil profile"
SYMBOLIZATION_TIMEOUT_S = 600
POLL_S = 0.1
STDERR_TAIL_BYTES = 64 * 1024
RUN_STATUSES = {0: "succeeded", 1: "run_failure", 2: "configuration_error"}


@dataclass(frozen=True)
class RunTarget:
    """The Run that a collector profiles, and where its files go: what
    the requests of both collectors share."""

    manifest: Path
    out: Path  # the profile directory
    recording: Path | None  # None: --no-recording
    provenance: Path | None
    guards: Guards
    provenance_mode: str = "full"

    def run_request(self, diagnostics: Path) -> RunRequest:
        """The diagnostics Run of this target."""
        return RunRequest(self.manifest, diagnostics, self.recording,
                          self.provenance, self.guards,
                          self.provenance_mode)


def preflight(target: RunTarget, cwd: Path) -> list[str]:
    """The usage problems of the outputs, before anything starts."""
    problems = []
    out = cwd / target.out
    if os.path.lexists(out):
        problems.append(f"the profile directory '{target.out}' already "
                        "exists; earlier evidence is never replaced")
    elif not Path(os.path.realpath(out)).parent.is_dir():
        problems.append(f"the profile directory '{target.out}': its parent "
                        "directory does not exist")
    if target.recording is not None and _same(
            cwd / target.recording, cwd / target.manifest):
        problems.append(f"the Recording '{target.recording}' is the "
                        "Manifest")
    if target.provenance is not None:
        recording = target.run_request(out).recording_path
        others = [("Manifest", target.manifest),
                  ("failure report", Path(f"{recording}.failure.json"))]
        if target.recording is not None:
            others.append(("Recording", target.recording))
        problems += alias_problems(
            [("provenance record", target.provenance)], others, cwd)
    return problems


def _same(a: Path, b: Path) -> bool:
    if os.path.realpath(a) == os.path.realpath(b):
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


@dataclass(frozen=True)
class CaptureRun:
    """What the collector, the launcher and the runner left."""

    command: list[str]
    perf_status: int  # perf's exit code, or -N when signal N ended it
    launcher_error: str | None
    supervisor_signal: int | None
    limit_reached: bool
    report: dict | None
    reserve_reached: bool = False  # the free-space reserve stopped sampling


def report_launcher_error(error: str | None) -> str | None:
    """Say on stderr that the launcher failed and no Run took place."""
    if error is not None:
        sys.stderr.write(f"{PROG}: sil-diagnostics-launcher failed, so no "
                         f"Run took place: "
                         f"{error.removeprefix('sil-diagnostics-launcher: ')}"
                         "\n")
    return error


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


def identity(target: RunTarget, report: dict | None) -> dict:
    """The `identity` member: from the diagnostics report of the Run."""
    keys = ("manifest_hash", "sil_version", "source_repository",
            "source_revision")
    if report is None:
        return {"manifest_path": str(target.manifest),
                **{key: None for key in keys},
                "unavailable": {key: "no_diagnostics_report" for key in keys}}
    source = report["identity"]
    identity = {"manifest_path": str(target.manifest),
                **{key: source.get(key) for key in keys}}
    identity["unavailable"] = {key: source.get("unavailable", {}).get(
        key, "not_reported") for key in keys if identity[key] is None}
    return identity


def host() -> dict:
    paranoid = perf_event_paranoid()
    uname = platform.uname()
    return {"os": uname.system, "kernel_release": uname.release,
            "architecture": uname.machine, "perf_event_paranoid": paranoid,
            "unavailable": {} if paranoid is not None else {
                "perf_event_paranoid": "not_reported_by_os"}}


def perf_event_paranoid() -> int | None:
    try:
        return int(Path("/proc/sys/kernel/perf_event_paranoid").read_text())
    except (OSError, ValueError):
        return None


def tool_environment() -> dict:
    """The environment of perf's own analysis commands, not of the Run:
    no debuginfod network lookup."""
    env = dict(os.environ)
    env.pop("DEBUGINFOD_URLS", None)
    return env


def artifact_rows(target: RunTarget, cwd: Path, collected: list) -> list:
    """`{role, path, bytes, sha256}` of every existing file in `out`: the
    diagnostics report, the Recording and the collector's files."""
    out = Path(os.path.realpath(cwd / target.out))
    candidates = [("diagnostics_report", out / "run.diagnostics.json")]
    if target.recording is not None:
        candidates.append(("recording", cwd / target.recording))
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
                         "sha256": file_sha256(path)})
    return rows


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def capture_outcome(reasons: list[str],
                    failing: set[str]) -> tuple[str, list[str]]:
    """The capture status that its reasons give: `failed` for a failing
    reason, then `interrupted`, then `partial` for any other reason."""
    if failing & set(reasons):
        return "failed", reasons
    if "interrupted" in reasons:
        return "interrupted", reasons
    return ("partial" if reasons else "complete"), reasons
