"""Exporting a Recording as a visualization MCAP (issue #346, spec #345 s1-2).

Sources are written as `sil_pod` MCAP, as the kernel writes them
(tests/visualization_fixture.py), so each test states its own types, times
and Publish order. Every expected
payload is a literal stated by hand from those inputs.
"""

from __future__ import annotations

import hashlib
import json
import struct
import subprocess

import pytest
from mcap.reader import make_reader
from mcap.writer import Writer
from visualization_fixture import write_qualification_source, write_source

from sil import cli
from sil.visualization import (ExportInputError, ExportOutputError,
                               export_recording)
from sil.visualization.export import receipt_path


def exported(path) -> list[tuple[str, int, dict]]:
    """(topic, log time, payload) of every output Message, in stored order."""
    with open(path, "rb") as stream:
        return [(channel.topic, message.log_time, json.loads(message.data))
                for _, channel, message in make_reader(stream).iter_messages(
                    log_time_order=False)]


SPEED = {"v.Speed": {"fields": [{"name": "speed_mps", "type": "f64"},
                                {"name": "counter", "type": "u32"}]}}


def test_a_channel_becomes_a_json_topic_with_the_sil_envelope(tmp_path):
    source = write_source(tmp_path / "run.mcap", SPEED, {"speed": "v.Speed"},
                          [("speed", 2_000_000_000,
                            {"speed_mps": 12.5, "counter": 9})])
    out = tmp_path / "run.visualization.mcap"

    export_recording(source, out, profile="visualization-json")

    assert exported(out) == [("speed", 2_000_000_000, {
        "sil": {"publication_time_ns": "2000000000", "record_index": "0",
                "nonfinite": {}},
        "fields": {"speed_mps": 12.5, "counter": 9},
    })]
    with open(out, "rb") as stream:
        summary = make_reader(stream).get_summary()
    (channel,) = summary.channels.values()
    assert channel.message_encoding == "json"
    assert summary.schemas[channel.schema_id].encoding == "jsonschema"


ALL_TYPES = {"t.All": {"fields": [
    {"name": "u8", "type": "u8"}, {"name": "u16", "type": "u16"},
    {"name": "u32", "type": "u32"}, {"name": "u64", "type": "u64"},
    {"name": "i8", "type": "i8"}, {"name": "i16", "type": "i16"},
    {"name": "i32", "type": "i32"}, {"name": "i64", "type": "i64"},
    {"name": "f32", "type": "f32"}, {"name": "f64", "type": "f64"},
    {"name": "blob", "type": "u8", "count": 3},
    {"name": "u16s", "type": "u16", "count": 2},
    {"name": "u32s", "type": "u32", "count": 2},
    {"name": "i8s", "type": "i8", "count": 2},
    {"name": "i16s", "type": "i16", "count": 2},
    {"name": "i32s", "type": "i32", "count": 2},
    {"name": "i64s", "type": "i64", "count": 2},
    {"name": "u64s", "type": "u64", "count": 1},
    {"name": "f32s", "type": "f32", "count": 3},
    {"name": "f64s", "type": "f64", "count": 2},
]}}

MAXIMA = {"u8": 255, "u16": 65535, "u32": 4294967295,
          "u64": 18446744073709551615, "i8": 127, "i16": 32767,
          "i32": 2147483647, "i64": 9223372036854775807,
          "f32": 0.1, "f64": 1.7976931348623157e308,
          "blob": b"\x00\xff\x10", "u16s": [0, 65535],
          "u32s": [0, 4294967295], "i8s": [-128, 127],
          "i16s": [-32768, 32767], "i32s": [-2147483648, 2147483647],
          "i64s": [9007199254740993, -9223372036854775808],
          "u64s": [9007199254740993], "f32s": [1.5, -0.0, 3.0],
          "f64s": [2.0, -2.5]}
