"""Supervise one independent Run and the proof's finite collector faults."""
from dataclasses import dataclass
import os
import signal
import subprocess
import time

from capture import FILE_BYTES, GUARD_BYTES, GUARD_SECONDS, Perf
from evidence import read_text


@dataclass(frozen=True)
class ExperimentCase:
    mode: str
    repetition: int
    observed: bool
    fault: str | None = None

    @property
    def name(self):
        return f"{self.mode}-{self.repetition}-{'observed' if self.observed else 'ordinary'}-{self.fault or 'normal'}"

    @property
    def deadline_ms(self):
        return 180 if self.mode == "edge" else 10000

    def result(self):
        return {"mode": self.mode, "fault": self.fault, "observed": self.observed,
                "policies": {"response_deadline_ms": self.deadline_ms, "guard_seconds": GUARD_SECONDS,
                    "raw_guard_bytes": GUARD_BYTES, "hard_per_file_bytes": FILE_BYTES,
                    "poll_ms": 10, "sampling_subtraction": False}, "growth": []}


class FaultInjection:
    def __init__(self, fault):
        self.fault, self.paused_at, self.resumed = fault, None, False

    def tick(self, perf, out, elapsed, size):
        marker_file = out / "markers.jsonl"
        markers = read_text(marker_file) if marker_file.exists() else ""
        if self.fault == "loss":
            self.pause_reader(perf, markers, elapsed)
        if size > GUARD_BYTES or (self.fault == "limit" and size > 65536):
            return "size_guard"
        if self.fault == "interrupt" and '"phase":"sleep"' in markers:
            return "interrupted"
        return None

    def pause_reader(self, perf, markers, elapsed):
        if self.paused_at is None and '"phase":"cpu"' in markers:
            perf.process.send_signal(signal.SIGSTOP)
            self.paused_at = elapsed
        if self.paused_at is not None and not self.resumed and elapsed - self.paused_at >= 100_000_000:
            perf.process.send_signal(signal.SIGCONT)
            self.resumed = True


class RunObservation:
    def __init__(self, out, case):
        out.mkdir()
        self.out, self.case = out, case
        self.result = case.result()
        self.perf = Perf(out, pages="1" if case.fault == "loss" else "1M") if case.observed else None
        self.collector_stopped = False
        self.injection = FaultInjection(case.fault)

    def observe(self, argv, env):
        if self.perf and not self.perf.ready:
            self.finish_collector()
            self.result["availability"] = "unavailable"
            return self.result
        start = time.monotonic_ns()
        try:
            self.observe_runner(argv, env, start)
        finally:
            self.finish_collector()
        self.result["availability"] = "available" if self.perf else "not_requested"
        return self.result

    def observe_runner(self, argv, env, start):
        with (self.out / "stdout.txt").open("wb") as stdout, (self.out / "stderr.txt").open("wb") as stderr:
            process = subprocess.Popen(argv, cwd=self.out, env=env, stdout=stdout,
                                       stderr=stderr, start_new_session=True)
            stopped = None
            try:
                stopped = self.supervise(process, start)
            finally:
                stop_runner(process)
                elapsed = time.monotonic_ns() - start
        self.result["run"] = {"argv": argv, "exit_code": process.returncode,
                              "stopped": stopped, "wall_ns": elapsed}

    def supervise(self, process, start):
        while process.poll() is None:
            elapsed = time.monotonic_ns() - start
            raw = self.out / "perf.data"
            size = raw.stat().st_size if raw.exists() else 0
            self.result["growth"].append([elapsed, size])
            if elapsed > GUARD_SECONDS * 10**9:
                return "wall_guard"
            if self.perf and not self.collector_stopped:
                reason = self.injection.tick(self.perf, self.out, elapsed, size)
                if reason:
                    self.result["collector_stop_reason"] = reason
                    self.finish_collector()
            time.sleep(.01)
        return None

    def finish_collector(self):
        if self.perf and not self.collector_stopped:
            if self.perf.process.poll() is None:
                self.perf.process.send_signal(signal.SIGCONT)
            self.result["collector"] = self.perf.finish()
            self.collector_stopped = True


def stop_runner(process):
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
    process.wait()
