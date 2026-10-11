"""`sil recording channels` and `sil recording show`: read a Recording with the
Schemas stored in it (#270)."""

import json
import struct

import pytest
from mcap.writer import CompressionType, Writer

from sil import cli
from sil.examples.acc import manifest
from sil.testing import run_simulation

STEP_NS = manifest.STEP_PERIOD_NS
STEPS = manifest.DURATION_NS // STEP_NS


@pytest.fixture(scope="module")
def acc(sil_run, tmp_path_factory):
    """One Run of the packaged ACC example: its Recording and Manifest hash."""
    return run_simulation(manifest.acc_manifest(), runner=sil_run,
                          workdir=tmp_path_factory.mktemp("acc"))


def sil(capsys, *argv):
    code = cli.main(["recording", *argv])
    out, err = capsys.readouterr()
    return code, out, err


def test_channels_lists_each_channel_with_its_schema_count_and_times(acc, capsys):
    code, out, _ = sil(capsys, "channels", str(acc.mcap_path), "--json")

    assert code == 0
    report = json.loads(out)
    assert report["sil_recording_channels"] == 1
    assert report["manifest_hash"] == acc.manifest_hash
    by_name = {c["channel"]: c for c in report["channels"]}
    assert by_name["acc.Sensing"] == {
        "channel": "acc.Sensing", "schema": "acc.Sensing", "messages": STEPS,
        "first_t_ns": 0, "last_t_ns": (STEPS - 1) * STEP_NS}
    assert by_name["acc.Command"] == {
        "channel": "acc.Command", "schema": "acc.Command", "messages": STEPS - 1,
        "first_t_ns": STEP_NS, "last_t_ns": (STEPS - 1) * STEP_NS}


def test_show_json_prints_one_decoded_message_per_line_in_the_selection(
        acc, capsys):
    code, out, _ = sil(capsys, "show", str(acc.mcap_path), "--json",
                       "--channel", "acc.Command",
                       "--from", str(2 * STEP_NS), "--to", str(5 * STEP_NS))

    assert code == 0
    lines = [json.loads(line) for line in out.splitlines()]
    expected = [{"channel": "acc.Command", "t_ns": t, "fields": fields}
                for t, fields in acc.messages("acc.Command")
                if 2 * STEP_NS <= t < 5 * STEP_NS]
    assert lines == expected
    assert [line["t_ns"] for line in lines] == [2 * STEP_NS, 3 * STEP_NS,
                                                4 * STEP_NS]


def test_show_prints_text_and_stops_after_the_limit(acc, capsys):
    code, out, _ = sil(capsys, "show", str(acc.mcap_path), "--limit", "3")

    assert code == 0
    assert out == (
        "0 acc.Sensing gap_m=60.0 relative_speed_mps=0.0 ego_speed_mps=25.0\n"
        "10000000 acc.Sensing gap_m=60.0 relative_speed_mps=0.0 ego_speed_mps=25.0\n"
        "10000000 acc.Command accel_mps2=1.5\n")


def test_an_unknown_channel_is_a_usage_error_that_lists_the_channels(
        acc, capsys):
    code, out, err = sil(capsys, "show", str(acc.mcap_path),
                         "--channel", "acc.Speed")

    assert code == 2
    assert out == ""
    assert "'acc.Speed'" in err
    assert "acc.Command" in err and "acc.Sensing" in err


# One field per Schema type, and both kinds of array.
EVERY_TYPE = {"fields": [
    {"name": "a", "type": "u8"}, {"name": "b", "type": "u16"},
    {"name": "c", "type": "u32"}, {"name": "d", "type": "u64"},
    {"name": "e", "type": "i8"}, {"name": "f", "type": "i16"},
    {"name": "g", "type": "i32"}, {"name": "h", "type": "i64"},
    {"name": "x", "type": "f32"}, {"name": "y", "type": "f64"},
    {"name": "frame", "type": "u8", "count": 4},
    {"name": "samples", "type": "f64", "count": 3},
]}


