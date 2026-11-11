"""Raw step-protocol participant that fails in one chosen way.

Usage: failure_modes.py <mode> <fail_at_ns> [channel]

Step modes fail at the first Step at or after <fail_at_ns>:
  exit        write stderr lines, then exit with status 3
  signal      kill itself with SIGKILL
  protocol    answer the Step with an op that is not step_done
  wrong_size  publish 5 bytes on <channel>, which takes a 16-byte toy.Counter
  burst       publish two toy.Counter Messages on <channel> in one Step

Init modes answer `init` with a malformed `ready` line:
  ready_number  the JSON number 42
  ready_array   an empty JSON array
  ready_op      an `op` that is not a string
  ready_reason  a `fail` line whose `reason` is not a string
"""

import base64
import json
import os
import signal
import struct
import sys

READY_LINES = {
    "ready_number": 42,
    "ready_array": [],
    "ready_op": {"op": 7},
    "ready_reason": {"op": "fail", "reason": 7},
}


def counter(seq: int) -> str:
    return base64.b64encode(struct.pack("<Qq", seq, -seq)).decode()


def outputs(mode: str, channel: str) -> list:
    if mode == "wrong_size":
        return [{"ch": channel, "data": base64.b64encode(b"short").decode()}]
    if mode == "burst":
        return [{"ch": channel, "data": counter(seq)} for seq in (1, 2)]
    raise ValueError(f"unknown mode: {mode}")


def fail(mode: str, t: int, channel: str) -> None:
    if mode == "exit":
        for index in range(30):
            print(f"stderr line {index} at t={t}", file=sys.stderr, flush=True)
        sys.exit(3)
    if mode == "signal":
        os.kill(os.getpid(), signal.SIGKILL)
    if mode == "protocol":
        print(json.dumps({"op": "not_step_done"}), flush=True)
        return
    out = outputs(mode, channel)
    print(json.dumps({"op": "step_done", "out": out}), flush=True)


def main(mode: str, fail_at_ns: int, channel: str) -> None:
    for line in sys.stdin:
        message = json.loads(line)
        if message["op"] == "init":
            ready = READY_LINES.get(mode, {"op": "ready"})
            print(json.dumps(ready), flush=True)
        elif message["op"] == "step":
            if message["t"] >= fail_at_ns:
                fail(mode, message["t"], channel)
            else:
                print(json.dumps({"op": "step_done", "out": []}), flush=True)
        elif message["op"] == "shutdown":
            return


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else "")
