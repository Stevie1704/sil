"""Python endpoint for the step protocol specified in ``docs/step-protocol.md``.

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

# Fixed-layout header at the front of every arena slot: seq (u64), len (u64),
# then one payload. Mirrors include/sil/arena.h — the kernel is the writer for
# inputs and the reader for outputs; this side is the mirror image.
_ARENA_HEADER = struct.Struct("<QQ")
_STEP_PROTOCOL_SINGLE_SLOT = 1
_STEP_PROTOCOL_INDEXED_SLOTS = 2


class _Arena:
    """A per-channel arena mapped MAP_SHARED with the kernel.

    Payloads cross through this region instead of base64/JSON. Its layout and
    per-step transport rules are specified in ``docs/step-protocol.md``;
    `seq` marks a fresh write so a stale read is caught.

    `capacity` is the kernel-authoritative arena size (derived from the schema
    `byte_size`, delivered in the init line); this side does not re-derive it
    from the schema, so the two ends cannot disagree.
    """

    def __init__(self, path: str, capacity: int, slots: int = 1):
        self._capacity = capacity
        self.slots = slots
        self._stride = _ARENA_HEADER.size + capacity
        self._file = open(path, "r+b")
        self._mmap = mmap.mmap(self._file.fileno(), self._stride * slots)
        self._seq = 0

    def _offset(self, slot: int) -> int:
        if (
            not isinstance(slot, int)
            or isinstance(slot, bool)
            or not 0 <= slot < self.slots
        ):
            raise ParticipantFailure(f"arena slot {slot!r} out of range")
        return slot * self._stride

    def read(self, seq: int, slot: int = 0) -> bytes:
        offset = self._offset(slot)
        got_seq, length = _ARENA_HEADER.unpack_from(self._mmap, offset)
        if got_seq != seq:
            raise ParticipantFailure(
                f"stale arena slot {slot} (expected seq {seq}, got {got_seq})"
            )
        if length > self._capacity:
            raise ParticipantFailure("arena len exceeds capacity")
        start = offset + _ARENA_HEADER.size
        return bytes(self._mmap[start : start + length])

    def write(self, payload: bytes, slot: int = 0) -> int:
        if len(payload) > self._capacity:
            raise ParticipantFailure("payload exceeds arena capacity")
        offset = self._offset(slot)
        start = offset + _ARENA_HEADER.size
        self._mmap[start : start + len(payload)] = payload
        self._seq += 1
        _ARENA_HEADER.pack_into(
            self._mmap, offset, self._seq, len(payload)
        )
        return self._seq

    def close(self) -> None:
        self._mmap.close()
        self._file.close()


class _InlineAdapter:
    """Encode and decode the inline base64 representation of one payload."""

    @staticmethod
    def encode(item: dict, raw: bytes) -> None:
        item["data"] = base64.b64encode(raw).decode()

    @staticmethod
    def decode(item: dict) -> bytes:
        return base64.b64decode(item["data"])


class _ArenaAdapter:
    """Encode and decode payloads through the kernel-owned arena mapping."""

    def __init__(self, arenas: dict[str, _Arena]):
        self._arenas = arenas

    def encode(
        self, item: dict, channel: str, raw: bytes, slot: int, indexed: bool
    ) -> None:
        if indexed:
            item["shm_slot"] = slot
        item["shm_seq"] = self._arenas[channel].write(raw, slot)

    def decode(self, item: dict, channel: str) -> bytes:
        return self._arenas[channel].read(
            item["shm_seq"], item.get("shm_slot", 0)
        )


class _StepCodec:
    """Step-scoped transport seam for the Python endpoint.

    Inputs are decoded in their received order, which is already global
    Publish order. Outputs fill the Arena's declared slots per Channel and use
    the inline representation for excess Messages; the local indices make slot
    allocation reset naturally for the next Step without a caller-visible
    ``begin_step`` operation.
    """

    def __init__(
        self,
        types_by_channel: dict[str, schema.MessageType],
        arenas: dict[str, _Arena],
        *,
        indexed_slots: bool = False,
    ):
        self._types_by_channel = types_by_channel
        self._inline = _InlineAdapter()
        self._arena = _ArenaAdapter(arenas)
        self._arenas = arenas
        self._indexed_slots = indexed_slots

    def decode_inputs(self, messages: list[dict]) -> list["Input"]:
        """Decode the step's input array without inferring transport."""
        inputs = []
        for message in messages:
            channel = message["ch"]
            if "shm_seq" in message:
                raw = self._arena.decode(message, channel)
            else:
                raw = self._inline.decode(message)
            inputs.append(
                Input(
                    channel,
                    message["t"],
                    self._types_by_channel[channel].unpack(raw),
                )
            )
        return inputs

    def encode_outputs(self, outputs) -> list[dict]:
        """Encode all outputs for one step, preserving their order."""
        encoded = []
        next_slot_by_channel: dict[str, int] = {}
        for channel, fields in outputs:
            raw = self._types_by_channel[channel].pack(**fields)
            item = {"ch": channel}
            slot = next_slot_by_channel.get(channel, 0)
            if channel in self._arenas and slot < self._arenas[channel].slots:
                next_slot_by_channel[channel] = slot + 1
                self._arena.encode(
                    item, channel, raw, slot, self._indexed_slots
                )
            else:
                self._inline.encode(item, raw)
            encoded.append(item)
        return encoded


