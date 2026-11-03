#!/usr/bin/env python3
"""Issue #361 experiment. Proof controls only; no production CLI or schema.

One fresh Run/collector per capture. All subprocesses and buffers are bounded.
The runner sends nonblocking notices; only this supervisor waits for perf ack.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import selectors
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time

from sil.manifest import Manifest
from sil.profile_storage import reserve_reached

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
NOTICE = struct.Struct("<QQQQ")
EVENT = "cpu-clock:u"
SAMPLE_OPTIONS = ["-e", EVENT, "-F", "997", "-k", "monotonic",
                  "--call-graph", "dwarf,8192", "--buildid-mmap", "-N", "-q"]
MAX_BYTES = 128 << 20
EXPORT_BYTES = 256 << 20


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def command(argv, out, *, timeout=180):
    """Retain bounded tool output; abort a whole tool session on timeout."""
    with out.open("wb") as output:
        process = subprocess.Popen([str(a) for a in argv], stdout=output,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        until = time.monotonic() + timeout
        while process.poll() is None:
            if time.monotonic() > until or output.tell() > EXPORT_BYTES:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise RuntimeError(f"tool exceeded time/output budget: {argv}")
            time.sleep(.02)
    return process.returncode


def build(work):
    work.mkdir(parents=True, exist_ok=False)
    original = (ROOT / "kernel/src/run_diagnostics.cpp").read_text()
    patched = '#include "notify.hpp"\n' + original
    before = ': task_(task), slot_ns_(slot_ns), start_ns_(now_ns()) {}'
    after = (': task_(task), slot_ns_(slot_ns), start_ns_(now_ns()) {\n'
             '  window_proof::notify(task_, slot_ns_, 0);\n}')
    if patched.count(before) != 1:
        raise ValueError("diagnostics constructor anchor changed")
    patched = patched.replace(before, after)
    before = '  const uint64_t end = now_ns();\n'
    # completed() and the unfinished destructor; not any other measurement.
    for signature in ('void Activation::completed() noexcept {',
                      'Activation::~Activation() {\n  if (done_) return;'):
        anchor = signature + '\n' + before
        if patched.count(anchor) != 1:
            raise ValueError("diagnostics completion anchor changed")
        patched = patched.replace(anchor, signature + '\n'
            '  window_proof::notify(task_, slot_ns_, 1);\n' + before)
    source = work / "run_diagnostics.cpp"
    source.write_text(patched)
    commands = [
        ["cmake", "-S", ROOT, "-B", work / "build", "-DCMAKE_BUILD_TYPE=Release",
         f"-DCMAKE_PROJECT_INCLUDE={HERE / 'overlay.cmake'}",
         f"-DWINDOW_PROOF_SOURCE={source}"],
        ["cmake", "--build", work / "build", "--target", "sil-run",
         "sil-run-diagnostics", "-j2"],
        ["cc", "-O2", "-g", "-fno-omit-frame-pointer", "-fPIC", "-shared",
         "-I", ROOT / "include", HERE / "workload.c", "-o", work / "workload.so"],
    ]
    for index, argv in enumerate(commands):
        if command(argv, work / f"build-{index}.txt", timeout=600):
            raise RuntimeError(f"build failed: {argv}")
    write(work / "commands.json", [[str(a) for a in c] for c in commands])


def manifest(work, variant, fail=False):
    m = Manifest(duration_ns=620_000_000)
    m.add_schemas({"Event": {"fields": [{"name": "task", "type": "u64"},
                                           {"name": "index", "type": "u64"}]}})
    m.add_channel("events", schema="Event")
    if variant == "native":
        m.add_native("work", library=str(work / "workload.so"),
                     publishes=["events"], config={"fail": fail})
    else:
        m.add_process("work", command=[sys.executable, str(HERE / "process.py"),
                      str(work / "workload.so"), "fail" if fail else "pass"],
                      period_ns=1_000_000, publishes=["events"])
    path = work / f"{variant}-{'fail' if fail else 'pass'}.json"
    if not path.exists():
        m.write(path)
    return path


def sample_times(path):
    """perf's --ns time-only sample pass, independent of Activation decoder."""
    result = []
    for line in path.read_text().splitlines():
        match = re.match(r"^\s*(\d+)\.(\d{9}):?\s*$", line)
        if match:
            result.append(int(match[1])*1_000_000_000 + int(match[2]))
    return result