MINIMA = {"u8": 0, "u16": 0, "u32": 0, "u64": 0, "i8": -128, "i16": -32768,
          "i32": -2147483648, "i64": -9223372036854775808,
          "f32": float("nan"), "f64": float("-inf"), "blob": b"abc",
          "u16s": [1, 2], "u32s": [1, 2], "i8s": [0, -1], "i16s": [0, -1],
          "i32s": [0, -1], "i64s": [0, -1], "u64s": [0],
          "f32s": [float("inf"), 0.25, float("nan")],
          "f64s": [float("-inf"), 1e-300]}


def test_every_type_has_its_profile_representation(tmp_path):
    source = write_source(tmp_path / "run.mcap", ALL_TYPES, {"all": "t.All"},
                          [("all", 0, MAXIMA), ("all", 1, MINIMA)])
    out = tmp_path / "out.mcap"

    export_recording(source, out)

    (_, _, high), (_, _, low) = exported(out)
    assert high == {"sil": {"publication_time_ns": "0", "record_index": "0",
                            "nonfinite": {}}, "fields": {
        "u8": 255, "u16": 65535, "u32": 4294967295,
        "u64": "18446744073709551615", "i8": 127, "i16": 32767,
        "i32": 2147483647, "i64": "9223372036854775807",
        "f32": 0.10000000149011612, "f64": 1.7976931348623157e308,
        "blob": "AP8Q", "u16s": [0, 65535],
        "u32s": [0, 4294967295], "i8s": [-128, 127],
        "i16s": [-32768, 32767], "i32s": [-2147483648, 2147483647],
        "i64s": ["9007199254740993", "-9223372036854775808"],
        "u64s": ["9007199254740993"], "f32s": [1.5, -0.0, 3.0],
        "f64s": [2.0, -2.5]}}
    assert low == {"sil": {"publication_time_ns": "1", "record_index": "1",
                           "nonfinite": {
                               "/fields/f32": "NaN", "/fields/f64": "-Infinity",
                               "/fields/f32s/0": "Infinity",
                               "/fields/f32s/2": "NaN",
                               "/fields/f64s/0": "-Infinity"}}, "fields": {
        "u8": 0, "u16": 0, "u32": 0, "u64": "0", "i8": -128, "i16": -32768,
        "i32": -2147483648, "i64": "-9223372036854775808",
        "f32": None, "f64": None, "blob": "YWJj", "u16s": [1, 2],
        "u32s": [1, 2], "i8s": [0, -1], "i16s": [0, -1], "i32s": [0, -1],
        "i64s": ["0", "-1"], "u64s": ["0"], "f32s": [None, 0.25, None],
        "f64s": [None, 1e-300]}}


def test_f32_values_round_trip_through_json_to_the_decoded_value(tmp_path):
    source = write_source(tmp_path / "run.mcap", ALL_TYPES, {"all": "t.All"},
                          [("all", 0, MAXIMA)])
    out = tmp_path / "out.mcap"

    export_recording(source, out)

    (_, _, payload), = exported(out)
    assert struct.pack("<f", payload["fields"]["f32"]) == struct.pack("<f", 0.1)


def output_schema(path, topic: str) -> dict:
    with open(path, "rb") as stream:
        summary = make_reader(stream).get_summary()
    (channel,) = [c for c in summary.channels.values() if c.topic == topic]
    return json.loads(summary.schemas[channel.schema_id].data)


def test_the_generated_schema_names_original_types_and_array_lengths(tmp_path):
    source = write_source(tmp_path / "run.mcap", ALL_TYPES, {"all": "t.All"}, [])
    out = tmp_path / "out.mcap"

    export_recording(source, out)

    schema = output_schema(out, "all")
    assert schema["title"] == "t.All"
    assert schema["required"] == ["sil", "fields"]
    fields = schema["properties"]["fields"]["properties"]
    assert fields["u32"] == {"type": "integer", "minimum": 0,
                             "maximum": 4294967295, "description": "SiL u32"}
    assert fields["i64"]["type"] == "string"
    assert fields["i64"]["description"] == "SiL i64 (decimal string)"
    assert fields["blob"] == {"type": "string", "contentEncoding": "base64",
                              "description": "SiL u8[3] (base64)"}
    assert fields["f32s"]["minItems"] == fields["f32s"]["maxItems"] == 3
    assert fields["f32s"]["items"] == {"type": ["number", "null"]}
    assert fields["f32s"]["description"] == (
        "SiL f32[3] (null when non-finite, see /sil/nonfinite)")
    assert fields["i64s"]["items"]["type"] == "string"
    assert fields["i64s"]["description"] == "SiL i64[2] (decimal string)"


