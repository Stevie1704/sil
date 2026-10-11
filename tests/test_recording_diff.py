"""Where two Recordings first differ, outside and inside the Messages
(issue #271). A Run of one Manifest cannot write a different metadata record
or Schema, so these Recordings are written by hand."""

import hashlib
import json

from mcap.writer import CompressionType, Writer

from sil.recording_diff import first_difference

COUNTER = {"fields": [{"name": "seq", "type": "u32"},
                      {"name": "value", "type": "u32"}]}


def recording(path, *, manifest_hash="abc", channels=None, messages=(),
              library="sil"):
    """An MCAP Recording as a Run writes one: a 'sil' metadata record, the
    Channels in order, and the Messages as (channel, t_ns, payload)."""
    channels = {"echo": COUNTER} if channels is None else channels
    with open(path, "wb") as f:
        writer = Writer(f, compression=CompressionType.NONE)
        writer.start("sil", library)
        writer.add_metadata("sil", {"manifest_hash": manifest_hash})
        ids = {}
        for name, schema in channels.items():
            schema_id = writer.register_schema(name, "sil_pod",
                                               json.dumps(schema).encode())
            ids[name] = writer.register_channel(name, "sil_pod", schema_id)
        for sequence, (channel, t_ns, payload) in enumerate(messages):
            writer.add_message(ids[channel], log_time=t_ns, data=payload,
                               publish_time=t_ns, sequence=sequence)
        writer.finish()
    return path


def test_identical_recordings_have_no_difference(tmp_path):
    messages = [("echo", 0, b"\0" * 8)]

    assert first_difference(recording(tmp_path / "a.mcap", messages=messages),
                            recording(tmp_path / "b.mcap", messages=messages)
                            ) is None


def test_a_metadata_difference_is_reported_as_metadata(tmp_path):
    first = first_difference(recording(tmp_path / "a.mcap", manifest_hash="a"),
                             recording(tmp_path / "b.mcap", manifest_hash="b"))

    assert first["kind"] == "metadata"
    assert first["run1"] == [{"name": "sil", "metadata": {"manifest_hash": "a"}}]
    assert first["run2"] == [{"name": "sil", "metadata": {"manifest_hash": "b"}}]
    assert "metadata" in first["detail"]


def test_a_different_channel_set_is_reported_as_channels(tmp_path):
    first = first_difference(
        recording(tmp_path / "a.mcap", channels={"echo": COUNTER}),
        recording(tmp_path / "b.mcap", channels={"echo": COUNTER,
                                                  "extra": COUNTER}))

    assert first["kind"] == "channels"
    assert first["run1"] == ["echo"]
    assert first["run2"] == ["echo", "extra"]


def test_a_different_schema_names_its_channel(tmp_path):
    wider = {"fields": [{"name": "seq", "type": "u64"},
                        {"name": "value", "type": "u32"}]}

    first = first_difference(
        recording(tmp_path / "a.mcap", channels={"echo": COUNTER}),
        recording(tmp_path / "b.mcap", channels={"echo": wider}))

    assert first["kind"] == "schema"
    assert first["channel"] == "echo"
    assert first["run1"] == {"name": "echo", "schema": COUNTER}
    assert first["run2"] == {"name": "echo", "schema": wider}


def test_a_message_only_one_recording_holds_is_the_first_difference(tmp_path):
    common = [("echo", 0, b"\0" * 8), ("echo", 10, b"\1" * 8)]

    first = first_difference(
        recording(tmp_path / "a.mcap", messages=common),
        recording(tmp_path / "b.mcap", messages=[*common,
                                                  ("echo", 20, b"\2" * 8)]))

    assert first["kind"] == "message"
    assert (first["channel"], first["t_ns"], first["message_index"]) == (
        "echo", 20, 2)
    assert first["run1"] is None
    assert first["run2"] == {
        "channel": "echo", "t_ns": 20,
        # sha256 of eight 0x02 bytes
        "payload_sha256": hashlib.sha256(b"\2" * 8).hexdigest()}
    assert first["detail"] == ("Message 2 in stored order differs: run 1 has "
                               "no Message, run 2 has 'echo' at t_ns 20")


def test_a_difference_outside_metadata_channels_and_messages_is_bytes(tmp_path):
    first = first_difference(recording(tmp_path / "a.mcap", library="sil"),
                             recording(tmp_path / "b.mcap", library="other"))

    assert first["kind"] == "bytes"
    assert first["channel"] is None and first["t_ns"] is None
