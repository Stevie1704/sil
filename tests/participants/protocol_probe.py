"""Record raw input step lines for process-protocol conformance tests."""

import json
import os
import sys


def main(log_path: str, echo_protocol: bool = False) -> None:
    """Acknowledge the child protocol and persist every received input array."""
    steps = []
    protocol = None
    arena_shapes = {}
    for line in sys.stdin:
        message = json.loads(line)
        if message["op"] == "init":
            protocol = message.get("protocol")
            arena_shapes = {
                channel: {
                    "capacity": info["shm_capacity"],
                    "slots": info["shm_slots"],
                    "file_size": os.path.getsize(info["shm_path"]),
                }
                for channel, info in message["channels"].items()
                if info.get("transport") == "shm"
            }
            ready = {"op": "ready"}
            if echo_protocol:
                ready["protocol"] = protocol
            print(json.dumps(ready), flush=True)
        elif message["op"] == "step":
            steps.append({
                "t": message["t"], "in": message["in"],
                "protocol": protocol, "arenas": arena_shapes,
            })
            print(json.dumps({"op": "step_done", "out": []}), flush=True)
        elif message["op"] == "shutdown":
            with open(log_path, "w") as log:
                json.dump(steps, log)
            return


if __name__ == "__main__":
    main(sys.argv[1], len(sys.argv) > 2 and sys.argv[2] == "echo")
