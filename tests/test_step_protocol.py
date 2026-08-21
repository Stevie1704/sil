"""Conformance cases for the C++ and Python step transport codecs."""

import json
import struct
import sys

import pytest

from sil import schema
from sil.manifest import Manifest
from sil.participant import (
    ParticipantFailure,
    _Arena,
    _StepCodec,
)

from conftest import ROOT
from test_run_boundary import ARRAY_SCHEMAS, ARRAY_TYPES, read_mcap


def payload(message_id):
    """Build the fixed-layout payload used by the process fixtures."""
    return {
        "id": message_id,
        "blob": bytes((message_id + k) % 256 for k in range(4)),
        "samples": [float(message_id * 10 + k) for k in range(8)],
    }


def _python_codec(tmp_path, arena_channels):
    """Return a Python codec and its mappings, closing arenas after use."""
    types = schema.load(ARRAY_SCHEMAS)
    arenas = {}
    for channel in arena_channels:
        capacity = types["big.Payload"].size
        path = tmp_path / f"{channel}.arena"
        path.write_bytes(b"\0" * (struct.calcsize("<QQ") + capacity))
        arenas[channel] = _Arena(str(path), capacity)
    codec = _StepCodec(
        {channel: types["big.Payload"] for channel in ("left", "right", "payload")},
        arenas,
    )
    return codec, arenas, types["big.Payload"]


class TestPythonStepCodec:
    """Exercise the child-side mirror of each protocol representation."""

    @pytest.mark.parametrize("arena_channels", [(), ("payload",)])
    def test_single_payload_round_trips_inline_or_arena(
        self, tmp_path, arena_channels
    ):
        codec, arenas, message_type = _python_codec(tmp_path, arena_channels)
        try:
            encoded = codec.encode_outputs([("payload", payload(7))])[0]
            assert ("shm_seq" in encoded) is bool(arena_channels)
            encoded["t"] = 123
            decoded = codec.decode_inputs([encoded])
            assert decoded[0].channel == "payload"
            assert decoded[0].publish_ns == 123
            assert message_type.pack(**decoded[0].data) == message_type.pack(
                **payload(7)
            )
        finally:
            for arena in arenas.values():
                arena.close()

    def test_burst_uses_arena_once_and_resets_next_step(self, tmp_path):
        codec, arenas, _ = _python_codec(tmp_path, ("payload",))
        try:
            first = codec.encode_outputs(
                [("payload", payload(1)), ("payload", payload(2))]
            )
            second = codec.encode_outputs([("payload", payload(3))])
            assert ["shm_seq" in item for item in first] == [True, False]
            assert "shm_seq" in second[0]
            assert second[0]["shm_seq"] == first[0]["shm_seq"] + 1
        finally:
            for arena in arenas.values():
                arena.close()

    def test_mixed_channels_honour_each_message_representation(self, tmp_path):
        codec, arenas, _ = _python_codec(tmp_path, ("left",))
        try:
            encoded = codec.encode_outputs([
                ("left", payload(0)),
                ("right", payload(1)),
                ("left", payload(2)),
                ("right", payload(3)),
            ])
            assert ["shm_seq" in item for item in encoded] == [
                True, False, False, False
            ]
            for index, item in enumerate(encoded):
                item["t"] = index
            assert [item.data["id"] for item in codec.decode_inputs(encoded)] == [
                0, 1, 2, 3
            ]
        finally:
            for arena in arenas.values():
                arena.close()

    def test_stale_sequence_and_capacity_are_runtime_failures(self, tmp_path):
        codec, arenas, message_type = _python_codec(tmp_path, ("payload",))
        try:
            with pytest.raises(ParticipantFailure, match="stale arena"):
                codec.decode_inputs([
                    {"ch": "payload", "t": 0, "shm_seq": 1}
                ])
            with pytest.raises(ParticipantFailure, match="payload exceeds arena capacity"):
                arenas["payload"].write(b"x" * (message_type.size + 1))
        finally:
            for arena in arenas.values():
                arena.close()


def _cpp_manifest(*, transport, source, sink=None):
    """Build a process route for the kernel-side codec conformance cases."""
    m = Manifest(duration_ns=30_000_000)
    m.add_schemas(ARRAY_SCHEMAS)
    m.add_channel("payload", schema="big.Payload", transport=transport)
    m.add_process(
        "source",
        command=[sys.executable, str(ROOT / "tests" / "participants" / source)],
        step_period_ns=10_000_000,
        publishes=["payload"],
    )
    if sink is not None:
        m.add_channel("mirror", schema="big.Payload", transport=transport)
        m.add_process(
            "sink",
            command=[sys.executable, str(ROOT / "tests" / "participants" / sink)],
            step_period_ns=10_000_000,
            subscribes=["payload"],
            publishes=["mirror"],
        )
    return m


def _recorded_message_ids(mcap_path, channel):
    """Read fixed-layout message IDs from one recorded channel."""
    _, messages = read_mcap(mcap_path)
    return [
        ARRAY_TYPES["big.Payload"].unpack(data)["id"]
        for name, _, data in messages
        if name == channel
    ]


