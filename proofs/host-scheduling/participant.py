"""One unshimmed Process participant; CPU/clock observations stay out of Messages."""
import ctypes
import os
import sys

from sil.participant import StepParticipant, run


class Workload(StepParticipant):
    def __init__(self, library, helper, name, mode):
        self.library = ctypes.CDLL(library)
        self.helper = helper.encode()
        # Keep strings alive: the C fixture retains the participant name.
        self.name = name.encode()
        self.library.proof_open.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        self.library.proof_step.argtypes = [ctypes.c_uint64, ctypes.c_uint64,
                                            ctypes.c_char_p, ctypes.c_int]
        self.library.proof_step.restype = ctypes.c_uint64
        self.library.proof_open(os.environ["SIL_SCHED_MARKERS"].encode(), self.name)
        self.index, self.mode = 0, mode

    def on_step(self, t, dt, inputs):
        index = self.index
        self.index += 1
        value = self.library.proof_step(index, t, self.helper, self.mode == "unclosed")
        if self.mode == "failure" and index == 1:
            raise RuntimeError("named fixture failure after independently marked work")
        return [(self.name.decode(), {"index": index, "value": value})]

if __name__ == "__main__":
    workload = Workload(*sys.argv[1:])
    try:
        run(workload)
    finally:
        workload.library.proof_close()
