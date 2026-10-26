"""Clock, sleep and allocation workload of the #330 Process participant."""
from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
from pathlib import Path

from descendant import clock_ns
from sil.participant import StepParticipant, run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workload", type=Path)
    args = parser.parse_args()
    library = ctypes.CDLL(str(args.workload))
    library.proof_alloc_site.argtypes = [ctypes.c_uint]
    library.proof_alloc_site.restype = ctypes.c_uint64
    libc = ctypes.CDLL(None, use_errno=True)

    class Participant(StepParticipant):
        def __init__(self):
            self.step_index = 0

        def on_step(self, t, dt, inputs):
            clocks = json.loads(subprocess.check_output(
                [sys.executable, str(Path(__file__).with_name("descendant.py")), "child"],
                text=True))
            request = (ctypes.c_long * 2)(0, 1_000_000)
            ctypes.set_errno(0)
            status = libc.nanosleep(ctypes.byref(request), None)
            error = ctypes.get_errno()
            result = library.proof_alloc_site(self.step_index)
            self.step_index += 1
            library.proof_host_wait()
            return [("proof.clock", {"parent": clock_ns(), **clocks,
                    "sleep_status": status, "sleep_errno": error, "result": result})]

    run(Participant())


if __name__ == "__main__":
    main()
