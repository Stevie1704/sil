"""Raw Step-protocol participant fixtures for run-boundary limit tests."""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path


PAYLOAD = base64.b64encode(b"\0" * 16).decode()


def _write_observer(path: str | None) -> None:
    if path is None:
        return
    Path(path).write_text(f"{os.getpid()}\n{Path.cwd()}\n")


def _long_response(op: str, *, newline: bool) -> None:
    response = json.dumps({"op": op, "padding": "x" * 4096})
    sys.stdout.write(response)
    if newline:
        sys.stdout.write("\n")
    sys.stdout.flush()


def main(mode: str, observer: str | None = None) -> None:
    _write_observer(observer)
    for line in sys.stdin:
        message = json.loads(line)
        if message["op"] == "init":
            if mode == "ready-long":
                _long_response("ready", newline=True)
            else:
                print(json.dumps({"op": "ready"}), flush=True)
        elif message["op"] == "step":
            if mode == "step-unterminated":
                _long_response("step_done", newline=False)
            elif mode == "many":
                print(
                    json.dumps(
                        {
                            "op": "step_done",
                            "out": [
                                {"ch": "ticks", "data": "not-base64"},
                                {"ch": "ticks", "data": "not-base64"},
                            ],
                        }
                    ),
                    flush=True,
                )
            elif mode == "inline":
                print(
                    json.dumps(
                        {
                            "op": "step_done",
                            "out": [{"ch": "ticks", "data": PAYLOAD}],
                        }
                    ),
                    flush=True,
                )
            else:
                print(json.dumps({"op": "step_done", "out": []}), flush=True)
        elif message["op"] == "shutdown":
            return


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