def test_equal_declarations_under_different_names_keep_their_own_titles(
        tmp_path):
    declaration = {"fields": [{"name": "v", "type": "u8"}]}
    source = write_source(tmp_path / "run.mcap",
                          {"a.Left": declaration, "b.Right": declaration},
                          {"left": "a.Left", "right": "b.Right"}, [])
    out = tmp_path / "out.mcap"

    export_recording(source, out)

    assert output_schema(out, "left")["title"] == "a.Left"
    assert output_schema(out, "right")["title"] == "b.Right"


def test_source_fields_named_like_the_envelope_do_not_collide(tmp_path):
    schemas = {"c.Clash": {"fields": [{"name": "sil", "type": "u8"},
                                      {"name": "fields", "type": "u8"},
                                      {"name": "nonfinite", "type": "f64"},
                                      {"name": "record_index", "type": "u8"}]}}
    source = write_source(tmp_path / "run.mcap", schemas, {"c": "c.Clash"},
                          [("c", 5, {"sil": 1, "fields": 2,
                                     "nonfinite": float("nan"),
                                     "record_index": 3})])
    out = tmp_path / "out.mcap"

    export_recording(source, out)

    assert exported(out) == [("c", 5, {
        "sil": {"publication_time_ns": "5", "record_index": "0",
                "nonfinite": {"/fields/nonfinite": "NaN"}},
        "fields": {"sil": 1, "fields": 2, "nonfinite": None,
                   "record_index": 3}})]


TWO = {"s.A": {"fields": [{"name": "v", "type": "i32"}]},
       "s.B": {"fields": [{"name": "w", "type": "u64"}]}}

# Publish order with an equal-time Burst at 20 (a, b, a), a regressing stored
# time (15 after 20), and Channel `b` sparse, with one Message only.
STORED = [("a", 10, {"v": 1}), ("a", 20, {"v": 2}),
          ("b", 20, {"w": 9007199254740993}), ("a", 20, {"v": 3}),
          ("a", 15, {"v": 4})]


def locators(path) -> list[tuple[str, int, str, str]]:
    return [(topic, time_ns, payload["sil"]["record_index"],
             payload["sil"]["publication_time_ns"])
            for topic, time_ns, payload in exported(path)]


def test_stored_order_times_and_whole_source_locators_are_preserved(tmp_path):
    source = write_source(tmp_path / "run.mcap", TWO,
                          {"a": "s.A", "b": "s.B", "never": "s.A"}, STORED)
    out = tmp_path / "out.mcap"

    export_recording(source, out)

    assert locators(out) == [("a", 10, "0", "10"), ("a", 20, "1", "20"),
                             ("b", 20, "2", "20"), ("a", 20, "3", "20"),
                             ("a", 15, "4", "15")]
    with open(out, "rb") as stream:
        summary = make_reader(stream).get_summary()
    # A Channel without Messages is still declared.
    assert sorted(c.topic for c in summary.channels.values()) == [
        "a", "b", "never"]
    assert summary.statistics.message_count == 5
    assert summary.chunk_indexes and all(i.message_index_offsets
                                         for i in summary.chunk_indexes)


def test_a_channel_subset_keeps_the_whole_source_record_index(tmp_path):
    source = write_source(tmp_path / "run.mcap", TWO, {"a": "s.A", "b": "s.B"},
                          STORED)
    out = tmp_path / "out.mcap"

    receipt = export_recording(source, out, channels=["b"])

    assert locators(out) == [("b", 20, "2", "20")]
    with open(out, "rb") as stream:
        summary = make_reader(stream).get_summary()
    assert [c.topic for c in summary.channels.values()] == ["b"]
    assert receipt["selection"] == {"channels": ["b"]}
    assert receipt["messages"] == 1


def sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_receipt_binds_source_and_output_and_reports_counts(tmp_path):
    source = write_source(tmp_path / "run.mcap", TWO,
                          {"a": "s.A", "b": "s.B", "never": "s.A"}, STORED,
                          metadata={"manifest_hash": "ab" * 32})
    before = sha256(source)
    out = tmp_path / "run.visualization.mcap"

    returned = export_recording(source, out)

    receipt = json.loads((tmp_path / "run.visualization.mcap.receipt.json")
                         .read_text())
    assert receipt == returned
    assert sha256(source) == before
    assert receipt["sil_visualization_export"] == 1
    assert receipt["profile"] == {"name": "sil_visualization_json",
                                  "version": 1}
    assert receipt["source"]["sha256"] == before
    assert receipt["source"]["manifest_hash"] == "ab" * 32
    assert receipt["output"]["sha256"] == sha256(out)
    assert receipt["output"]["size_bytes"] == out.stat().st_size
    assert receipt["messages"] == 5
    assert receipt["bounds"] == {"start_ns": "10", "end_ns": "20"}
    assert receipt["selection"] == {"channels": None}
    assert [(c["topic"], c["source_schema"], c["messages"], c["bounds"])
            for c in receipt["channels"]] == [
        ("a", "s.A", 4, {"start_ns": "10", "end_ns": "20"}),
        ("b", "s.B", 1, {"start_ns": "20", "end_ns": "20"}),
        ("never", "s.A", 0, None)]
    assert receipt["completeness"]["run_outcome"] == "unknown"
    assert "visualization" in receipt["derivative"]


def test_output_metadata_binds_the_source_digest_and_profile(tmp_path):
    source = write_source(tmp_path / "run.mcap", TWO, {"a": "s.A"}, [])
    out = tmp_path / "out.mcap"

    export_recording(source, out)

    with open(out, "rb") as stream:
        reader = make_reader(stream)
        (channel,) = reader.get_summary().channels.values()
        (record,) = list(reader.iter_metadata())
    assert channel.metadata["sil_source_sha256"] == sha256(source)
    assert channel.metadata["sil_source_schema"] == "s.A"
    assert channel.metadata["sil_profile"] == "sil_visualization_json"
    assert channel.metadata["sil_profile_version"] == "1"
    assert record.name == "sil_visualization"
    assert record.metadata["source_sha256"] == sha256(source)
    assert json.loads(record.metadata["channels"]) == ["a"]


def test_an_empty_source_is_a_valid_export_without_a_playback_range(tmp_path):
    source = write_source(tmp_path / "run.mcap", TWO, {"a": "s.A"}, [])
    out = tmp_path / "out.mcap"

    receipt = export_recording(source, out)

    assert exported(out) == []
    assert receipt["messages"] == 0
    assert receipt["bounds"] is None


# Rejections and failures leave nothing that looks like a success. ----------


def rejected(tmp_path, source, error=ExportInputError, **options) -> str:
    out_dir = tmp_path / "out"
    out_dir.mkdir(exist_ok=True)
    before = sorted(out_dir.iterdir())
    with pytest.raises(error) as caught:
        export_recording(source, out_dir / "out.mcap", **options)
    assert sorted(out_dir.iterdir()) == before
    return str(caught.value)


def test_a_truncated_source_is_rejected(tmp_path):
    source = write_source(tmp_path / "run.mcap", TWO, {"a": "s.A"}, STORED[:1])
    source.write_bytes(source.read_bytes()[:-20])

    assert "truncated" in rejected(tmp_path, source)


def test_a_non_mcap_source_is_rejected(tmp_path):
    source = tmp_path / "run.mcap"
    source.write_bytes(b"not an mcap file at all")

    assert "not an MCAP file" in rejected(tmp_path, source)