def write_recording(path, spec, payload, *, other=None):
    """A Recording shaped like the kernel's: Channel `bus.Frame` with one
    Message at t_ns 7, and Channel `bus.Idle` without Messages when `other`
    gives its Schema."""
    with open(path, "wb") as f:
        writer = Writer(f, compression=CompressionType.NONE)
        writer.start(profile="sil", library="test")
        schema_id = writer.register_schema(
            "bus.Frame", "sil_pod", json.dumps(spec).encode())
        channel_id = writer.register_channel("bus.Frame", "sil_pod", schema_id)
        if other is not None:
            writer.register_channel("bus.Idle", "sil_pod", writer.register_schema(
                "bus.Idle", "sil_pod", json.dumps(other).encode()))
        writer.add_message(channel_id, log_time=7, data=payload, publish_time=7)
        writer.finish()
    return path


def test_show_json_decodes_every_schema_type_and_arrays(tmp_path, capsys):
    payload = struct.pack("<BHIQbhiqfd4s3d", 255, 65535, 4294967295,
                          2**64 - 1, -128, -32768, -2**31, -2**63, 0.5, 0.25,
                          bytes([0xde, 0xad, 0xbe, 0xef]),
                          float("nan"), float("inf"), float("-inf"))
    recording = write_recording(tmp_path / "frame.mcap", EVERY_TYPE, payload)

    code, out, _ = sil(capsys, "show", str(recording), "--json")

    assert code == 0
    assert json.loads(out) == {"channel": "bus.Frame", "t_ns": 7, "fields": {
        "a": 255, "b": 65535, "c": 4294967295, "d": 18446744073709551615,
        "e": -128, "f": -32768, "g": -2147483648, "h": -9223372036854775808,
        "x": 0.5, "y": 0.25, "frame": "deadbeef",
        "samples": ["NaN", "Infinity", "-Infinity"]}}


def test_channels_of_a_recording_no_run_wrote_has_no_manifest_hash(
        tmp_path, capsys):
    recording = write_recording(tmp_path / "frame.mcap",
                                {"fields": [{"name": "a", "type": "u8"}]},
                                b"\x01")

    code, out, _ = sil(capsys, "channels", str(recording))

    assert code == 0
    assert out == ("manifest_hash none\n"
                   "CHANNEL    SCHEMA     MESSAGES  FIRST_T_NS  LAST_T_NS\n"
                   "bus.Frame  bus.Frame  1         7           7\n")


@pytest.mark.parametrize("option", [["--from", "-1"], ["--to", "x"],
                                    ["--limit", "0"]])
def test_a_bad_selection_is_a_usage_error(option, tmp_path, capsys):
    with pytest.raises(SystemExit) as exit_:
        sil(capsys, "show", str(tmp_path / "frame.mcap"), *option)

    assert exit_.value.code == 2


@pytest.mark.parametrize("name", ["missing.mcap", "frame.csv", "empty.mcap"])
def test_an_unreadable_recording_is_a_usage_error(name, tmp_path, capsys):
    (tmp_path / "frame.csv").write_text("t\n")
    (tmp_path / "empty.mcap").write_bytes(b"")

    code, out, err = sil(capsys, "channels", str(tmp_path / name))

    assert code == 2
    assert out == ""
    assert err.startswith("sil recording channels: error: cannot read Recording")


def test_a_channel_without_messages_has_no_times(tmp_path, capsys):
    recording = write_recording(tmp_path / "frame.mcap",
                                {"fields": [{"name": "a", "type": "u8"}]},
                                b"\x01", other={"fields": []})

    code, out, _ = sil(capsys, "channels", str(recording), "--json")

    assert code == 0
    assert json.loads(out)["channels"][1] == {
        "channel": "bus.Idle", "schema": "bus.Idle", "messages": 0,
        "first_t_ns": None, "last_t_ns": None}


def test_show_decodes_a_selected_channel_beside_a_foreign_schema(
        tmp_path, capsys):
    recording = write_recording(tmp_path / "frame.mcap",
                                {"fields": [{"name": "a", "type": "u8"}]},
                                b"\x01", other={"fields": [{"name": "z",
                                                             "type": "f128"}]})

    assert sil(capsys, "show", str(recording), "--channel", "bus.Frame") == (
        0, "7 bus.Frame a=1\n", "")
    code, _, err = sil(capsys, "show", str(recording))
    assert code == 2
    assert "'bus.Idle' has a Schema that is not a SiL Schema" in err
