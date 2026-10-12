"""Raw step-protocol participant that fails in one chosen way at one Step.

Usage: failure_modes.py <mode> <fail_at_ns>

Modes:
  exit      write stderr lines, then exit with status 3
  signal    kill itself with SIGKILL
  protocol  answer the Step with an op that is not step_done
"""

import json
import os
import signal
import sys


def fail(mode: str, t: int) -> None:
    if mode == "exit":
        for index in range(30):
            print(f"stderr line {index} at t={t}", file=sys.stderr, flush=True)
        sys.exit(3)
    if mode == "signal":
        os.kill(os.getpid(), signal.SIGKILL)
    if mode == "protocol":
        print(json.dumps({"op": "not_step_done"}), flush=True)
        return
    raise ValueError(f"unknown mode: {mode}")


def main(mode: str, fail_at_ns: int) -> None:
    for line in sys.stdin:
        message = json.loads(line)
        if message["op"] == "init":
            print(json.dumps({"op": "ready"}), flush=True)
        elif message["op"] == "step":
            if message["t"] >= fail_at_ns:
                fail(mode, message["t"])
            else:
                print(json.dumps({"op": "step_done", "out": []}), flush=True)
        elif message["op"] == "shutdown":
            return


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]))
