"""Conformance cases for the C++ and Python step transport codecs."""

import json
import struct
import sys
from pathlib import Path

import pytest

from sil import schema
from sil.manifest import Manifest, SubscriberRoute
from sil.participant import (
    ParticipantFailure,
    _Arena,
    _StepCodec,
)
from sil.testing import participant_command

from conftest import COMPAT_ROUTE_CAPACITY, ROOT
from test_run_boundary import ARRAY_SCHEMAS, ARRAY_TYPES, read_mcap


def payload(message_id):
    """Build the fixed-layout payload used by the process fixtures."""
    return {
        "id": message_id,
        "blob": bytes((message_id + k) % 256 for k in range(4)),
        "samples": [float(message_id * 10 + k) for k in range(8)],
    }


def _python_codec(tmp_path, arena_channels, *, slots=1):
    """Return a Python codec and its mappings, closing arenas after use."""
    types = schema.load(ARRAY_SCHEMAS)
    arenas = {}
    for channel in arena_channels:
        capacity = types["big.Payload"].size
        path = tmp_path / f"{channel}.arena"
        path.write_bytes(b"\0" * ((struct.calcsize("<QQ") + capacity) * slots))
        arenas[channel] = _Arena(str(path), capacity, slots)
    codec = _StepCodec(
        {channel: types["big.Payload"] for channel in ("left", "right", "payload")},
        arenas,
        indexed_slots=slots > 1,
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
            if arena_channels:
                assert "shm_slot" not in encoded
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

    def test_burst_uses_declared_slots_then_falls_back_and_resets(self, tmp_path):
        codec, arenas, _ = _python_codec(tmp_path, ("payload",), slots=2)
        try:
            first = codec.encode_outputs(
                [
                    ("payload", payload(1)),
                    ("payload", payload(2)),
                    ("payload", payload(3)),
                ]
            )
            second = codec.encode_outputs([("payload", payload(4))])
            assert ["shm_seq" in item for item in first] == [True, True, False]
            assert [item.get("shm_slot") for item in first] == [0, 1, None]
            assert "shm_seq" in second[0]
            assert second[0]["shm_slot"] == 0
            assert second[0]["shm_seq"] > first[0]["shm_seq"]
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

    def test_each_slot_has_an_independent_freshness_marker(self, tmp_path):
        codec, arenas, _ = _python_codec(tmp_path, ("payload",), slots=2)
        try:
            encoded = codec.encode_outputs([
                ("payload", payload(1)),
                ("payload", payload(2)),
            ])
            with pytest.raises(ParticipantFailure, match="stale arena slot 1"):
                codec.decode_inputs([{
                    **encoded[1],
                    "t": 0,
                    "shm_seq": encoded[0]["shm_seq"],
                }])
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
            subscribes=[SubscriberRoute("payload", capacity=COMPAT_ROUTE_CAPACITY)],
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

    def test_legacy_manifest_without_slots_offers_protocol_one(
        self, run_sil, tmp_path
    ):
        log_path = tmp_path / "legacy-init.json"
        m = Manifest(duration_ns=10_000_000)
        m.add_schemas(ARRAY_SCHEMAS)
        m.add_channel("payload", schema="big.Payload", transport="shm")
        m.add_process(
            "probe",
            command=[
                sys.executable,
                str(ROOT / "tests" / "participants" / "protocol_probe.py"),
                str(log_path),
                "echo",
            ],
            step_period_ns=10_000_000,
            subscribes=[SubscriberRoute("payload", capacity=COMPAT_ROUTE_CAPACITY)],
        )
        document = m.to_doc()
        del document["channels"]["payload"]["slots"]
        path = tmp_path / "legacy.json"
        path.write_text(
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
        )

        proc = run_sil(path)

        assert proc.returncode == 0, proc.stderr
        steps = json.loads(log_path.read_text())
        assert steps[0]["protocol"] == 1
        assert steps[0]["arenas"]["payload"]["slots"] == 1

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
            subscribes=[
                SubscriberRoute("left", capacity=COMPAT_ROUTE_CAPACITY),
                SubscriberRoute("right", capacity=COMPAT_ROUTE_CAPACITY),
            ],
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

    def test_legacy_participant_negotiates_one_slot_over_multislot_manifest(
        self, run_sil, tmp_path
    ):
        """An absent ready.protocol keeps the established one-slot wire."""
        log_path = tmp_path / "protocol.json"
        m = Manifest(duration_ns=90_000_000)
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
            subscribes=[
                SubscriberRoute("left", capacity=COMPAT_ROUTE_CAPACITY),
                SubscriberRoute("right", capacity=COMPAT_ROUTE_CAPACITY),
            ],
        )
        proc = run_sil(m.write(tmp_path / "wire.json").path)
        assert proc.returncode == 0, proc.stderr

        steps = json.loads(log_path.read_text())
        populated_steps = [step["in"] for step in steps if step["in"]]
        assert len(populated_steps) >= 2
        for inputs in populated_steps:
            assert all(step["protocol"] == 2 for step in steps)
            assert inputs[0]["ch"] == "left"
            left_items = [item for item in inputs if item["ch"] == "left"]
            assert len(left_items) >= 2
            assert "shm_seq" in left_items[0]
            assert "shm_slot" not in left_items[0]
            assert all(
                "data" in item and "shm_seq" not in item
                for item in left_items[1:]
            )

    def test_cpp_input_wire_uses_declared_slots_then_inline_fallback(
        self, run_sil, tmp_path
    ):
        """Protocol 2 uses both declared slots without changing Publish order."""
        log_path = tmp_path / "protocol-v2.json"
        m = Manifest(duration_ns=90_000_000)
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
                "echo",
            ],
            step_period_ns=30_000_000,
            subscribes=[
                SubscriberRoute("left", capacity=COMPAT_ROUTE_CAPACITY),
                SubscriberRoute("right", capacity=COMPAT_ROUTE_CAPACITY),
            ],
        )

        proc = run_sil(m.write(tmp_path / "wire-v2.json").path)

        assert proc.returncode == 0, proc.stderr
        steps = json.loads(log_path.read_text())
        populated_steps = [step["in"] for step in steps if step["in"]]
        assert populated_steps
        shape = steps[0]["arenas"]["left"]
        assert shape["slots"] == 2
        assert shape["file_size"] == shape["slots"] * (
            struct.calcsize("<QQ") + shape["capacity"]
        )
        for inputs in populated_steps:
            assert [item["ch"] for item in inputs] == [
                "left", "right"
            ] * (len(inputs) // 2)
            left_items = [item for item in inputs if item["ch"] == "left"]
            assert len(left_items) > 2
            assert [item.get("shm_slot") for item in left_items[:2]] == [0, 1]
            assert all("shm_seq" in item for item in left_items[:2])
            assert all(
                "data" in item and "shm_seq" not in item
                for item in left_items[2:]
            )

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


class TestInitializationFailure:
    """A participant that never reaches `ready`, and why it did not.

    A participant that rejects its init line answers `fail`, and the kernel
    calls that a configuration error — a Manifest that names it this way is
    wrong, not a Run that went wrong. A participant that merely breaks on the
    way up says nothing, and stays an ordinary Run failure.
    """

    def manifest(self, tmp_path, cls: str) -> Path:
        m = Manifest(duration_ns=10_000_000)
        m.add_schemas(ARRAY_SCHEMAS)
        m.add_channel("payload", schema="big.Payload")
        m.add_process(
            "starter",
            command=participant_command(
                ROOT / "tests" / "participants" / "reject_at_init.py", cls
            ),
            step_period_ns=10_000_000,
            publishes=["payload"],
        )
        return m.write(tmp_path / f"{cls}.json").path

    def test_a_rejected_init_line_is_a_configuration_error(
        self, run_sil, tmp_path
    ):
        proc = run_sil(self.manifest(tmp_path, "RejectAtInit"))
        assert proc.returncode == 2, proc.stderr
        assert "starter" in proc.stderr
        assert "refuses its init line" in proc.stderr

    def test_any_other_failure_while_initializing_stays_a_run_failure(
        self, run_sil, tmp_path
    ):
        proc = run_sil(self.manifest(tmp_path, "BreakAtInit"))
        assert proc.returncode == 1, proc.stderr
        assert "broke while initializing" in proc.stderr

    def test_a_reported_initialization_failure_keeps_its_run_diagnostic(
        self, run_sil, tmp_path
    ):
        proc = run_sil(self.manifest(tmp_path, "FailAtInit"))
        assert proc.returncode == 1, proc.stderr
        assert "the participant's own initialization failed" in proc.stderr
