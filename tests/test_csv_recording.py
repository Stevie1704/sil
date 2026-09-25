"""CSV ingestion at the converter boundary: a CSV file and a mapping document
in, a Recording and a conversion receipt out (issue #179).

The expected values here are stated by hand from the fixture text, not
computed by the converter's own arithmetic.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from mcap.reader import make_reader

from conftest import ROOT
from sil.csv_recording import ConversionError, convert, main

EXAMPLE = ROOT / "examples" / "csv"
U64_MAX = 2**64 - 1


def decoded(recording: Path) -> list[tuple[str, int, dict]]:
    """(channel, log time, fields) in stored order, decoded with the embedded
    schema rather than the mapping, so the recording stands on its own."""
    from sil.schema import MessageType

    out = []
    with open(recording, "rb") as f:
        for schema, channel, message in make_reader(f).iter_messages():
            assert message.log_time == message.publish_time
            codec = MessageType(schema.name, json.loads(schema.data))
            out.append((channel.topic, message.log_time, codec.unpack(message.data)))
    return out


def schema_records(recording: Path) -> dict[str, tuple[str, str, bytes]]:
    with open(recording, "rb") as f:
        summary = make_reader(f).get_summary()
    return {
        channel.topic: (
            summary.schemas[channel.schema_id].name,
            summary.schemas[channel.schema_id].encoding,
            summary.schemas[channel.schema_id].data,
        )
        for channel in summary.channels.values()
    }


def mapping(*, timestamp=None, schemas=None, channels=None) -> dict:
    """A one-channel mapping over columns `t` (ns) and `v` (f64)."""
    return {
        "sil_csv_mapping": 1,
        "timestamp": timestamp or {"column": "t", "unit": "ns"},
        "schemas": schemas or {"s.V": {"fields": [{"name": "v", "type": "f64"}]}},
        "channels": channels or [
            {"channel": "v", "schema": "s.V", "fields": {"v": {"column": "v"}}}
        ],
    }


def convert_text(tmp_path, csv_text: str, doc: dict | str | None = None):
    mapping_path = tmp_path / "mapping.json"
    text = doc if isinstance(doc, str) else json.dumps(doc or mapping())
    mapping_path.write_text(text)
    source = tmp_path / "source.csv"
    source.write_text(csv_text)
    out = tmp_path / "out.mcap"
    return convert(mapping_path, source, out), out


def rejected(tmp_path, csv_text: str, doc=None) -> str:
    with pytest.raises(ConversionError) as caught:
        convert_text(tmp_path, csv_text, doc)
    assert not (tmp_path / "out.mcap").exists()
    return str(caught.value)


class TestExampleFixture:
    """The fixture in examples/csv, decoded value by value."""

    def test_values_timestamps_order_and_counts(self, tmp_path):
        receipt = convert(EXAMPLE / "mapping.json", EXAMPLE / "signals.csv",
                          tmp_path / "signals.mcap")

        assert decoded(tmp_path / "signals.mcap") == [
            ("ego.speed", 0, {"speed_mps": 10.0}),
            ("target.range", 0, {"range_m": 90.0}),
            ("ego.gear", 0, {"gear": 3}),
            ("ego.speed", 20_000_000, {"speed_mps": 10.050000190734863}),
            ("ego.gear", 20_000_000, {"gear": 3}),
            # Two rows share 40 ms: row order first, then mapping order.
            ("ego.speed", 40_000_000, {"speed_mps": 10.100000381469727}),
            ("target.range", 40_000_000, {"range_m": 89.5}),
            ("ego.speed", 40_000_000, {"speed_mps": 10.149999618530273}),
            ("ego.speed", 60_000_000, {"speed_mps": 10.199999809265137}),
            ("target.range", 60_000_000, {"range_m": 89.0}),
            ("ego.gear", 60_000_000, {"gear": 4}),
        ]
        assert receipt["channels"] == {
            "ego.speed": {"schema": "csv.Speed", "messages": 5,
                          "first_ns": 0, "last_ns": 60_000_000},
            "target.range": {"schema": "csv.Range", "messages": 3,
                             "first_ns": 0, "last_ns": 60_000_000},
            "ego.gear": {"schema": "csv.Gear", "messages": 3,
                         "first_ns": 0, "last_ns": 60_000_000},
        }
        assert receipt["time_bounds"] == {"first_ns": 0, "last_ns": 60_000_000}

    def test_schema_records_are_the_kernels_canonical_json(self, tmp_path):
        convert(EXAMPLE / "mapping.json", EXAMPLE / "signals.csv",
                tmp_path / "signals.mcap")

        assert schema_records(tmp_path / "signals.mcap") == {
            "ego.speed": ("csv.Speed", "sil_pod",
                          b'{"fields":[{"name":"speed_mps","type":"f32"}]}'),
            "target.range": ("csv.Range", "sil_pod",
                             b'{"fields":[{"name":"range_m","type":"f64"}]}'),
            "ego.gear": ("csv.Gear", "sil_pod",
                         b'{"fields":[{"name":"gear","type":"i8"}]}'),
        }

    def test_receipt_names_inputs_output_and_converter(self, tmp_path):
        out = tmp_path / "signals.mcap"
        receipt = convert(EXAMPLE / "mapping.json", EXAMPLE / "signals.csv", out)

        def digest(path):
            return hashlib.sha256(path.read_bytes()).hexdigest()

        assert receipt["sil_csv_receipt"] == 1
        assert receipt["source"] == {
            "file": "signals.csv", "sha256": digest(EXAMPLE / "signals.csv"),
            "rows": 5,
        }
        assert receipt["mapping"] == {
            "file": "mapping.json", "sha256": digest(EXAMPLE / "mapping.json"),
        }
        assert receipt["recording"] == {"file": "signals.mcap",
                                        "sha256": digest(out)}
        assert receipt["converter"]["name"] == "sil-csv"
        assert receipt["converter"]["version"] == "0.1.0"
        assert receipt["converter"]["mcap"].startswith("mcap-python/")

    def test_repeat_conversion_is_byte_identical(self, tmp_path):
        first = convert(EXAMPLE / "mapping.json", EXAMPLE / "signals.csv",
                        tmp_path / "a" / "signals.mcap")
        second = convert(EXAMPLE / "mapping.json", EXAMPLE / "signals.csv",
                         tmp_path / "b" / "signals.mcap")

        assert (tmp_path / "a" / "signals.mcap").read_bytes() == (
            tmp_path / "b" / "signals.mcap").read_bytes()
        assert first == second


class TestTimestamps:
    @pytest.mark.parametrize(("unit", "cell", "expected"), [
        ("s", "1.000000001", 1_000_000_001),
        ("ms", "2.5", 2_500_000),
        ("us", "7", 7_000),
        ("ns", "18446744073709551615", U64_MAX),
        ("ns", "+5", 5),
    ])
    def test_exact_integer_conversion(self, tmp_path, unit, cell, expected):
        doc = mapping(timestamp={"column": "t", "unit": unit})
        convert_text(tmp_path, f"t,v\n{cell},1\n", doc)
        assert [t for _, t, _ in decoded(tmp_path / "out.mcap")] == [expected]

    def test_origin_is_subtracted_exactly(self, tmp_path):
        doc = mapping(timestamp={"column": "t", "unit": "s",
                                 "origin": 1695640000.5})
        convert_text(tmp_path, "t,v\n1695640000.5,1\n1695640001.000000007,2\n", doc)
        assert [t for _, t, _ in decoded(tmp_path / "out.mcap")] == [
            0, 500_000_007]

    def test_unrepresentable_precision_is_rejected(self, tmp_path):
        doc = mapping(timestamp={"column": "t", "unit": "s"})
        message = rejected(tmp_path, "t,v\n0,1\n0.0000000005,2\n", doc)
        assert "row 2" in message and "'t'" in message
        assert "whole number of nanoseconds" in message

    def test_negative_normalized_time_is_rejected(self, tmp_path):
        doc = mapping(timestamp={"column": "t", "unit": "ms", "origin": 10})
        message = rejected(tmp_path, "t,v\n9,1\n", doc)
        assert "row 1" in message and "negative" in message

    def test_overflow_is_rejected(self, tmp_path):
        message = rejected(tmp_path, "t,v\n18446744073709551616,1\n")
        assert "row 1" in message and "overflows" in message

    def test_descending_timestamps_are_rejected(self, tmp_path):
        message = rejected(tmp_path, "t,v\n5,1\n6,2\n4,3\n")
        assert "row 3" in message and "descend" in message

    @pytest.mark.parametrize("cell", ["", "1e3", "0x10", "nan", " 1", "1.", ".5"])
    def test_malformed_timestamp_is_rejected(self, tmp_path, cell):
        message = rejected(tmp_path, f"t,v\n{cell},1\n")
        assert "row 1" in message and "'t'" in message

    @pytest.mark.parametrize("unit", ["min", "S", "", None, ["s"], {"s": 1}])
    def test_unknown_unit_is_rejected(self, tmp_path, unit):
        doc = mapping(timestamp={"column": "t", "unit": unit})
        assert "unit" in rejected(tmp_path, "t,v\n0,1\n", doc)


class TestValues:
    @pytest.mark.parametrize(("field_type", "scale", "offset", "cell",
                              "expected"), [
        ("f64", None, None, "-0", -0.0),
        ("f64", 0.5, None, "3", 1.5),
        ("f64", 2, -1, "1.25e1", 24.0),
        ("f32", None, None, "0.1", 0.10000000149011612),
        ("i16", 3, -7, "-100", -307),
        ("u8", None, None, "255", 255),
        ("u64", None, None, "18446744073709551615", U64_MAX),
        ("i64", None, None, "-9223372036854775808", -(2**63)),
    ])
    def test_supported_conversions(self, tmp_path, field_type, scale,
                                   offset, cell, expected):
        field = {"column": "v"}
        if scale is not None:
            field["scale"] = scale
        if offset is not None:
            field["offset"] = offset
        doc = mapping(
            schemas={"s.V": {"fields": [{"name": "v", "type": field_type}]}},
            channels=[{"channel": "v", "schema": "s.V", "fields": {"v": field}}],
        )
        convert_text(tmp_path, f"t,v\n0,{cell}\n", doc)
        [(_, _, fields)] = decoded(tmp_path / "out.mcap")
        assert repr(fields["v"]) == repr(expected)

    @pytest.mark.parametrize("cell", ["nan", "inf", "-Infinity", "NaN"])
    def test_non_finite_value_is_rejected(self, tmp_path, cell):
        message = rejected(tmp_path, f"t,v\n0,{cell}\n")
        assert "row 1" in message and "'v'" in message and "finite" in message

    def test_float_overflow_after_scaling_is_rejected(self, tmp_path):
        doc = mapping(channels=[{"channel": "v", "schema": "s.V",
                                 "fields": {"v": {"column": "v", "scale": 1e300}}}])
        message = rejected(tmp_path, "t,v\n0,1e10\n", doc)
        assert "row 1" in message and "finite" in message

    def test_f32_out_of_range_is_rejected(self, tmp_path):
        doc = mapping(schemas={"s.V": {"fields": [{"name": "v", "type": "f32"}]}})
        message = rejected(tmp_path, "t,v\n0,1e39\n", doc)
        assert "row 1" in message and "f32" in message

    @pytest.mark.parametrize(("field_type", "cell", "scale"), [
        ("f64", "1e-400", None),
        ("f64", "1e-200", 1e-200),
        ("f32", "1e-50", None),
    ])
    def test_underflow_to_zero_is_rejected(self, tmp_path, field_type, cell, scale):
        field = {"column": "v"} if scale is None else {"column": "v", "scale": scale}
        doc = mapping(
            schemas={"s.V": {"fields": [{"name": "v", "type": field_type}]}},
            channels=[{"channel": "v", "schema": "s.V", "fields": {"v": field}}],
        )
        message = rejected(tmp_path, f"t,v\n0,{cell}\n", doc)
        assert "row 1" in message and "underflows" in message

    def test_zero_stays_zero(self, tmp_path):
        doc = mapping(channels=[{"channel": "v", "schema": "s.V",
                                 "fields": {"v": {"column": "v", "scale": 1e-200}}}])
        _, out = convert_text(tmp_path, "t,v\n0,0e5\n", doc)
        assert decoded(out) == [("v", 0, {"v": 0.0})]

    def test_integer_scale_beyond_binary64_is_rejected(self, tmp_path):
        text = json.dumps(mapping(channels=[{
            "channel": "v", "schema": "s.V",
            "fields": {"v": {"column": "v", "scale": 1}}}]))
        text = text.replace('"scale": 1', '"scale": 1' + "0" * 400)
        assert "finite" in rejected(tmp_path, "t,v\n0,1\n", text)

    @pytest.mark.parametrize("column", ["t", "v"])
    def test_very_long_digit_strings_are_rejected(self, tmp_path, column):
        doc = mapping(schemas={"s.V": {"fields": [{"name": "v", "type": "i64"}]}})
        long = "1" * 5000
        cells = {"t": "0", "v": "1", column: long}
        message = rejected(tmp_path, f"t,v\n{cells['t']},{cells['v']}\n", doc)
        assert "row 1" in message and repr(column) in message

    def test_very_long_mapping_integer_is_rejected(self, tmp_path):
        text = json.dumps(mapping(timestamp={"column": "t", "unit": "ns",
                                             "origin": 1}))
        text = text.replace('"origin": 1', '"origin": 1' + "0" * 5000)
        assert "mapping" in rejected(tmp_path, "t,v\n0,1\n", text)

    @pytest.mark.parametrize(("field_type", "cell"), [
        ("u8", "256"), ("u8", "-1"), ("i8", "-129"), ("u64", "18446744073709551616"),
    ])
    def test_integer_out_of_range_is_rejected(self, tmp_path, field_type, cell):
        doc = mapping(schemas={"s.V": {"fields": [{"name": "v", "type": field_type}]}})
        message = rejected(tmp_path, f"t,v\n0,{cell}\n", doc)
        assert "row 1" in message and field_type in message

    @pytest.mark.parametrize("cell", ["1.0", "1e2", "abc", "0x1"])
    def test_integer_field_needs_an_integer_cell(self, tmp_path, cell):
        doc = mapping(schemas={"s.V": {"fields": [{"name": "v", "type": "i32"}]}})
        message = rejected(tmp_path, f"t,v\n0,{cell}\n", doc)
        assert "row 1" in message and "'v'" in message

    def test_integer_field_rejects_a_fractional_scale(self, tmp_path):
        doc = mapping(
            schemas={"s.V": {"fields": [{"name": "v", "type": "i32"}]}},
            channels=[{"channel": "v", "schema": "s.V",
                       "fields": {"v": {"column": "v", "scale": 0.5}}}],
        )
        assert "scale" in rejected(tmp_path, "t,v\n0,2\n", doc)


class TestRows:
    def test_a_channel_with_all_cells_empty_has_no_message_in_that_row(self, tmp_path):
        doc = mapping(
            schemas={"s.P": {"fields": [{"name": "x", "type": "f64"},
                                        {"name": "y", "type": "f64"}]}},
            channels=[{"channel": "p", "schema": "s.P",
                       "fields": {"x": {"column": "x"}, "y": {"column": "y"}}}],
        )
        receipt, out = convert_text(tmp_path, "t,x,y\n0,1,2\n1,,\n2,3,4\n", doc)
        assert decoded(out) == [("p", 0, {"x": 1.0, "y": 2.0}),
                                ("p", 2, {"x": 3.0, "y": 4.0})]
        assert receipt["source"]["rows"] == 3
        assert receipt["channels"]["p"]["messages"] == 2

    def test_a_partial_message_is_rejected(self, tmp_path):
        doc = mapping(
            schemas={"s.P": {"fields": [{"name": "x", "type": "f64"},
                                        {"name": "y", "type": "f64"}]}},
            channels=[{"channel": "p", "schema": "s.P",
                       "fields": {"x": {"column": "x"}, "y": {"column": "y"}}}],
        )
        message = rejected(tmp_path, "t,x,y\n0,1,\n", doc)
        assert "row 1" in message and "'y'" in message and "empty" in message

    def test_wrong_cell_count_is_rejected(self, tmp_path):
        message = rejected(tmp_path, "t,v\n0,1\n1,2,3\n")
        assert "row 2" in message and "line 3" in message

    @pytest.mark.parametrize(("text", "column"), [
        ('t,v\n0,1\n1,"2\n', "'v'"),        # unterminated quoted cell
        ('t,v\n0,1\n"1"x,2\n', "'t'"),       # text after a closing quote
    ])
    def test_malformed_quoting_names_row_and_column(self, tmp_path, text, column):
        message = rejected(tmp_path, text)
        assert "row 2 (line 3)" in message and f"column {column}" in message

    def test_blank_line_is_rejected(self, tmp_path):
        assert "row 2" in rejected(tmp_path, "t,v\n0,1\n\n2,3\n")

    def test_missing_column_is_rejected(self, tmp_path):
        message = rejected(tmp_path, "t,w\n0,1\n")
        assert "missing" in message and "'v'" in message

    def test_duplicate_header_is_rejected(self, tmp_path):
        message = rejected(tmp_path, "t,v,v\n0,1,2\n")
        assert "'v'" in message and "more than once" in message

    def test_header_only_is_rejected(self, tmp_path):
        assert "no Messages" in rejected(tmp_path, "t,v\n")

    def test_empty_file_is_rejected(self, tmp_path):
        assert "header" in rejected(tmp_path, "")

    def test_non_utf8_source_is_rejected(self, tmp_path):
        source = tmp_path / "source.csv"
        source.write_bytes(b"t,v\n0,\xff\n")
        (tmp_path / "mapping.json").write_text(json.dumps(mapping()))
        with pytest.raises(ConversionError, match="UTF-8"):
            convert(tmp_path / "mapping.json", source, tmp_path / "out.mcap")


class TestMapping:
    def test_duplicate_key_is_rejected(self, tmp_path):
        text = json.dumps(mapping()).replace(
            '"sil_csv_mapping": 1', '"sil_csv_mapping": 1, "sil_csv_mapping": 1')
        assert "duplicate" in rejected(tmp_path, "t,v\n0,1\n", text)

    def test_duplicate_channel_is_rejected(self, tmp_path):
        entry = {"channel": "v", "schema": "s.V", "fields": {"v": {"column": "v"}}}
        doc = mapping(channels=[entry, entry])
        assert "'v'" in rejected(tmp_path, "t,v\n0,1\n", doc)

    def test_unmapped_schema_field_is_rejected(self, tmp_path):
        doc = mapping(schemas={"s.V": {"fields": [{"name": "v", "type": "f64"},
                                                  {"name": "w", "type": "f64"}]}})
        message = rejected(tmp_path, "t,v\n0,1\n", doc)
        assert "'w'" in message and "not mapped" in message

    def test_field_outside_the_schema_is_rejected(self, tmp_path):
        doc = mapping(channels=[{"channel": "v", "schema": "s.V", "fields": {
            "v": {"column": "v"}, "extra": {"column": "v"}}}])
        assert "'extra'" in rejected(tmp_path, "t,v\n0,1\n", doc)

    def test_undeclared_schema_is_rejected(self, tmp_path):
        doc = mapping(channels=[{"channel": "v", "schema": "s.Missing",
                                 "fields": {"v": {"column": "v"}}}])
        assert "'s.Missing'" in rejected(tmp_path, "t,v\n0,1\n", doc)

    def test_array_field_is_rejected(self, tmp_path):
        doc = mapping(schemas={"s.V": {"fields": [
            {"name": "v", "type": "f64", "count": 2}]}})
        assert "scalar" in rejected(tmp_path, "t,v\n0,1\n", doc)

    def test_unknown_field_type_is_rejected(self, tmp_path):
        doc = mapping(schemas={"s.V": {"fields": [{"name": "v", "type": "f16"}]}})
        assert "f16" in rejected(tmp_path, "t,v\n0,1\n", doc)

    @pytest.mark.parametrize("key", ["interpolate", "default"])
    def test_unknown_field_key_is_rejected(self, tmp_path, key):
        doc = mapping(channels=[{"channel": "v", "schema": "s.V",
                                 "fields": {"v": {"column": "v", key: 0}}}])
        assert repr(key) in rejected(tmp_path, "t,v\n0,1\n", doc)

    @pytest.mark.parametrize("literal", ["NaN", "Infinity"])
    def test_non_finite_scale_is_rejected(self, tmp_path, literal):
        text = json.dumps(mapping(channels=[{
            "channel": "v", "schema": "s.V",
            "fields": {"v": {"column": "v", "scale": 1}}}]))
        text = text.replace('"scale": 1', f'"scale": {literal}')
        assert literal in rejected(tmp_path, "t,v\n0,1\n", text)

    def test_unsupported_version_is_rejected(self, tmp_path):
        doc = mapping()
        doc["sil_csv_mapping"] = 2
        assert "sil_csv_mapping" in rejected(tmp_path, "t,v\n0,1\n", doc)

    def test_malformed_json_is_rejected(self, tmp_path):
        assert "JSON" in rejected(tmp_path, "t,v\n0,1\n", "{")


class TestCommandLine:
    def test_writes_recording_and_receipt(self, tmp_path):
        out = tmp_path / "signals.mcap"
        receipt = tmp_path / "signals.receipt.json"
        code = main([str(EXAMPLE / "mapping.json"), str(EXAMPLE / "signals.csv"),
                     "-o", str(out), "--receipt", str(receipt)])
        assert code == 0
        document = json.loads(receipt.read_text())
        assert document["recording"]["sha256"] == hashlib.sha256(
            out.read_bytes()).hexdigest()

    def test_prints_the_receipt_without_a_receipt_path(self, tmp_path, capsys):
        out = tmp_path / "signals.mcap"
        assert main([str(EXAMPLE / "mapping.json"), str(EXAMPLE / "signals.csv"),
                     "-o", str(out)]) == 0
        assert json.loads(capsys.readouterr().out)["recording"]["file"] == (
            "signals.mcap")

    def test_rejection_exits_2_with_a_diagnostic(self, tmp_path):
        source = tmp_path / "bad.csv"
        source.write_text("time_s,speed_cmps,range_raw,gear\n0,nan,1,1\n")
        proc = subprocess.run(
            [sys.executable, "-m", "sil.csv_recording",
             str(EXAMPLE / "mapping.json"), str(source),
             "-o", str(tmp_path / "bad.mcap")],
            capture_output=True, text=True,
        )
        assert proc.returncode == 2
        assert proc.stderr.startswith("sil-csv: error: ")
        assert "row 1" in proc.stderr
        assert not (tmp_path / "bad.mcap").exists()

    def test_output_must_be_mcap(self, tmp_path):
        with pytest.raises(ConversionError, match="mcap"):
            convert(EXAMPLE / "mapping.json", EXAMPLE / "signals.csv",
                    tmp_path / "signals.csv.out")
