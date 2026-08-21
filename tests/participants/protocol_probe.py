"""Record raw input step lines for process-protocol conformance tests."""

import json
import sys


def main(log_path: str) -> None:
    """Acknowledge the child protocol and persist every received input array."""
    steps = []
    for line in sys.stdin:
        message = json.loads(line)
        if message["op"] == "init":
            print(json.dumps({"op": "ready"}), flush=True)
        elif message["op"] == "step":
            steps.append({"t": message["t"], "in": message["in"]})
            print(json.dumps({"op": "step_done", "out": []}), flush=True)
        elif message["op"] == "shutdown":
            with open(log_path, "w") as log:
                json.dump(steps, log)
            return


if __name__ == "__main__":
    main(sys.argv[1])
