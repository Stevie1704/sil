"""One bounded Run/perf session; the scheduler never waits for its supervisor."""
from contextlib import ExitStack
import errno
import os
from pathlib import Path
import selectors
import signal
import socket
import struct
import subprocess
import tempfile
import time

from sil.profile_storage import reserve_reached
from model import EVENT, MAX_CAPTURE_BYTES, Notice

NOTICE_BYTES = struct.Struct("<QQQQ")
GUARD_POLL_NS = 100_000_000
SUPERVISOR_TIMEOUT_NS = 60_000_000_000


class PerfControl:
    def __init__(self, active):
        self.active = active
        self.pending = None
        self.records = []
        self.buffer = b""

    def receive(self, descriptor):
        self.buffer += os.read(descriptor, 4096)
        self.buffer = self.buffer.lstrip(b"\0")
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            self.buffer = self.buffer.lstrip(b"\0")
            if line != b"ack" or self.pending is None:
                raise RuntimeError("unexpected perf acknowledgment")
            self.pending["ack_ns"] = time.monotonic_ns()
            self.active = self.pending["command"] == "enable"
            self.pending = None

    def request(self, descriptor, desired):
        if self.pending is not None or desired == self.active:
            return
        verb = "enable" if desired else "disable"
        record = {"command": verb, "write_ns": time.monotonic_ns(), "ack_ns": None}
        os.write(descriptor, f"{verb} {EVENT}\n".encode())
        self.pending = record
        self.records.append(record)


class SelectionController:
    def __init__(self, case, selection):
        self.case, self.selection = case, selection
        self.desired = not case.gated
        self.selection_open = False
        self.stop_reason = None
        self.end_reason = None

    def observe(self, notice):
        if notice.edge == 2:
            self.selection_open = False
            if self.desired:
                self.desired = False
                self.end_reason = "notifier_shutdown"
            return
        if not self.case.gated or self.stop_reason:
            return
        selected = self.selection.contains(notice.virtual_ns, notice.task)
        if self.selection.task is not None:
            if selected:
                self.desired = self.selection_open = notice.edge == 0
                if notice.edge == 1:
                    self.end_reason = "selected_activation_return"
        elif notice.edge == 0:
            self.desired = self.selection_open = selected
            if notice.virtual_ns >= self.selection.to_ns:
                self.end_reason = "requested_range_end"

    def stop(self, reason):
        self.desired = False
        self.stop_reason = reason
        self.end_reason = reason


class FaultInjection:
    def __init__(self, fault, reserve_directory):
        self.fault = fault
        self.reserve_directory = reserve_directory
        self.applied = False
        self.due = {}
        self.storage_errno = None

    def observe(self, notice, controller):
        if self.fault == "notice_loss" and not self.applied:
            time.sleep(.1)  # Pause only the supervisor, after a real notice.
            self.applied = True
        if self.fault == "exhaustion" and not self.applied and notice.virtual_ns >= 100_000_000:
            self.fill_storage()
            self.applied = True
        if notice.edge != 0 or not controller.desired or self.applied:
            return
        delays = {"delayed_enable": ("enable", 20_000_000),
                  "host_stop": ("disable", 30_000_000), "kill": ("kill", 30_000_000)}
        if self.fault in delays:
            action, delay = delays[self.fault]
            self.due[action] = time.monotonic_ns() + delay
            self.applied = True
            if action == "enable":
                controller.desired = False

    def fill_storage(self):
        path = self.reserve_directory / "filler"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            for _ in range(512):
                try:
                    os.write(descriptor, b"x"*65536)
                except OSError as error:
                    if error.errno != errno.ENOSPC:
                        raise
                    self.storage_errno = error.errno
                    return
            raise RuntimeError("storage fault did not exhaust the 16 MiB filesystem")
        finally:
            os.close(descriptor)

    def tick(self, controller, process):
        now = time.monotonic_ns()
        for action, due in list(self.due.items()):
            if now < due:
                continue
            del self.due[action]
            if action == "enable" and controller.selection_open and not controller.stop_reason:
                controller.desired = True
            elif action == "disable":
                controller.stop("explicit_host_stop")
            elif action == "kill":
                children = Path(f"/proc/{process.pid}/task/{process.pid}/children")
                for pid in children.read_text().split():
                    os.kill(int(pid), signal.SIGKILL)
                controller.stop("forced_sigkill")