def capture(out, work, variant, mode, *, small=None):
    out.mkdir()
    failing = mode in ("failure", "ordinary-failure")
    source = manifest(work, variant, failing)
    ordinary = mode in ("ordinary", "ordinary-failure")
    measured = mode not in ("ordinary", "ordinary-failure", "diagnostics", "observer")
    window = measured and mode not in ("whole", "limit")
    data = (small / (out.name + ".data")) if small else out / "perf.data"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "python/src")
    env["SIL_WINDOW_MARKERS"] = str(out / "markers.json")
    notices, controls, growth = [], [], []
    started = time.monotonic_ns()
    requested = {"from_ns": 610_000_000, "to_ns": 611_000_000, "task": None}
    if mode == "empty": requested["to_ns"] = requested["from_ns"]
    if mode == "unreached": requested.update(from_ns=900_000_000, to_ns=901_000_000)
    if mode == "exact-peer": requested["task"] = 1 if variant == "native" else 0
    if mode in ("failure", "unclosed"): requested["to_ns"] = 615_000_000
    if mode == "loss": requested.update(from_ns=0, to_ns=620_000_000)
    runner = work / "build" / ("sil-run" if ordinary else "sil-run-diagnostics")
    argv = [str(runner), str(source), "-o", str(out / "run.mcap"),
            "--participant-timeout-ms", "10000"]
    if not ordinary:
        argv += ["--diagnostics-out", str(out / "diagnostics.json"),
                 "--activation-observations", str(out / "activations.jsonl"),
                 "--activation-observations-limit-bytes", str(8 << 20)]
    # Short Unix socket path, regardless of evidence path length.
    with tempfile.TemporaryDirectory(prefix="sil-window-") as temporary, \
            selectors.DefaultSelector() as selector:
        receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        receiver.bind(temporary + "/notice")
        receiver.setblocking(False)
        selector.register(receiver, selectors.EVENT_READ, "notice")
        if mode not in ("ordinary", "diagnostics"):
            env.update(SIL_WINDOW_SOCKET=temporary + "/notice",
                       SIL_WINDOW_OBSERVER=str(out / "observer.json"))
        control_fd = ack_fd = None
        if measured:
            ctl, ack = out / "control.fifo", out / "ack.fifo"
            os.mkfifo(ctl, 0o600)
            os.mkfifo(ack, 0o600)
            control_fd = os.open(ctl, os.O_RDWR | os.O_NONBLOCK)
            ack_fd = os.open(ack, os.O_RDWR | os.O_NONBLOCK)
            selector.register(ack_fd, selectors.EVENT_READ, "ack")
            argv = ["perf", "record", *SAMPLE_OPTIONS,
                    "--control", f"fifo:{ctl},{ack}", "-o", str(data),
                    *(["--delay=-1"] if window else []), "--", *argv]
        write(out / "command.json", argv)
        pending, desired, active, stopped = None, not window, not window, None
        enable_due = disable_due = kill_due = None
        ack_buffer = b""
        last_poll = 0
        with (out / "stdout.txt").open("wb") as stdout, \
                (out / "stderr.txt").open("wb") as stderr:
            process = subprocess.Popen(argv, env=env, stdout=stdout, stderr=stderr,
                                       start_new_session=True)
            try:
                # Deliberately force kernel socket backpressure in the loss case.
                if mode == "loss": time.sleep(.1)
                while True:
                    now = time.monotonic_ns()
                    if now - started > 60_000_000_000:
                        raise RuntimeError("capture exceeded 60 s guard")
                    for key, _ in selector.select(.001):
                        if key.data == "ack":
                            ack_buffer += os.read(ack_fd, 4096)
                            ack_buffer = ack_buffer.lstrip(b"\0")
                            while b"\n" in ack_buffer:
                                line, ack_buffer = ack_buffer.split(b"\n", 1)
                                ack_buffer = ack_buffer.lstrip(b"\0")
                                if line != b"ack" or pending is None:
                                    raise RuntimeError("unexpected perf acknowledgment")
                                pending["ack_ns"] = time.monotonic_ns()
                                active = pending["command"] == "enable"
                                pending = None
                        else:
                            # Drain only a bounded batch so guards/acks get service.
                            for _ in range(256):
                                try: raw = receiver.recv(128)
                                except BlockingIOError: break
                                if len(raw) != NOTICE.size:
                                    raise RuntimeError("bad proof notice")
                                host, vt, task, edge = NOTICE.unpack(raw)
                                notices.append([host, vt, task, edge, time.monotonic_ns()])
                                if len(notices) > 10000:
                                    raise RuntimeError("notice budget exceeded")
                                selected = (requested["from_ns"] <= vt < requested["to_ns"]
                                            and (requested["task"] is None or task == requested["task"]))
                                if window and not stopped:
                                    if requested["task"] is not None:
                                        if selected: desired = edge == 0
                                    elif edge == 0:
                                        desired = selected
                                    if mode == "during" and desired and enable_due is None and not active:
                                        enable_due = time.monotonic_ns() + 20_000_000
                                        desired = False
                                    if mode == "end-during" and desired and disable_due is None:
                                        disable_due = time.monotonic_ns() + 30_000_000
                                    if mode == "unclosed" and desired and kill_due is None:
                                        kill_due = time.monotonic_ns() + 30_000_000
                    now = time.monotonic_ns()
                    if enable_due and now >= enable_due:
                        desired, enable_due = True, None
                    if disable_due and now >= disable_due:
                        desired, disable_due, stopped = False, None, "explicit_host_stop"
                    if kill_due and now >= kill_due:
                        # perf's launched child is the runner. Kill just it;
                        # perf must remain alive to finalize its own capture.
                        children = Path(f"/proc/{process.pid}/task/{process.pid}/children")
                        for pid in children.read_text().split(): os.kill(int(pid), signal.SIGKILL)
                        kill_due = None
                    if measured and now - last_poll >= 100_000_000:
                        last_poll = now
                        size = data.stat().st_size if data.exists() else 0
                        growth.append([now, size])
                        limit = (64 << 10) if mode == "limit" else MAX_BYTES
                        if not stopped and (size > limit or reserve_reached(data.parent)):
                            stopped = "capture_limit" if size > limit else "storage_reserve"
                            desired = False
                    if measured and pending is None and desired != active:
                        # Event-specific control leaves the dummy tracking event on.
                        verb = "enable" if desired else "disable"
                        sent = time.monotonic_ns()
                        os.write(control_fd, f"{verb} {EVENT}\n".encode())
                        pending = {"command": verb, "write_ns": sent, "ack_ns": None}
                        controls.append(pending)
                    if process.poll() is not None:
                        break
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                receiver.close()
                if control_fd is not None: os.close(control_fd)
                if ack_fd is not None: os.close(ack_fd)
                for name in ("control.fifo", "ack.fifo"):
                    (out / name).unlink(missing_ok=True)
    result = {"variant": variant, "mode": mode, "requested": requested if window else None,
              "exit_code": process.returncode, "elapsed_ns": time.monotonic_ns()-started,
              "controls": controls, "stop_reason": stopped,
              "growth": growth, "perf_bytes": data.stat().st_size if data.exists() else 0,
              "recording_sha256": digest(out / "run.mcap") if (out / "run.mcap").exists() else None}
    write(out / "notices.json", notices)
    if (out / "observer.json").exists():
        result["observer"] = json.loads((out / "observer.json").read_text())
        result["observer"]["received"] = len(notices)
    if measured and data.exists():
        passes = {
            "samples.txt": ["script", "--ns", "-F", "time", "--no-call-graph"],
            "metadata.txt": ["script", "--ns", "--show-mmap-events", "--show-task-events",
                             "--show-lost-events", "--no-call-graph"],
            "stats.txt": ["report", "--stdio", "--stats"],
            "events.txt": ["evlist", "-v"],
            "buildids.txt": ["buildid-list", "--with-hits"],
        }
        result["exports"] = {}
        for name, args in passes.items():
            result["exports"][name] = command(["perf", *args, "-i", data], out / name)
        times = sample_times(out / "samples.txt")
        write(out / "sample-times.json", times)
        result["samples"] = {"count": len(times), "first_ns": min(times) if times else None,
                             "last_ns": max(times) if times else None}
        # Retain raw observations; no inference that ack equals a sample edge.
        metadata = (out / "metadata.txt").read_text(errors="replace")
        first_enable = next((c["write_ns"] for c in controls if c["command"] == "enable"), None)
        result["metadata"] = {
            "exec_records": metadata.count("PERF_RECORD_COMM exec"),
            "fork_records": metadata.count("PERF_RECORD_FORK"),
            "exit_records": metadata.count("PERF_RECORD_EXIT"),
            "workload_mapping": "workload.so" in metadata,
            "first_enable_write_ns": first_enable,
            "lost_records": metadata.count("PERF_RECORD_LOST"),
            "throttle_records": metadata.count("PERF_RECORD_THROTTLE"),
        }
    write(out / "result.json", result)
    return result


