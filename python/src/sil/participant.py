"""Python side of the kernel's out-of-process step protocol.

A participant subclasses StepParticipant and hands an instance to run().
The kernel steps it over JSON lines on stdin/stdout; payloads are decoded
to and from field dicts using the schemas delivered in the init message.

The participant contract from DESIGN #6 applies: no wall-clock reads, no
free-running threads — all behavior must derive from (t, dt, inputs).
"""

from __future__ import annotations

import base64
import json
import mmap
import struct
import sys
import traceback
from dataclasses import dataclass

from sil import schema

# Fixed-layout header at the front of every shm arena: seq (u64), len (u64),
# then the payload. Mirrors include/sil/shm_arena.h — the kernel is the writer
# for inputs and the reader for outputs; this side is the mirror image.
_ARENA_HEADER = struct.Struct("<QQ")


class _Arena:
    """A per-channel shared-memory arena mapped MAP_SHARED with the kernel.

    Payloads cross through this region instead of base64/JSON. A single slot is
    enough: the step protocol is sequential and each channel carries at most one
    message per step. `seq` marks a fresh write so a stale read is caught.

    `capacity` is the kernel-authoritative arena size (derived from the schema
    `byte_size`, delivered in the init line); this side does not re-derive it
    from the schema, so the two ends cannot disagree.
    """

    def __init__(self, path: str, capacity: int):
        self._capacity = capacity
        self._file = open(path, "r+b")
        self._mmap = mmap.mmap(
            self._file.fileno(), _ARENA_HEADER.size + capacity
        )
        self._seq = 0

    def read(self, seq: int) -> bytes:
        got_seq, length = _ARENA_HEADER.unpack_from(self._mmap, 0)
        if got_seq != seq:
            raise ParticipantFailure(
                f"stale shm arena (expected seq {seq}, got {got_seq})"
            )
        if length > self._capacity:
            raise ParticipantFailure("shm arena len exceeds capacity")
        start = _ARENA_HEADER.size
        return bytes(self._mmap[start : start + length])

    def write(self, payload: bytes) -> int:
        if len(payload) > self._capacity:
            raise ParticipantFailure("payload exceeds shm arena capacity")
        start = _ARENA_HEADER.size
        self._mmap[start : start + len(payload)] = payload
        self._seq += 1
        _ARENA_HEADER.pack_into(self._mmap, 0, self._seq, len(payload))
        return self._seq

    def close(self) -> None:
        self._mmap.close()
        self._file.close()


class ParticipantFailure(Exception):
    """Raised by a participant to abort the whole run."""


@dataclass(frozen=True)
class Input:
    channel: str
    publish_ns: int
    data: dict


class StepParticipant:
    def on_init(self, init: dict) -> None:
        pass

    def on_step(self, t: int, dt: int, inputs: list[Input]):
        """Advance from t to t+dt. Returns [(channel, fields_dict), ...]
        to publish at t, or None."""
        return None


def run(participant: StepParticipant) -> None:
    stdin = sys.stdin
    stdout = sys.stdout
    types_by_channel: dict[str, schema.MessageType] = {}
    # shm channels map an arena and move payloads through it; inline channels
    # stay on the base64/JSON path. The transport choice never reaches the
    # participant — on_step sees the same field-dict/bytes/list either way.
    arenas: dict[str, _Arena] = {}

    def send(msg: dict) -> None:
        stdout.write(json.dumps(msg) + "\n")
        stdout.flush()

    def unpack_input(i: dict) -> Input:
        ch = i["ch"]
        arena = arenas.get(ch)
        raw = arena.read(i["shm_seq"]) if arena else base64.b64decode(i["data"])
        return Input(ch, i["t"], types_by_channel[ch].unpack(raw))

    def encode_output(ch: str, fields: dict) -> dict:
        raw = types_by_channel[ch].pack(**fields)
        arena = arenas.get(ch)
        if arena:
            return {"ch": ch, "shm_seq": arena.write(raw)}
        return {"ch": ch, "data": base64.b64encode(raw).decode()}

    try:
        for line in stdin:
            msg = json.loads(line)
            op = msg["op"]
            if op == "init":
                types = schema.load(msg["schemas"])
                types_by_channel = {
                    ch: types[info["schema"]]
                    for ch, info in msg["channels"].items()
                }
                arenas = {
                    ch: _Arena(info["shm_path"], info["shm_capacity"])
                    for ch, info in msg["channels"].items()
                    if info.get("transport") == "shm"
                }
                participant.on_init(msg)
                send({"op": "ready"})
            elif op == "step":
                inputs = [unpack_input(i) for i in msg["in"]]
                try:
                    outputs = participant.on_step(msg["t"], msg["dt"], inputs) or []
                except Exception as e:  # noqa: BLE001 — any error must abort the run
                    reason = "".join(traceback.format_exception_only(e)).strip()
                    send({"op": "fail", "reason": reason})
                    continue
                send({
                    "op": "step_done",
                    "out": [encode_output(ch, fields) for ch, fields in outputs],
                })
            elif op == "shutdown":
                return
    finally:
        for arena in arenas.values():
            arena.close()


def _load(spec: str) -> StepParticipant:
    """Instantiates a participant from a '<file.py>:<ClassName>' spec."""
    import importlib.util

    path, _, cls_name = spec.rpartition(":")
    if not path:
        raise SystemExit(f"participant spec must be <file.py>:<Class>, got {spec!r}")
    module_spec = importlib.util.spec_from_file_location("sil_participant_module", path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return getattr(module, cls_name)()


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        raise SystemExit("usage: python -m sil.participant <file.py>:<Class>")
    run(_load(args[0]))


if __name__ == "__main__":
    main()