class CaptureSession:
    def __init__(self, out, case, selection, data, reserve_directory):
        self.out, self.case, self.data = out, case, data
        self.controller = SelectionController(case, selection)
        self.control = PerfControl(active=not case.gated)
        self.fault = FaultInjection(case.fault, reserve_directory)
        self.notices, self.growth = [], []
        self.last_poll = 0
        self.started_host_ns = self.finished_host_ns = None
        self.resources = ExitStack()

    def __enter__(self):
        self.selector = self.resources.enter_context(selectors.DefaultSelector())
        temporary = self.resources.enter_context(tempfile.TemporaryDirectory(prefix="sil-window-"))
        self.socket_path = temporary + "/notice"
        self.receiver = self.resources.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM))
        self.receiver.bind(self.socket_path)
        self.receiver.setblocking(False)
        self.selector.register(self.receiver, selectors.EVENT_READ, "notice")
        if self.case.collector:
            self.open_control()
        return self

    def __exit__(self, *exception):
        return self.resources.__exit__(*exception)

    def open_control(self):
        self.control_paths = self.out / "control.fifo", self.out / "ack.fifo"
        descriptors = []
        for path in self.control_paths:
            os.mkfifo(path, 0o600)
            self.resources.callback(path.unlink, missing_ok=True)
            descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK)
            self.resources.callback(os.close, descriptor)
            descriptors.append(descriptor)
        self.control_fd, self.ack_fd = descriptors
        self.selector.register(self.ack_fd, selectors.EVENT_READ, "ack")

    def environment(self, original):
        environment = original.copy()
        if self.case.notifications:
            environment.update(SIL_WINDOW_SOCKET=self.socket_path,
                               SIL_WINDOW_OBSERVER=str(self.out / "observer.json"))
        if self.case.fault == "notice_loss":
            environment["SIL_WINDOW_SMALL_BUFFER"] = "1"
        return environment

    def collector_command(self, runner_argv, sampling_options):
        if not self.case.collector:
            return runner_argv
        control, ack = self.control_paths
        return ["perf", "record", *sampling_options,
                *(["-m", "1"] if self.case.fault == "perf_loss" else []),
                "--control", f"fifo:{control},{ack}", "-o", str(self.data),
                *(["--delay=-1"] if self.case.gated else []), "--", *runner_argv]

    def receive_notices(self):
        for _ in range(256):
            try:
                raw = self.receiver.recv(128)
            except BlockingIOError:
                break
            if len(raw) != NOTICE_BYTES.size:
                raise RuntimeError("bad proof notice")
            notice = Notice(*NOTICE_BYTES.unpack(raw), time.monotonic_ns())
            self.notices.append(notice)
            if len(self.notices) > 10000:
                raise RuntimeError("notice budget exceeded")
            self.controller.observe(notice)
            self.fault.observe(notice, self.controller)

    def receive(self, timeout):
        for key, _ in self.selector.select(timeout):
            if key.data == "ack":
                self.control.receive(self.ack_fd)
            else:
                self.receive_notices()

    def poll_storage(self, now):
        if not self.case.collector or now - self.last_poll < GUARD_POLL_NS:
            return
        self.last_poll = now
        size = self.data.stat().st_size if self.data.exists() else 0
        self.growth.append([now, size])
        limit = (64 << 10) if self.case.fault == "capture_limit" else MAX_CAPTURE_BYTES
        if not self.controller.stop_reason:
            if size > limit:
                self.controller.stop("capture_limit")
            elif reserve_reached(self.data.parent):
                self.controller.stop("storage_reserve")

    def supervise(self, process):
        while True:
            now = time.monotonic_ns()
            if now - self.started_host_ns > SUPERVISOR_TIMEOUT_NS:
                raise RuntimeError("proof supervisor exceeded 60 s guard")
            self.receive(.001)
            self.fault.tick(self.controller, process)
            self.poll_storage(time.monotonic_ns())
            if self.case.collector:
                self.control.request(self.control_fd, self.controller.desired)
            if process.poll() is not None:
                self.receive(0)  # Retain queued final notices/acks without inventing any.
                self.finished_host_ns = time.monotonic_ns()
                if self.controller.end_reason is None:
                    self.controller.end_reason = "collector_exit_without_end_notice"
                return

    def run(self, argv, environment):
        with (self.out / "stdout.txt").open("wb") as stdout, (self.out / "stderr.txt").open("wb") as stderr:
            self.started_host_ns = time.monotonic_ns()
            process = subprocess.Popen(argv, env=self.environment(environment), stdout=stdout,
                                       stderr=stderr, start_new_session=True)
            try:
                self.supervise(process)
                return process.returncode
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()

    def observations(self):
        return {"controls": self.control.records, "stop_reason": self.controller.stop_reason,
                "sampling_end_reason": self.controller.end_reason,
                "storage_fault_injected": self.fault.storage_errno == errno.ENOSPC,
                "storage_fault_errno": self.fault.storage_errno, "growth": self.growth,
                "collector_start_host_ns": self.started_host_ns,
                "collector_exit_observed_host_ns": self.finished_host_ns}
