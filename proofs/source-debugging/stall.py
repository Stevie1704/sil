"""A Process participant that hangs at one Step (#367 hang control).

It answers every Step before `HANG_NS` at once and never answers the Step at
`HANG_NS`. It subscribes and publishes nothing.
"""
import json
import sys
import time

HANG_NS = 50_000_000

for line in sys.stdin:
    message = json.loads(line)
    if message["op"] == "init":
        print(json.dumps({"op": "ready"}), flush=True)
    elif message["op"] == "step":
        while message["t"] >= HANG_NS:
            time.sleep(1)
        print(json.dumps({"op": "step_done", "out": []}), flush=True)
    elif message["op"] == "shutdown":
        break
