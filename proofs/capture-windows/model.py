"""Shared vocabulary and fixed cases for the capture-window experiment."""
from dataclasses import asdict, dataclass
import re
from typing import NamedTuple

PERIOD_NS = 1_000_000
RUN_DURATION_NS = 620_000_000
EVENT = "cpu-clock:u"
MAX_CAPTURE_BYTES = 1 << 30  # Same default as sil.profile_run.
MAX_INPUT_BYTES = 256 << 20
HOST_TIME = re.compile(r"(?:^|\s)(\d+)\.(\d{9}):?")


def perf_sample_host_ns(line):
    match = HOST_TIME.search(line)
    return int(match[1])*1_000_000_000 + int(match[2]) if match else None


@dataclass(frozen=True)
class CaptureWindow:
    from_ns: int = 610_000_000
    to_ns: int = 611_000_000
    task: int | None = None

    def contains(self, virtual_ns, task):
        return self.from_ns <= virtual_ns < self.to_ns and (self.task is None or self.task == task)

    def json(self):
        return asdict(self)


class Notice(NamedTuple):
    host_ns: int
    virtual_ns: int
    task: int
    edge: int  # 0 begin, 1 return, 2 notifier shutdown (no Virtual-time advance)
    received_host_ns: int


@dataclass(frozen=True)
class WorkInterval:
    task: int
    index: int
    virtual_ns: int
    begin_ns: int
    end_ns: int

    def contains(self, host_ns):
        return self.begin_ns <= host_ns < self.end_ns

    def json(self):
        return asdict(self)


@dataclass(frozen=True)
class Case:
    repeats: int = 1
    runner: str = "observed"
    collector: bool = True
    gated: bool = True
    failing_workload: bool = False
    outcome: str = "complete"
    fault: str | None = None
    window: CaptureWindow = CaptureWindow()
    exact_peer: bool = False
    required_reason: str | None = None
    storage: bool = False

    @property
    def notifications(self):
        return self.runner == "observed"

    def selection(self, variant):
        return CaptureWindow(task=1 if variant == "native" else 0) if self.exact_peer else self.window


CASES = {
    "ordinary": Case(3, runner="ordinary", collector=False, gated=False),
    "diagnostics": Case(3, runner="baseline", collector=False, gated=False),
    "observer": Case(3, collector=False, gated=False),
    "whole-baseline": Case(3, runner="baseline", gated=False),
    "whole": Case(3, gated=False),
    "window": Case(3),
    "exact-peer": Case(exact_peer=True),
    "empty": Case(window=CaptureWindow(to_ns=610_000_000)),
    "unreached": Case(window=CaptureWindow(900_000_000, 901_000_000)),
    "during": Case(fault="delayed_enable"),
    "end-during": Case(fault="host_stop"),
    "ordinary-failure": Case(runner="ordinary", collector=False, gated=False,
                             failing_workload=True, outcome="failed"),
    "failure": Case(failing_workload=True, outcome="failed", window=CaptureWindow(to_ns=615_000_000)),
    "unclosed": Case(outcome="killed", fault="kill", window=CaptureWindow(to_ns=615_000_000),
                     required_reason="unclosed_run_work"),
    "loss": Case(fault="notice_loss", window=CaptureWindow(0, RUN_DURATION_NS), required_reason="notification_loss"),
    "perf-loss": Case(fault="perf_loss", required_reason="perf_lost_records"),
    "limit": Case(gated=False, fault="capture_limit", required_reason="capture_limit"),
    "reserve": Case(gated=False, required_reason="storage_reserve", storage=True),
    "exhaustion": Case(gated=False, outcome="storage_fault", fault="exhaustion", storage=True),
}
