"""A stand-in for Linux perf, for the `sil profile` orchestration tests.

It implements only what `sil profile` calls, with the process behavior of
perf 6.1 (tools/perf/builtin-record.c): `record` forks the workload after
`--`, ends as the workload ended (its exit code, or the signal that ended
it), and on SIGINT or SIGTERM sends SIGTERM to the workload, waits for it
and then ends by the signal it got. It opens `--control fifo:PATH` and
records a `disable` command in the output file. It samples nothing.

The FAKE_PERF_* variables select failures and the canned export text.
Mock-only success does not qualify a backend: the real perf run is
tests/test_profile_perf_integration.py.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path


def option(arguments: list[str], name: str) -> str | None:
    if name in arguments:
        return arguments[arguments.index(name) + 1]
    return None


def text_file(variable: str, default: str = "") -> str:
    path = os.environ.get(variable)
    return Path(path).read_text() if path else default


def run_directory_text(text: str, arguments: list[str]) -> str:
    """`{RETAINED:NAME}` is the path that perf recorded for the file NAME
    that the Run working directory held: the Run working directory is gone,
    its copy is the `.run-directory` beside perf.data."""
    def path(match: re.Match) -> str:
        retained = Path(option(arguments, "-i")).parent / ".run-directory"
        found = next(retained.rglob(match[1]), None)
        relative = found.relative_to(retained) if found else match[1]
        return str(Path(os.path.realpath(Path.cwd())) / ".sil-run-fake"
                   / relative)
    return re.sub(r"\{RETAINED:([^}]+)\}", path, text)


def record(arguments: list[str]) -> int:
    if os.environ.get("FAKE_PERF_DENY"):
        sys.stderr.write("Error:\nNo permission to enable cpu-clock:u event."
                         "\n\nAccess to performance monitoring and "
                         "observability operations is limited.\n")
        return 255
    out = Path(option(arguments, "-o"))
    control = option(arguments, "--control")
    workload = arguments[arguments.index("--") + 1:]
    log = Path(os.environ["FAKE_PERF_LOG"]) if "FAKE_PERF_LOG" in \
        os.environ else None
    probe = Path(workload[0]).name == "true"
    if log is not None and not probe:
        with log.open("a") as stream:
            stream.write(json.dumps({"record": arguments,
                                     "env": sorted(os.environ)}) + "\n")
    fifo = None
    if control is not None:
        fifo = os.open(control.removeprefix("fifo:"),
                       os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
    received: list[int] = []

    def on_signal(number, _frame):
        received.append(number)

    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, on_signal)
    child = subprocess.Popen(workload, close_fds=False)
    grow = 0 if probe else int(os.environ.get("FAKE_PERF_GROW", "0"))
    state = {"disabled": False}
    with out.open("wb") as data:
        data.write(b"FAKEPERF")
        data.flush()
        while child.poll() is None:
            if received:
                child.send_signal(signal.SIGTERM)
                child.wait()
                break
            if grow and not state["disabled"]:
                data.write(b"\0" * 65536)
                data.flush()
            if fifo is not None:
                try:
                    if b"disable" in os.read(fifo, 64):
                        state["disabled"] = True
                except BlockingIOError:
                    pass
            time.sleep(0.01)
        # Growth goes on until the supervisor disables sampling, so its
        # size poll sees the limit also when the Run is short.
        deadline = time.monotonic() + 10
        while grow and not state["disabled"] and time.monotonic() < deadline:
            data.write(b"\0" * 65536)
            data.flush()
            try:
                state["disabled"] = b"disable" in os.read(fifo, 64)
            except BlockingIOError:
                time.sleep(0.01)
        data.write(json.dumps(state).encode())
    status = child.wait()
    if received:
        signal.signal(received[0], signal.SIG_DFL)
        os.kill(os.getpid(), received[0])
    if os.environ.get("FAKE_PERF_RECORD_EXIT") and not probe:
        return int(os.environ["FAKE_PERF_RECORD_EXIT"])
    if status < 0:
        signal.signal(-status, signal.SIG_DFL)
        os.kill(os.getpid(), -status)
    return status


def script(arguments: list[str]) -> int:
    if os.environ.get("FAKE_PERF_SCRIPT_FAIL"):
        sys.stderr.write("perf script: failed to process sample\n")
        return 1
    if "-G" in arguments:
        if os.environ.get("FAKE_PERF_SRCLINE_SLEEP"):
            time.sleep(float(os.environ["FAKE_PERF_SRCLINE_SLEEP"]))
        sys.stdout.write(text_file("FAKE_PERF_SRCLINES"))
    else:
        sys.stdout.write(run_directory_text(text_file("FAKE_PERF_SCRIPT"),
                                            arguments))
    return 0


def main() -> int:
    arguments = sys.argv[1:]
    if arguments == ["--version"]:
        print("perf version 6.99.0-fake")
        return 0
    command, rest = arguments[0], arguments[1:]
    if command == "record":
        return record(rest)
    if command == "script":
        return script(rest)
    if command == "buildid-list":
        sys.stdout.write(run_directory_text(
            text_file("FAKE_PERF_BUILDIDS"), rest))
        return 0
    if command == "buildid-cache":
        cache = Path(os.environ["PERF_BUILDID_DIR"])
        cache.mkdir(parents=True, exist_ok=True)
        added = Path(option(rest, "-a"))
        (cache / added.name).write_bytes(added.read_bytes())
        return 0
    if command == "report":
        sys.stdout.write(text_file("FAKE_PERF_STATS", "Aggregated stats:\n"))
        return 0
    sys.stderr.write(f"fake perf: unknown command {command}\n")
    return 129


if __name__ == "__main__":
    sys.exit(main())