def test_an_unsupported_codec_is_rejected(tmp_path):
    source = tmp_path / "run.mcap"
    with open(source, "wb") as stream:
        writer = Writer(stream)
        writer.start()
        schema = writer.register_schema("x", "ros1msg", b"int32 v")
        writer.register_channel("x", "ros1", schema)
        writer.finish()

    assert "unsupported message encoding 'ros1'" in rejected(tmp_path, source)


def test_an_unsupported_schema_type_is_rejected(tmp_path):
    schemas = {"h.Half": {"fields": [{"name": "v", "type": "f16"}]}}
    source = tmp_path / "run.mcap"
    with open(source, "wb") as stream:
        writer = Writer(stream)
        writer.start()
        schema = writer.register_schema("h.Half", "sil_pod",
                                        json.dumps(schemas["h.Half"]).encode())
        writer.register_channel("h", "sil_pod", schema)
        writer.finish()

    assert "unsupported type 'f16'" in rejected(tmp_path, source)


def test_a_malformed_payload_is_rejected_after_writing_started(tmp_path):
    source = tmp_path / "run.mcap"
    with open(source, "wb") as stream:
        writer = Writer(stream)
        writer.start()
        schema = writer.register_schema("s.A", "sil_pod",
                                        json.dumps(TWO["s.A"]).encode())
        channel = writer.register_channel("a", "sil_pod", schema)
        writer.add_message(channel, log_time=0, publish_time=0,
                           data=struct.pack("<i", 1))
        writer.add_message(channel, log_time=1, publish_time=1, data=b"\x01")
        writer.finish()

    assert "has 4 bytes, got 1" in rejected(tmp_path, source)


def test_an_unknown_or_repeated_channel_selection_is_rejected(tmp_path):
    source = write_source(tmp_path / "run.mcap", TWO, {"a": "s.A"}, [])

    assert "'z' is not in the source" in rejected(tmp_path, source,
                                                  channels=["z"])
    assert "selected twice" in rejected(tmp_path, source,
                                        channels=["a", "a"])


def test_an_unknown_profile_is_rejected(tmp_path):
    source = write_source(tmp_path / "run.mcap", TWO, {"a": "s.A"}, [])

    assert "unsupported profile" in rejected(tmp_path, source,
                                             profile="protobuf")


@pytest.mark.parametrize("existing", ["out.mcap", "out.mcap.receipt.json"])
def test_an_existing_output_is_not_overwritten(tmp_path, existing):
    source = write_source(tmp_path / "run.mcap", TWO, {"a": "s.A"}, [])
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / existing).write_text("keep")

    assert "already exists" in rejected(tmp_path, source)
    assert (tmp_path / "out" / existing).read_text() == "keep"


def test_the_source_cannot_be_the_output(tmp_path):
    source = write_source(tmp_path / "run.mcap", TWO, {"a": "s.A"}, [])
    before = sha256(source)

    with pytest.raises(ExportInputError, match="is the source"):
        export_recording(source, source)
    assert sha256(source) == before


def test_an_unwritable_output_directory_is_an_output_failure(tmp_path):
    source = write_source(tmp_path / "run.mcap", TWO, {"a": "s.A", "b": "s.B"},
                          STORED)
    (tmp_path / "out").mkdir()
    (tmp_path / "out").chmod(0o500)
    try:
        message = rejected(tmp_path, source, error=ExportOutputError)
    finally:
        (tmp_path / "out").chmod(0o700)
    assert "cannot write" in message


# The command ------------------------------------------------------------------


def test_the_command_exits_0_2_or_1(tmp_path, capsys):
    source = write_source(tmp_path / "run.mcap", TWO, {"a": "s.A", "b": "s.B"},
                          STORED)
    out = tmp_path / "run.visualization.mcap"
    command = ["recording", "export", str(source), "--profile",
               "visualization-json", "-o", str(out)]

    assert cli.main(command) == 0
    assert out.exists() and receipt_path(out).exists()
    assert cli.main(command) == 2
    assert "already exists" in capsys.readouterr().err
    missing_dir = tmp_path / "missing" / "out.mcap"
    assert cli.main([*command[:-1], str(missing_dir)]) == 1
    assert cli.main([*command, "--channel", "nope"]) == 2


