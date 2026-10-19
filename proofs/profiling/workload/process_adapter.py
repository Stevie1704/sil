"""Process participant of the profiling proof (issue #314).

    python3 process_adapter.py LIBRARY CHILD

loads LIBRARY (libsilproof_process.so) with ctypes and calls
`silproof_process_step` once per Step. Every CHILD_EVERY Steps it also runs
CHILD (silproof_child) as a short-lived child process. It publishes the
library result, the library's libc CLOCK_MONOTONIC reading and the child's
reading on `proof.process`. Under the Clock shim both readings are Virtual
time, so they are deterministic.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys

from sil.participant import StepParticipant, run

CHILD_EVERY = 25


class ProofProcess(StepParticipant):
    def __init__(self, library: str, child: str):
        self._library = ctypes.CDLL(library)
        self._step_fn = self._library.silproof_process_step
        self._step_fn.argtypes = [ctypes.c_uint64,
                                  ctypes.POINTER(ctypes.c_uint64)]
        self._step_fn.restype = ctypes.c_uint64
        self._child = child
        self._step = 0

    def _run_child(self, index: int) -> tuple[int, int]:
        done = subprocess.run([self._child, str(index)], capture_output=True,
                              text=True, check=True)
        clock_ns, result = done.stdout.split()
        return int(clock_ns), int(result)

    def on_step(self, t, dt, inputs):
        step = self._step
        self._step += 1
        clock_now = ctypes.c_uint64(0)
        result = self._step_fn(step, ctypes.byref(clock_now))
        child_clock, child_result = 0, 0
        if step % CHILD_EVERY == 0:
            child_clock, child_result = self._run_child(step)
        return [("proof.process", {
            "result": result,
            "clock_now_ns": clock_now.value,
            "child_clock_ns": child_clock,
            "child_result": child_result,
        })]


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: process_adapter.py LIBRARY CHILD")
    run(ProofProcess(sys.argv[1], sys.argv[2]))
