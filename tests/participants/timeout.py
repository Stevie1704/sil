"""Small raw step-protocol participants for the participant-timeout suite."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path


def _hang() -> None:
    while True:
        time.sleep(1)


def _write_observer(path: str | None) -> None:
    if path is None:
        return
    Path(path).write_text(f"{os.getpid()}\n{Path.cwd()}\n")


def _send_partial_step() -> None:
    # Keep producing an unterminated response for long enough that a buggy
    # implementation which resets its deadline after every read can be kept
    # alive. The complete response arrives only after the configured deadline.
    fragments = [
        '{"op":"step_done"',
        ',"out"',
        ':[]',
        '}',
    ]
    for index in range(40):
        sys.stdout.write(fragments[index % len(fragments)])
        sys.stdout.flush()
        time.sleep(0.01)
    sys.stdout.write("\n")
    sys.stdout.flush()


def main(mode: str, observer: str | None = None) -> None:
    _write_observer(observer)
    if mode == "ignore-term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

    for line in sys.stdin:
        message = json.loads(line)
        if message["op"] == "init":
            if mode in {"init", "ignore-term"}:
                _hang()
            print(json.dumps({"op": "ready"}), flush=True)
        elif message["op"] == "step":
            if mode == "step" or mode == "ignore-term":
                _hang()
            if mode == "partial":
                _send_partial_step()
            elif mode == "slow":
                time.sleep(0.06)
                print(json.dumps({"op": "step_done", "out": []}), flush=True)
            else:
                print(json.dumps({"op": "step_done", "out": []}), flush=True)
        elif message["op"] == "shutdown":
            return


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