def test_a_missing_source_is_rejected_but_an_unreadable_one_is_an_io_failure(
        tmp_path, capsys):
    source = write_source(tmp_path / "run.mcap", TWO, {"a": "s.A"}, STORED[:1])
    command = ["recording", "export", "--profile", "visualization-json", "-o",
               str(tmp_path / "out.mcap")]

    assert cli.main([*command, str(tmp_path / "missing.mcap")]) == 2
    source.chmod(0o000)
    try:
        assert cli.main([*command, str(source)]) == 1
    finally:
        source.chmod(0o600)
    assert "cannot read source" in capsys.readouterr().err
    assert not (tmp_path / "out.mcap").exists()


# A failed Run --------------------------------------------------------------------


def test_a_failed_runs_recording_is_exported_with_its_known_outcome(
        sil_run, tmp_path):
    import sys

    from conftest import ROOT
    from toys import add_producer, toy_manifest

    manifest = toy_manifest(duration_ns=100_000_000)
    manifest.add_channel("ticks", schema="toy.Counter")
    add_producer(manifest, channel="ticks", period_ns=10_000_000)
    # The controller exits during its Step at 30 ms.
    manifest.add_process("controller", command=[
        sys.executable, str(ROOT / "tests" / "participants" / "failure_modes.py"),
        "exit", "30000000"], step_period_ns=10_000_000)
    path = manifest.write(tmp_path / "manifest.json").path
    source = tmp_path / "failed.mcap"
    proc = subprocess.run([str(sil_run), str(path), "-o", str(source)],
                          capture_output=True, text=True, cwd=tmp_path)
    assert proc.returncode == 1, proc.stderr

    receipt = export_recording(source, tmp_path / "failed.visualization.mcap")

    assert receipt["completeness"]["run_outcome"] == "failed"
    assert receipt["completeness"]["run_exit_code"] == 1
    assert receipt["completeness"]["evidence"] == "failed.mcap.provenance.json"
    times = [t for _, t, _ in exported(tmp_path / "failed.visualization.mcap")]
    assert times and times == sorted(times) and max(times) < 100_000_000
    assert receipt["messages"] == len(times)


# The qualification fixture ----------------------------------------------------


def test_the_qualification_fixture_exports_to_an_indexed_seekable_file(tmp_path):
    source = write_qualification_source(tmp_path / "qualification.mcap")
    out = tmp_path / "qualification.visualization.mcap"

    receipt = export_recording(source, out)

    assert {c["topic"]: c["messages"] for c in receipt["channels"]} == {
        "vehicle/speed": 1000, "counter": 100, "sensor": 100, "burst": 30,
        "sparse": 1, "arrays": 10}
    assert receipt["bounds"] == {"start_ns": "0", "end_ns": "9990000000"}
    with open(out, "rb") as stream:
        reader = make_reader(stream)
        # An indexed time-range read, as a file player seeks.
        window = [(channel.topic, message.log_time, json.loads(message.data))
                  for _, channel, message in reader.iter_messages(
                      start_time=5_000_000_000, end_time=5_000_000_001)]
    assert [(topic, t) for topic, t, _ in window] == [
        ("vehicle/speed", 5_000_000_000), ("counter", 5_000_000_000),
        ("sensor", 5_000_000_000), ("burst", 5_000_000_000),
        ("burst", 5_000_000_000), ("burst", 5_000_000_000),
        ("arrays", 5_000_000_000), ("sparse", 5_000_000_000)]
    by_topic = {topic: payload for topic, _, payload in window}
    assert by_topic["counter"]["fields"] == {
        "count": "9007199254741492", "delta": "-4611686018427387404",
        "small": 500}
    assert by_topic["sensor"]["fields"] == {"reading": None}
    assert by_topic["sensor"]["sil"]["nonfinite"] == {
        "/fields/reading": "-Infinity"}
    bursts = [payload["fields"]["id"] for topic, _, payload in window
              if topic == "burst"]
    assert bursts == [500, 501, 502]