class TestCppStepCodec:
    """Exercise the kernel codec through the real process-participant boundary."""

    @pytest.mark.parametrize("transport", ["inline", "shm"])
    def test_single_payload_round_trip(self, run_sil, tmp_path, transport):
        manifest = _cpp_manifest(transport=transport, source="array_source.py")
        proc = run_sil(manifest.write(tmp_path / f"{transport}.json").path)
        assert proc.returncode == 0, proc.stderr
        assert _recorded_message_ids(proc.mcap_path, "payload") == [0, 1, 2]

    def test_burst_fallback_and_mixed_channels_preserve_publish_order(
        self, run_sil, tmp_path
    ):
        mixed = Manifest(duration_ns=30_000_000)
        mixed.add_schemas(ARRAY_SCHEMAS)
        mixed.add_channel("left", schema="big.Payload", transport="shm")
        mixed.add_channel("right", schema="big.Payload")
        mixed.add_channel("mirror", schema="big.Payload", transport="shm")
        mixed.add_process(
            "source",
            command=[
                sys.executable,
                str(ROOT / "tests" / "participants" / "multi_channel_burst_source.py"),
            ],
            step_period_ns=10_000_000,
            publishes=["left", "right"],
        )
        mixed.add_process(
            "sink",
            command=[
                sys.executable,
                str(ROOT / "tests" / "participants" / "array_echo.py"),
            ],
            step_period_ns=10_000_000,
            subscribes=["left", "right"],
            publishes=["mirror"],
        )
        proc = run_sil(mixed.write(tmp_path / "mixed.json").path)
        assert proc.returncode == 0, proc.stderr
        assert _recorded_message_ids(proc.mcap_path, "mirror") == list(range(8))

        inline = _cpp_manifest(
            transport="inline",
            source="burst_array_source.py",
            sink="array_echo.py",
        )
        shm = _cpp_manifest(
            transport="shm",
            source="burst_array_source.py",
            sink="array_echo.py",
        )
        inline_proc = run_sil(inline.write(tmp_path / "burst-inline.json").path)
        shm_proc = run_sil(shm.write(tmp_path / "burst-shm.json").path)
        assert inline_proc.returncode == 0, inline_proc.stderr
        assert shm_proc.returncode == 0, shm_proc.stderr
        assert _recorded_message_ids(shm_proc.mcap_path, "mirror") == _recorded_message_ids(
            inline_proc.mcap_path, "mirror"
        )
        assert len(_recorded_message_ids(shm_proc.mcap_path, "mirror")) >= 3

    def test_cpp_input_wire_uses_arena_once_then_inline_fallback(
        self, run_sil, tmp_path
    ):
        """Inspect the raw C++ input line for burst and mixed-channel cases."""
        log_path = tmp_path / "protocol.json"
        m = Manifest(duration_ns=60_000_000)
        m.add_schemas(ARRAY_SCHEMAS)
        m.add_channel("left", schema="big.Payload", transport="shm")
        m.add_channel("right", schema="big.Payload")
        m.add_process(
            "source",
            command=[
                sys.executable,
                str(ROOT / "tests" / "participants" / "multi_channel_burst_source.py"),
            ],
            step_period_ns=10_000_000,
            publishes=["left", "right"],
        )
        m.add_process(
            "probe",
            command=[
                sys.executable,
                str(ROOT / "tests" / "participants" / "protocol_probe.py"),
                str(log_path),
            ],
            step_period_ns=30_000_000,
            subscribes=["left", "right"],
        )
        proc = run_sil(m.write(tmp_path / "wire.json").path)
        assert proc.returncode == 0, proc.stderr

        steps = json.loads(log_path.read_text())
        inputs = next(step["in"] for step in steps if step["in"])
        assert [item["ch"] for item in inputs] == [
            "left", "right", "left", "right",
        ] * 3
        assert ["shm_seq" in item for item in inputs] == [
            True, False, False, False,
            False, False, False, False,
            False, False, False, False,
        ]

    @pytest.mark.parametrize(
        ("mode", "diagnostic"),
        [("stale", "stale arena"), ("capacity", "arena len exceeds capacity")],
    )
    def test_malformed_arena_output_is_a_runtime_failure(
        self, run_sil, tmp_path, mode, diagnostic
    ):
        m = Manifest(duration_ns=10_000_000)
        m.add_schemas(ARRAY_SCHEMAS)
        m.add_channel("payload", schema="big.Payload", transport="shm")
        m.add_process(
            "fault",
            command=[
                sys.executable,
                str(ROOT / "tests" / "participants" / "protocol_fault.py"),
                mode,
            ],
            step_period_ns=10_000_000,
            publishes=["payload"],
        )
        proc = run_sil(m.write(tmp_path / f"{mode}.json").path)
        assert proc.returncode == 1
        assert diagnostic in proc.stderr
