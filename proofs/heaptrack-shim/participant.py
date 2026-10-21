"""Clock, sleep and two descendant generations of the #330 workload."""
from __future__ import annotations

import ctypes
import json
import subprocess
import sys
from pathlib import Path


def clock_ns():
    class Timespec(ctypes.Structure):
        _fields_ = [("seconds", ctypes.c_long), ("ns", ctypes.c_long)]
    ts = Timespec()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.clock_gettime(1, ctypes.byref(ts)):
        raise OSError(ctypes.get_errno(), "clock_gettime")
    return ts.seconds * 1_000_000_000 + ts.ns


def descendant(mode):
    value = [clock_ns()]
    if mode == "child":
        value += json.loads(subprocess.check_output(
            [sys.executable, __file__, "grandchild"], text=True))
    return value


def main():
    if sys.argv[1] in {"child", "grandchild"}:
        print(json.dumps(descendant(sys.argv[1])))
        return
    from sil.participant import StepParticipant, run

    library = ctypes.CDLL(sys.argv[1])
    library.proof_alloc_site.argtypes = [ctypes.c_uint]
    library.proof_alloc_site.restype = ctypes.c_uint64
    libc = ctypes.CDLL(None, use_errno=True)

    class Participant(StepParticipant):
        def on_step(self, t, dt, inputs):
            clocks = json.loads(subprocess.check_output(
                [sys.executable, str(Path(__file__)), "child"], text=True))
            # Both policies are observable without a retry loop.
            request = (ctypes.c_long * 2)(0, 1_000_000)
            ctypes.set_errno(0)
            status = libc.nanosleep(ctypes.byref(request), None)
            error = ctypes.get_errno()
            result = library.proof_alloc_site(t // 100_000_000)
            library.proof_host_wait()
            return [("proof.clock", {"parent": clock_ns(), "child": clocks[0],
                    "grandchild": clocks[1], "sleep_status": status,
                    "sleep_errno": error, "result": result})]

    run(Participant())


if __name__ == "__main__":
    main()