def qualify(out, work, environment, small):
    out.mkdir(parents=True, exist_ok=False)
    identities = {"environment": environment, "uname": platform.uname()._asdict(),
                  "libc": platform.libc_ver(), "revision": os.environ.get("SIL_SOURCE_REVISION"),
                  "perf_event_paranoid": Path("/proc/sys/kernel/perf_event_paranoid").read_text().strip()}
    for label, args in {"perf": ["perf", "version"], "compiler": ["cc", "--version"],
                        "packages": ["dpkg-query", "-W"], "buildids": ["readelf", "-n", work / "workload.so"]}.items():
        command(args, out / f"{label}.txt")
    identities["binaries"] = {str(p): digest(p) for p in
        (work / "workload.so", work / "build/sil-run", work / "build/sil-run-diagnostics")}
    write(out / "identities.json", identities)
    # Retain permission/feature availability before the experiment. No sysctl here.
    probe = command(["perf", "record", *SAMPLE_OPTIONS, "--delay=-1", "-o",
                     out / "probe.data", "--", "/bin/true"], out / "availability.txt")
    write(out / "availability.json", {"exit_code": probe, "available": probe == 0})
    if probe:
        write(out / "results.json", {"decision": "unavailable", "runs": []})
        return
    results = []
    for variant in ("native", "process"):
        baseline = failure_baseline = None
        modes = ("ordinary", "diagnostics", "observer", "whole", "window", "exact-peer",
                 "empty", "unreached", "during", "end-during", "ordinary-failure", "failure", "unclosed",
                 "loss", "limit")
        for mode in modes:
            repeats = 3 if mode in ("ordinary", "diagnostics", "observer", "whole", "window") else 1
            for repeat in range(repeats):
                row = capture(out / f"{variant}-{mode}-{repeat}", work, variant, mode)
                if mode == "ordinary": baseline = row["recording_sha256"]
                if mode == "ordinary-failure":
                    failure_baseline = row["recording_sha256"]
                    if row["exit_code"] != 1: raise RuntimeError("ordinary failure did not fail")
                if mode == "failure":
                    row["recording_equal"] = row["recording_sha256"] == failure_baseline
                    if row["exit_code"] != 1 or not row["recording_equal"]:
                        raise RuntimeError("observed failure changed outcome/Recording")
                if mode not in ("ordinary-failure", "failure", "unclosed"):
                    row["recording_equal"] = row["recording_sha256"] == baseline
                    if row["exit_code"] or not row["recording_equal"]:
                        raise RuntimeError(f"outcome/Recording changed: {variant} {mode}")
                results.append(row)
                write(out / "results.json", {"decision": "pending_analysis", "runs": results})
        if small:
            row = capture(out / f"{variant}-reserve", work, variant, "window", small=small)
            row["recording_equal"] = row["recording_sha256"] == baseline
            results.append(row)
            write(out / "results.json", {"decision": "pending_analysis", "runs": results})
    write(out / "digests.json", {str(p.relative_to(out)): digest(p)
                                for p in out.rglob("*") if p.is_file()})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "run"))
    parser.add_argument("work", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--environment", default="unspecified")
    parser.add_argument("--small-dir", type=Path)
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() != "x86_64" or platform.libc_ver()[0] != "glibc":
        parser.error("qualification requires Linux x86-64/glibc")
    if args.action == "build": build(args.work.resolve())
    else:
        if args.out is None: parser.error("run requires --out")
        qualify(args.out.resolve(), args.work.resolve(), args.environment, args.small_dir)
