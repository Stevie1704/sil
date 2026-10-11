"""`sil bundle matrix`: run a list of sealed regression bundles as one CI job (issue #202).

A case list names each case, the sealed bundle it runs and the lock digest
`sil bundle seal` printed for it. The lock pins the bundle's Manifests,
targets, references and comparison contracts, so it pins the comparison
policy too. Each case states a whole-case wall-clock guard:

    {"sil_matrix": 1,
     "cases": [{"name": "library", "bundle": "/bundles/library",
                "expect_lock": "<sha256>", "timeout_s": 300,
                "required": true}]}

    sil bundle matrix cases.json -o <out> [--jobs N] [--fail-fast]

Every case is one `sil bundle run` into its own evidence directory,
`<out>/cases/<name>/`, in its own process group. Case names are unique
without regard to case, so no two cases share a directory on any file
system. At most `--jobs` cases run at the same time; a Run itself is never
parallelized. The Process response deadlines are the ones each bundle
declares (`runs[].participant_timeout_ms`).

A case that exceeds its guard, or a matrix that receives SIGINT, SIGHUP or
SIGTERM, has its process group sent SIGTERM. `sil-run` then ends its Run and
terminates the process groups of its Process participants. `sil bundle` then
writes the Runs that finished. The Process participant groups stay in the
session of the case, so after the grace period, and after every case, each
process still in that session is killed.

Each case gets one status: `pass`, `behavioral-failure`, `manifest-error`,
`determinism-violation`, `timeout` or `skipped`. The summary keeps the
`sil bundle` exit code, each Run's `sil-run` exit code, and the evidence
paths. Its `identity` holds only deterministic results; the duration and
the peak resident set size are in `observations`.

Without `--fail-fast`, every case runs (complete matrix). With it, no case
starts after a required case fails; the cases that already run finish, and
the cases not started are `skipped`. Exit 0 when every required case
passes, 1 when one does not, 2 when the case list or the output directory
is refused, and 130 when the matrix is interrupted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ElementTree
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from sil.bundle import EXIT_FAIL, EXIT_REFUSED, SUMMARY, _NAME, Refusal, file_sha256

PROG = "sil bundle matrix"
# The JUnit suite name CI dashboards key on; it stays as the first release wrote it.
SUITE = "sil-matrix"
FORMAT = 1
JUNIT = "junit.xml"
EXIT_INTERRUPTED = 130
PASS = "pass"
BEHAVIORAL_FAILURE = "behavioral-failure"
MANIFEST_ERROR = "manifest-error"
DETERMINISM_VIOLATION = "determinism-violation"
TIMEOUT = "timeout"
SKIPPED = "skipped"
# The most severe status a Run of a case has decides the case.
_SEVERITY = (MANIFEST_ERROR, DETERMINISM_VIOLATION, BEHAVIORAL_FAILURE, PASS)
_JUNIT_ELEMENT = {BEHAVIORAL_FAILURE: "failure", DETERMINISM_VIOLATION: "failure",
                  MANIFEST_ERROR: "error", TIMEOUT: "error", SKIPPED: "skipped"}
# How long `sil-run` has to end its Run after SIGTERM before its session is killed.
TERMINATION_GRACE_S = 10.0
_POLL_S = 0.05
# How a case process ended, beside TIMEOUT.
_EXITED, _INTERRUPTED = "exited", "interrupted"


@dataclass(frozen=True)
class Case:
    name: str
    bundle: Path
    expect_lock: str
    timeout_s: float
    required: bool


def read_cases(path: Path) -> tuple[Case, ...]:
    try:
        doc = json.loads(path.read_bytes())
    except OSError as error:
        raise Refusal(f"cannot read the case list {path}: {error.strerror}")
    except ValueError as error:
        raise Refusal(f"{path} is not JSON: {error}")
    if not isinstance(doc, dict) or doc.keys() != {"sil_matrix", "cases"}:
        raise Refusal(f"{path} must hold exactly 'sil_matrix' and 'cases'")
    if doc["sil_matrix"] != FORMAT:
        raise Refusal(f"sil_matrix must be {FORMAT}")
    if not isinstance(doc["cases"], list) or not doc["cases"]:
        raise Refusal("cases must list at least one case")
    base = path.resolve().parent
    cases = tuple(_case(item, index, base) for index, item in enumerate(doc["cases"]))
    seen = set()
    for case in cases:
        if case.name.casefold() in seen:
            raise Refusal(f"cases names case {case.name!r} twice")
        seen.add(case.name.casefold())
    return cases


def _case(value, index: int, base: Path) -> Case:
    context = f"cases[{index}]"
    required = {"name", "bundle", "expect_lock", "timeout_s"}
    if not isinstance(value, dict) or not required <= value.keys():
        raise Refusal(f"{context} must be an object with {sorted(required)}")
    unknown = sorted(value.keys() - required - {"required"})
    if unknown:
        raise Refusal(f"{context} has unknown keys {unknown}")
    name = value["name"]
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise Refusal(f"{context}.name {name!r} must be letters, digits, '.', '_' or '-'")
    if not isinstance(value["bundle"], str) or not value["bundle"]:
        raise Refusal(f"{context}.bundle must name a sealed bundle directory")
    lock = value["expect_lock"]
    if not isinstance(lock, str) or len(lock) != 64:
        raise Refusal(f"{context}.expect_lock must be the lock sha256 seal printed")
    timeout = value["timeout_s"]
    if type(timeout) not in (int, float) or not 0 < timeout < math.inf:
        raise Refusal(f"{context}.timeout_s must be a positive, finite number of seconds")
    is_required = value.get("required", True)
    if not isinstance(is_required, bool):
        raise Refusal(f"{context}.required must be true or false")
    return Case(name, (base / value["bundle"]).resolve(), lock, float(timeout),
                is_required)


def _output_directory(out: Path, cases: tuple[Case, ...]) -> Path:
    out = out.resolve()
    for case in cases:
        if out.is_relative_to(case.bundle) or case.bundle.is_relative_to(out):
            raise Refusal(f"the output directory {out} must be outside the bundle "
                          f"of case {case.name}")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise Refusal(f"the output directory {out} must be new or empty")
    return out


def classify(bundle_exit_code: int | None, bundle_summary: dict | None) -> tuple[str, str]:
    """The status of a finished case, and the reason for a status other than pass."""
    if bundle_summary is None:
        return MANIFEST_ERROR, f"sil bundle exited {bundle_exit_code} without a summary"
    if bundle_summary["verdict"] == "refused":
        return MANIFEST_ERROR, bundle_summary["refusal"]
    worst = (PASS, "")
    for run in bundle_summary["runs"]:
        status, reason = _run_status(run)
        if _SEVERITY.index(status) < _SEVERITY.index(worst[0]):
            worst = (status, f"run {run['name']}: {reason}")
    if worst[0] == PASS and bundle_summary.get("bundle_unchanged") is False:
        return BEHAVIORAL_FAILURE, "the bundle changed during its Runs"
    return worst


def _run_status(run: dict) -> tuple[str, str]:
    status = _exit_status(run["exit_code"])
    if status != PASS:
        return status, f"sil-run exited {run['exit_code']}"
    determinism = run["determinism"]
    if determinism is not None and determinism["verdict"] != PASS:
        second = determinism["second_exit_code"]
        if second != 0:
            return _exit_status(second), f"the repeated Run exited {second}"
        return DETERMINISM_VIOLATION, "the two Recordings differ"
    failed = sorted(n for n, c in run["comparisons"].items() if c["verdict"] != PASS)
    if failed:
        return BEHAVIORAL_FAILURE, f"comparison {', '.join(failed)} failed"
    return PASS, ""


def _exit_status(code: int | None) -> str:
    if code == 0:
        return PASS
    # A runner that cannot start is an environment problem, as exit 2 is.
    return MANIFEST_ERROR if code is None or code == EXIT_REFUSED else BEHAVIORAL_FAILURE


class Matrix:
    """Runs the cases with bounded concurrency and records one result each."""

    def __init__(self, cases: tuple[Case, ...], out: Path, jobs: int, fail_fast: bool):
        self.cases = cases
        self.out = out
        self.jobs = jobs
        self.fail_fast = fail_fast
        self.interrupted = threading.Event()
        # Held to decide whether a case starts, and to stop the starts, so no
        # case starts after a required case failed (fail-fast).
        self._starting = threading.Lock()
        self._stop_starting = False

    def run(self) -> list[dict]:
        (self.out / "cases").mkdir(parents=True)
        (self.out / "logs").mkdir()
        with ThreadPoolExecutor(max_workers=self.jobs) as pool:
            futures = [pool.submit(self._run_case, case) for case in self.cases]
            return [future.result() for future in futures]

    def _run_case(self, case: Case) -> dict:
        evidence = self.out / "cases" / case.name
        with self._starting:
            if self.interrupted.is_set():
                return _skipped(case, "interrupted")
            if self._stop_starting:
                return _skipped(case, "fail-fast")
            started = time.monotonic()
            try:
                proc = _start(case, evidence, self.out / "logs" / f"{case.name}.log")
            except OSError as error:
                proc = None
                result = _unstarted(case, f"cannot start sil bundle: {error}")
        if proc is not None:
            result = self._finish(case, proc, evidence, started)
        if self.fail_fast and case.required and result["status"] != PASS:
            with self._starting:
                self._stop_starting = True
        return result

    def _finish(self, case: Case, proc: subprocess.Popen, evidence: Path,
                started: float) -> dict:
        code, usage, ended = _wait(proc, started + case.timeout_s, self.interrupted)
        observations = {"duration_s": round(time.monotonic() - started, 3),
                        "max_rss_kib": _kib(usage.ru_maxrss) if usage else None}
        # A terminated sil bundle still writes the Runs that finished.
        bundle_summary = _read_summary(evidence / SUMMARY)
        if ended == _INTERRUPTED:
            status, reason = SKIPPED, ("interrupted after it started; its Run "
                                       "processes were terminated")
        elif ended == TIMEOUT:
            status, reason = TIMEOUT, (f"exceeded its {case.timeout_s:g} s guard; "
                                       "its Run processes were terminated")
        else:
            status, reason = classify(code, bundle_summary)
        identity = {"status": status, "bundle_exit_code": code,
                    "summary_sha256": file_sha256(evidence / SUMMARY)
                    if bundle_summary is not None else None}
        return _result(case, True, status, reason, code, bundle_summary, identity,
                       observations)


def _start(case: Case, evidence: Path, log: Path) -> subprocess.Popen:
    command = [sys.executable, "-m", "sil.bundle", "run", str(case.bundle),
               "-o", str(evidence), "--expect-lock", case.expect_lock]
    with log.open("wb") as output:
        # Its own session: the group holds sil bundle and sil-run, and
        # sil-run leads the groups of its Process participants.
        return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output,
                                stderr=subprocess.STDOUT, start_new_session=True)


def _result(case: Case, started: bool, status: str, reason: str, code: int | None,
            bundle_summary: dict | None, identity: dict, observations: dict) -> dict:
    """One case entry of the summary; the skipped and the executed cases share it."""
    result = {"name": case.name, "required": case.required,
              "evidence": f"cases/{case.name}" if started else None,
              "log": f"logs/{case.name}.log" if started else None,
              "status": status, "bundle_exit_code": code}
    if status != PASS:
        result["reason"] = reason
    result["runs"] = [_run_entry(run) for run in (bundle_summary or {}).get("runs", [])]
    result.update(identity=identity, identity_sha256=_digest(identity),
                  observations=observations)
    return result


def _wait(proc: subprocess.Popen, deadline: float, interrupted: threading.Event):
    """Wait for the case, ending its process group at the guard or on interrupt."""
    while True:
        pid, status, usage = os.wait4(proc.pid, os.WNOHANG)
        if pid:
            proc.returncode = os.waitstatus_to_exitcode(status)
            _kill_session(proc.pid)
            return proc.returncode, usage, _EXITED
        if interrupted.is_set() or time.monotonic() >= deadline:
            ended = _INTERRUPTED if interrupted.is_set() else TIMEOUT
            usage = _terminate(proc)
            return proc.returncode, usage, ended
        time.sleep(_POLL_S)


def _terminate(proc: subprocess.Popen):
    """SIGTERM the case group, wait for its session to empty, then kill the rest.

    `start_new_session` makes the case the leader of a session. Process
    participants lead their own groups, but they stay in that session."""
    session = proc.pid
    _signal_group(session, signal.SIGTERM)
    usage = None
    deadline = time.monotonic() + TERMINATION_GRACE_S
    while time.monotonic() < deadline:
        usage = _reap(proc) or usage
        if proc.returncode is not None and not _session_members(session):
            break
        time.sleep(_POLL_S)
    _kill_session(session)
    if proc.returncode is None:
        _, status, usage = os.wait4(proc.pid, 0)
        proc.returncode = os.waitstatus_to_exitcode(status)
    return usage


def _reap(proc: subprocess.Popen):
    if proc.returncode is not None:
        return None
    pid, status, usage = os.wait4(proc.pid, os.WNOHANG)
    if not pid:
        return None
    proc.returncode = os.waitstatus_to_exitcode(status)
    return usage


def _signal_group(group: int, signal_number: int) -> None:
    try:
        os.killpg(group, signal_number)
    except ProcessLookupError:
        pass


def _kill_session(session: int) -> None:
    """SIGKILL every process left in the session, and wait until they are gone.

    The wait is bounded: an orphan nobody reaps stays in it as a zombie."""
    deadline = time.monotonic() + 1.0
    while (members := _session_members(session)) and time.monotonic() < deadline:
        for pid in members:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(_POLL_S)


def _session_members(session: int) -> list[int]:
    members = []
    for pid in _process_ids():
        try:
            if os.getsid(pid) == session:
                members.append(pid)
        except (ProcessLookupError, PermissionError):
            pass
    return members


def _process_ids() -> list[int]:
    proc = Path("/proc")
    if proc.is_dir():
        return [int(entry.name) for entry in proc.iterdir() if entry.name.isdigit()]
    # No /proc on macOS, where the suite also runs; the runtime image has no ps.
    listing = subprocess.run(["/bin/ps", "-A", "-o", "pid="], capture_output=True,
                             text=True).stdout
    return [int(pid) for pid in listing.split()]


def _kib(maxrss: int) -> int:
    # Linux reports kibibytes, macOS bytes.
    return maxrss // 1024 if sys.platform == "darwin" else maxrss


def _read_summary(path: Path) -> dict | None:
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None


def _run_entry(run: dict) -> dict:
    status, _ = _run_status(run)
    return {"name": run["name"], "status": status, "exit_code": run["exit_code"],
            "recording_sha256": run["recording_sha256"]}


def _skipped(case: Case, reason: str) -> dict:
    return _not_run(case, SKIPPED, reason)


def _unstarted(case: Case, reason: str) -> dict:
    """A case whose sil bundle could not start: an environment problem."""
    return _not_run(case, MANIFEST_ERROR, reason)


def _not_run(case: Case, status: str, reason: str) -> dict:
    identity = {"status": status, "bundle_exit_code": None, "summary_sha256": None}
    return _result(case, False, status, reason, None, None, identity,
                   {"duration_s": None, "max_rss_kib": None})


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def verdict(results: list[dict], interrupted: bool) -> str:
    if interrupted:
        return _INTERRUPTED
    return PASS if all(r["status"] == PASS for r in results if r["required"]) else "fail"


def write_summary(out: Path, results: list[dict], matrix_verdict: str,
                  jobs: int, fail_fast: bool) -> dict:
    counts = {status: 0 for status in (*_SEVERITY, TIMEOUT, SKIPPED)}
    for result in results:
        counts[result["status"]] += 1
    # Paths are relative to the output directory, so the summary is shareable.
    summary = {"sil_matrix_evidence": FORMAT, "verdict": matrix_verdict,
               "jobs": jobs, "fail_fast": fail_fast, "counts": counts,
               "cases": results}
    (out / SUMMARY).write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")
    return summary


def write_junit(out: Path, results: list[dict]) -> None:
    def count(kind: str) -> str:
        return str(sum(_JUNIT_ELEMENT.get(r["status"]) == kind for r in results))

    root = ElementTree.Element("testsuites")
    suite = ElementTree.SubElement(
        root, "testsuite", name=SUITE, tests=str(len(results)),
        failures=count("failure"), errors=count("error"), skipped=count("skipped"))
    for result in results:
        testcase = ElementTree.SubElement(
            suite, "testcase", classname=SUITE, name=result["name"],
            time=str(result["observations"]["duration_s"] or 0))
        element = _JUNIT_ELEMENT.get(result["status"])
        if element:
            required = "" if result["required"] else " (optional case)"
            ElementTree.SubElement(testcase, element, type=result["status"],
                                   message=result["reason"] + required)
        if result["evidence"]:
            ElementTree.SubElement(testcase, "system-out").text = (
                f"evidence {result['evidence']}\nlog {result['log']}\n")
    ElementTree.indent(root)
    ElementTree.ElementTree(root).write(out / JUNIT, encoding="utf-8",
                                        xml_declaration=True)


def render(summary: dict) -> str:
    width = max(len(r["name"]) for r in summary["cases"])
    lines = []
    for result in summary["cases"]:
        line = f"{result['status']:<21} {result['name']:<{width}}"
        if not result["required"]:
            line += " (optional)"
        if result["status"] != PASS:
            line += f"  {result['reason'].splitlines()[0]}"
        if result["evidence"]:
            line += f"  [{result['evidence']}]"
        lines.append(line.rstrip())
    counts = ", ".join(f"{n} {s}" for s, n in summary["counts"].items() if n)
    lines.append(f"matrix: {summary['verdict']} ({counts})")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=PROG, description="Run a list of sealed regression bundles, each "
                               "into its own evidence directory, and summarize them.",
        allow_abbrev=False,
    )
    parser.add_argument("cases", type=Path, help="the case list (sil_matrix 1)")
    parser.add_argument("-o", "--out", type=Path, required=True,
                        help="a new or empty directory outside every bundle")
    parser.add_argument("--jobs", type=int, default=1,
                        help="the most cases that run at the same time (default 1)")
    parser.add_argument("--fail-fast", action="store_true",
                        help="start no case after a required case fails")
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    try:
        cases = read_cases(args.cases)
        out = _output_directory(args.out, cases)
    except Refusal as refusal:
        sys.stderr.write(f"{PROG}: refused: {refusal}\n")
        return EXIT_REFUSED
    matrix = Matrix(cases, out, args.jobs, args.fail_fast)
    for signal_number in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM):
        signal.signal(signal_number, lambda *_: matrix.interrupted.set())
    results = matrix.run()
    interrupted = matrix.interrupted.is_set()
    summary = write_summary(out, results, verdict(results, interrupted),
                            args.jobs, args.fail_fast)
    write_junit(out, results)
    sys.stdout.write(render(summary))
    if interrupted:
        return EXIT_INTERRUPTED
    return 0 if summary["verdict"] == PASS else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
