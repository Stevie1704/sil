"""Finite perf session owned independently of the Run (#362 proof only)."""
from __future__ import annotations

import os
from pathlib import Path
import resource
import select
import signal
import subprocess
import time

EVENTS = ("sched_switch", "sched_wakeup", "sched_wakeup_new", "sched_process_fork",
          "sched_process_exec", "sched_process_exit")
FILE_BYTES = 64 << 20
GUARD_BYTES = 32 << 20
GUARD_SECONDS = 30


def limits():
    resource.setrlimit(resource.RLIMIT_FSIZE, (FILE_BYTES, FILE_BYTES))


def command(argv, out: Path, *, timeout=120):
    start = time.monotonic_ns()
    with out.open("wb") as log:
        process = subprocess.Popen(list(map(str, argv)), stdout=log,
            stderr=subprocess.STDOUT, start_new_session=True, preexec_fn=limits)
        try:
            process.wait(timeout=timeout)
        except BaseException:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
    return {"argv": list(map(str, argv)), "exit_code": process.returncode,
            "wall_ns": time.monotonic_ns() - start, "log": out.name}


class Perf:
    def __init__(self, out: Path, *, pages="1M"):
        self.out = out
        self.log = (out / "collector.txt").open("wb")
        control_read, self.control = os.pipe()
        self.ack, ack_write = os.pipe()
        self.commands = []
        self.pages = pages
        self.argv = ["perf", "record", "-a", "-k", "mono", "-m", pages,
            "--delay=-1", "--control", f"fd:{control_read},{ack_write}",
            "-o", str(out / "perf.data")]
        for name in EVENTS:
            self.argv += ["-e", "sched:" + name]
        self.argv += ["--", "sleep", "35"]
        self.start_ns = time.monotonic_ns()
        self.process = subprocess.Popen(self.argv, stdout=self.log, stderr=self.log,
            pass_fds=(control_read, ack_write), start_new_session=True, preexec_fn=limits)
        os.close(control_read)
        os.close(ack_write)
        self.ready = self.control_command("enable")

    def control_command(self, name):
        row = {"command": name, "write_ns": time.monotonic_ns(), "ack_ns": None}
        self.commands.append(row)
        try:
            os.write(self.control, (name + "\n").encode())
            if select.select([self.ack], [], [], 5)[0] and os.read(self.ack, 64).strip() == b"ack":
                row["ack_ns"] = time.monotonic_ns()
                return True
        except BrokenPipeError:
            pass
        return False

    def finish(self):
        if self.process.poll() is None:
            self.control_command("disable")
            os.killpg(self.process.pid, signal.SIGINT)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
        self.process.wait()
        self.log.close()
        os.close(self.control)
        os.close(self.ack)
        return {"argv": self.argv, "ready": self.ready,
                "start_ns": self.start_ns, "exit_observed_ns": time.monotonic_ns(),
                "exit_code": self.process.returncode, "controls": self.commands,
                "ring_per_cpu": self.pages}
