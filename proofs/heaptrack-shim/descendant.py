"""Exec descendant clock probe; JSON fields identify each generation."""
from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys


def clock_ns():
    class Timespec(ctypes.Structure):
        _fields_ = [("seconds", ctypes.c_long), ("ns", ctypes.c_long)]
    ts = Timespec()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.clock_gettime(1, ctypes.byref(ts)):
        raise OSError(ctypes.get_errno(), "clock_gettime")
    return ts.seconds * 1_000_000_000 + ts.ns


def probe(generation):
    clocks = {generation: clock_ns()}
    if generation == "child":
        clocks.update(json.loads(subprocess.check_output(
            [sys.executable, __file__, "grandchild"], text=True)))
    return clocks


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("generation", choices=("child", "grandchild"))
    print(json.dumps(probe(parser.parse_args().generation)))
