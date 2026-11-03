"""Same optimized workload through a Process participant's public interface."""
import ctypes
import sys

from sil.participant import StepParticipant, run


class Workload(StepParticipant):
    def __init__(self, library, fail):
        self.library = ctypes.CDLL(library)
        self.library.window_work.argtypes = [ctypes.c_uint64, ctypes.c_uint64]
        self.index = 0
        self.fail_late = fail

    def on_step(self, t, dt, inputs):
        index = self.index
        self.index += 1
        self.library.window_work(0, index)
        if self.fail_late and index == 610:
            raise RuntimeError("named late failure")
        return [("events", {"task": 0, "index": index})]


if __name__ == "__main__":
    run(Workload(sys.argv[1], sys.argv[2] == "fail"))
