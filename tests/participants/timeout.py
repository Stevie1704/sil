"""Small raw step-protocol participants for the participant-timeout suite."""

from __future__ import annotations

import json
import mmap
import os
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path


_ARENA_HEADER = struct.Struct("<QQ")
_DESCENDANT_MODES = {
    "descendant-success",
    "descendant-failure",
    "descendant-timeout",
    "descendant-ignore-term",
}


def _hang() -> None:
    while True:
        time.sleep(1)


def _write_observer(path: str | None) -> None:
    if path is None:
        return
    Path(path).write_text(f"{os.getpid()}\n{Path.cwd()}\n")


def _grandchild(observer: str, ignore_term: bool) -> None:
    if ignore_term:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    Path("descendant-owned.txt").write_text("descendant is alive")
    Path(observer).write_text(json.dumps({
        "pid": os.getpid(),
        "cwd": str(Path.cwd()),
        "descendant_marker": Path("descendant-owned.txt").exists(),
    }))
    _hang()


def _start_descendant(mode: str, observer: str) -> None:
    Path("participant-owned.txt").write_text("participant is alive")
    subprocess.Popen(
        [
            sys.executable,
            __file__,
            "--grandchild",
            observer,
            "ignore-term" if mode == "descendant-ignore-term" else "term",
        ],
        close_fds=True,
    )

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            info = json.loads(Path(observer).read_text())
            break
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.01)
    else:
        return
    info["participant_marker"] = Path("participant-owned.txt").exists()
    Path(observer).write_text(json.dumps(info))


def _open_arena(init: dict):
    channel = init["channels"]["payload"]
    capacity = channel["shm_capacity"]
    slots = channel["shm_slots"]
    arena_file = open(channel["shm_path"], "r+b")
    stride = _ARENA_HEADER.size + capacity
    arena = mmap.mmap(arena_file.fileno(), stride * slots)
    return arena_file, arena


def _write_arena(arena: mmap.mmap, sequence: int) -> dict:
    payload = b"\x01"
    arena[_ARENA_HEADER.size : _ARENA_HEADER.size + len(payload)] = payload
    _ARENA_HEADER.pack_into(arena, 0, sequence, len(payload))
    return {"ch": "payload", "shm_seq": sequence}


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
    if mode in _DESCENDANT_MODES:
        if observer is None:
            raise SystemExit("descendant mode requires an observer")
        _start_descendant(mode, observer)
    else:
        _write_observer(observer)
    if mode == "ignore-term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

    arena_file = None
    arena = None
    sequence = 0
    try:
        for line in sys.stdin:
            message = json.loads(line)
            if message["op"] == "init":
                if mode in {"init", "ignore-term"}:
                    _hang()
                if mode in _DESCENDANT_MODES:
                    arena_file, arena = _open_arena(message)
                print(json.dumps({"op": "ready"}), flush=True)
            elif message["op"] == "step":
                if mode in {"step", "descendant-timeout"}:
                    _hang()
                if mode == "partial":
                    _send_partial_step()
                elif mode == "descendant-failure":
                    print(json.dumps({"op": "fail", "reason": "fixture failure"}),
                          flush=True)
                elif mode in {"descendant-success", "descendant-ignore-term"}:
                    sequence += 1
                    print(json.dumps({
                        "op": "step_done",
                        "out": [_write_arena(arena, sequence)],
                    }), flush=True)
                else:
                    if mode == "slow":
                        time.sleep(0.03)
                    print(json.dumps({"op": "step_done", "out": []}), flush=True)
            elif message["op"] == "shutdown":
                return
    finally:
        if arena is not None:
            arena.close()
        if arena_file is not None:
            arena_file.close()


if __name__ == "__main__":
    if sys.argv[1] == "--grandchild":
        _grandchild(sys.argv[2], sys.argv[3] == "ignore-term")
    else:
        main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