class ParticipantFailure(Exception):
    """Raised by a participant to abort the whole run."""


class ConfigurationError(Exception):
    """Raised by a participant whose init line cannot be honoured at all.

    It is answered with `fail` instead of `ready`, which the kernel treats as a
    configuration error rather than a Run failure — see docs/step-protocol.md.
    Every other exception during initialization stays a Run failure, so a
    participant that breaks on the way up is not reported as a bad Manifest.
    """


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


def _reason(error: Exception) -> str:
    """The one-line diagnostic a `fail` line carries for an exception."""
    return "".join(traceback.format_exception_only(error)).strip()


def run(participant: StepParticipant) -> None:
    stdin = sys.stdin
    stdout = sys.stdout
    types_by_channel: dict[str, schema.MessageType] = {}
    arenas: dict[str, _Arena] = {}
    codec: _StepCodec | None = None

    def send(msg: dict) -> None:
        stdout.write(json.dumps(msg) + "\n")
        stdout.flush()

    try:
        for line in stdin:
            msg = json.loads(line)
            op = msg["op"]
            if op == "init":
                protocol = min(
                    msg.get("protocol", _STEP_PROTOCOL_SINGLE_SLOT),
                    _STEP_PROTOCOL_INDEXED_SLOTS,
                )
                types = schema.load(msg["schemas"])
                types_by_channel = {
                    ch: types[info["schema"]]
                    for ch, info in msg["channels"].items()
                }
                arenas = {
                    ch: _Arena(
                        info["shm_path"], info["shm_capacity"],
                        info.get("shm_slots", 1),
                    )
                    for ch, info in msg["channels"].items()
                    if info.get("transport") == "shm"
                }
                codec = _StepCodec(
                    types_by_channel, arenas,
                    indexed_slots=protocol >= _STEP_PROTOCOL_INDEXED_SLOTS,
                )
                try:
                    participant.on_init(msg)
                except ConfigurationError as e:
                    # Only a deliberate rejection answers `fail`: that line
                    # before `ready` is what makes the kernel call this a
                    # configuration error. Returning ends the loop — no step
                    # can follow an initialization that never finished.
                    send({"op": "fail", "reason": _reason(e)})
                    return
                ready = {"op": "ready"}
                if "protocol" in msg:
                    ready["protocol"] = protocol
                send(ready)
            elif op == "step":
                try:
                    if codec is None:
                        raise ParticipantFailure("step received before init")
                    inputs = codec.decode_inputs(msg["in"])
                    outputs = participant.on_step(msg["t"], msg["dt"], inputs) or []
                    send({
                        "op": "step_done",
                        "out": codec.encode_outputs(outputs),
                    })
                except Exception as e:  # noqa: BLE001 — any error must abort the run
                    send({"op": "fail", "reason": _reason(e)})
                    continue
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
    # `python -m sil.participant` runs this file as `__main__`, so the classes
    # defined here are not the ones a participant gets from `import
    # sil.participant`. Delegating to the imported module gives both sides the
    # same ConfigurationError, which the init handshake compares by identity.
    from sil.participant import main as _main

    _main()
