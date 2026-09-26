"""Selecting a replay window from a Recording (issue #185).

A source Recording and a window document in; a rebased Recording and a
receipt out. The source is converted from CSV text with `sil-csv`, the
supported ingestion path, and every expected value here is stated by hand
from that text.
"""

import hashlib
import json
import subprocess
import sys

import pytest
from mcap.reader import make_reader

from sil.csv_recording import convert
from sil.replay_window import WindowError, main, prepare

MS = 1_000_000

# Channel `a` has a Message in every row, `b` in some. `stamp` carries the
# row's own source time in a u64 field and the value of `a`.
SOURCE_CSV = """t,a,b
0,1,
10,2,20
20,3,
20,4,
30,5,30
40,6,
50,7,50
"""

MAPPING = {
    "sil_csv_mapping": 1,
    "timestamp": {"column": "t", "unit": "ms"},
    "schemas": {
        "s.A": {"fields": [{"name": "v", "type": "f64"}]},
        "s.B": {"fields": [{"name": "v", "type": "f64"}]},
        "s.Stamp": {"fields": [{"name": "stamp_ns", "type": "u64"},
                               {"name": "v", "type": "f32"}]},
    },
    "channels": [
        {"channel": "a", "schema": "s.A", "fields": {"v": {"column": "a"}}},
        {"channel": "b", "schema": "s.B", "fields": {"v": {"column": "b"}}},
        {"channel": "stamp", "schema": "s.Stamp", "fields": {
            "stamp_ns": {"column": "t", "scale": MS},
            "v": {"column": "a"}}},
    ],
}


def window(**overrides) -> dict:
    doc = {
        "sil_replay_window": 1,
        "source_origin_ns": 10 * MS,
        "replay_start_ns": 10 * MS,
        "evaluation_start_ns": 20 * MS,
        "end_ns": 50 * MS,
        "channels": ["a", "b"],
    }
    doc.update(overrides)
    return {k: v for k, v in doc.items() if v is not None}


@pytest.fixture
def source(tmp_path):
    csv_path = tmp_path / "source.csv"
    csv_path.write_text(SOURCE_CSV)
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(json.dumps(MAPPING))
    out = tmp_path / "source.mcap"
    convert(mapping_path, csv_path, out)
    return out


def select(tmp_path, source, doc, name="window"):
    path = tmp_path / f"{name}.json"
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    out = tmp_path / f"{name}.mcap"
    return prepare(path, source, out), out


def rejected(tmp_path, source, doc) -> str:
    with pytest.raises(WindowError) as caught:
        select(tmp_path, source, doc)
    assert not (tmp_path / "window.mcap").exists()
    return str(caught.value)


def decoded(recording) -> list[tuple[str, int, dict]]:
    from sil.schema import MessageType

    out = []
    with open(recording, "rb") as f:
        for schema, channel, message in make_reader(f).iter_messages(
                log_time_order=False):
            assert message.log_time == message.publish_time
            codec = MessageType(schema.name, json.loads(schema.data))
            out.append((channel.topic, message.log_time,
                        codec.unpack(message.data)))
    return out


def payloads(recording) -> list[bytes]:
    with open(recording, "rb") as f:
        return [m.data for _, _, m in
                make_reader(f).iter_messages(log_time_order=False)]


class TestSelection:
    def test_the_window_is_half_open_and_rebased_to_the_origin(
        self, tmp_path, source
    ):
        _, out = select(tmp_path, source, window())
        # Source 10..40 ms become 0..30 ms. The Message at the exclusive end,
        # 50 ms, is not selected; the one at the replay start is.
        assert decoded(out) == [
            ("a", 0, {"v": 2.0}), ("b", 0, {"v": 20.0}),
            ("a", 10 * MS, {"v": 3.0}), ("a", 10 * MS, {"v": 4.0}),
            ("a", 20 * MS, {"v": 5.0}), ("b", 20 * MS, {"v": 30.0}),
            ("a", 30 * MS, {"v": 6.0}),
        ]

    def test_an_origin_before_the_replay_start_keeps_the_offset(
        self, tmp_path, source
    ):
        _, out = select(tmp_path, source, window(
            source_origin_ns=0, replay_start_ns=30 * MS,
            evaluation_start_ns=30 * MS))
        assert [(c, t) for c, t, _ in decoded(out)] == [
            ("a", 30 * MS), ("b", 30 * MS), ("a", 40 * MS)]

    def test_payload_bytes_are_copied_unchanged(self, tmp_path, source):
        _, out = select(tmp_path, source, window(channels=["stamp"]))
        with open(source, "rb") as f:
            expected = [m.data for _, c, m in make_reader(f).iter_messages(
                log_time_order=False)
                if c.topic == "stamp" and 10 * MS <= m.log_time < 50 * MS]
        assert payloads(out) == expected

    def test_only_the_declared_channels_are_carried_with_their_schemas(
        self, tmp_path, source
    ):
        _, out = select(tmp_path, source, window(channels=["b"]))
        with open(out, "rb") as f:
            summary = make_reader(f).get_summary()
        with open(source, "rb") as f:
            source_summary = make_reader(f).get_summary()
        (channel,) = summary.channels.values()
        schema = summary.schemas[channel.schema_id]
        (source_b,) = [c for c in source_summary.channels.values()
                       if c.topic == "b"]
        expected = source_summary.schemas[source_b.schema_id]
        assert channel.topic == "b"
        assert (schema.name, schema.encoding, schema.data) == (
            expected.name, expected.encoding, expected.data)


class TestSourceTimeFields:
    def test_a_source_time_field_is_unchanged_unless_declared(
        self, tmp_path, source
    ):
        _, out = select(tmp_path, source, window(channels=["stamp"]))
        assert [m["stamp_ns"] for _, _, m in decoded(out)] == [
            10 * MS, 20 * MS, 20 * MS, 30 * MS, 40 * MS]

    def test_a_declared_source_time_field_is_rebased_like_the_log_time(
        self, tmp_path, source
    ):
        _, out = select(tmp_path, source, window(
            channels=["stamp"], source_time_fields={"stamp": ["stamp_ns"]}))
        assert [(t, m) for _, t, m in decoded(out)] == [
            (0, {"stamp_ns": 0, "v": 2.0}),
            (10 * MS, {"stamp_ns": 10 * MS, "v": 3.0}),
            (10 * MS, {"stamp_ns": 10 * MS, "v": 4.0}),
            (20 * MS, {"stamp_ns": 20 * MS, "v": 5.0}),
            (30 * MS, {"stamp_ns": 30 * MS, "v": 6.0}),
        ]

    def test_a_source_time_before_the_origin_is_rejected(
        self, tmp_path, source
    ):
        # The held Message states 0 ms, before the 10 ms origin, and a u64
        # field cannot hold the negative rebased time.
        message = rejected(tmp_path, source, window(
            replay_start_ns=15 * MS, evaluation_start_ns=15 * MS,
            channels=["stamp"], hold_initial=["stamp"],
            source_origin_ns=15 * MS,
            source_time_fields={"stamp": ["stamp_ns"]}))
        assert "channel 'stamp' field 'stamp_ns'" in message
        assert "10000000 ns is before the source origin 15000000 ns" in message

    def test_a_schema_that_is_not_a_declaration_is_rejected(self, tmp_path):
        from mcap.writer import Writer

        src = tmp_path / "foreign.mcap"
        with open(src, "wb") as f:
            writer = Writer(f)
            writer.start()
            schema = writer.register_schema("s.X", "sil_pod", b"not json")
            channel = writer.register_channel("x", "sil_pod", schema)
            writer.add_message(channel, log_time=0, data=b"\0" * 8,
                               publish_time=0)
            writer.finish()
        message = rejected(tmp_path, src, window(
            source_origin_ns=0, replay_start_ns=0, evaluation_start_ns=0,
            end_ns=1, channels=["x"], source_time_fields={"x": ["t"]}))
        assert "schema 's.X' in the source is not a SiL schema declaration" \
            in message

    @pytest.mark.parametrize("fields, reason", [
        ({"stamp": ["v"]}, "must be a u64 or i64 field"),
        ({"stamp": ["nope"]}, "is not a field of schema 's.Stamp'"),
        ({"b": ["v"]}, "must be a u64 or i64 field"),
        ({"c": ["v"]}, "channel 'c' is not a selected channel"),
        ({"stamp": []}, "must be a non-empty array"),
        ({"stamp": ["stamp_ns", "stamp_ns"]}, "names field 'stamp_ns' twice"),
    ])
    def test_an_unusable_source_time_field_is_rejected(
        self, tmp_path, source, fields, reason
    ):
        assert reason in rejected(tmp_path, source, window(
            channels=["b", "stamp"], source_time_fields=fields))


class TestRanges:
    @pytest.mark.parametrize("overrides, reason", [
        ({"replay_start_ns": 5 * MS},
         "replay_start_ns 5000000 is before source_origin_ns 10000000"),
        ({"evaluation_start_ns": 5 * MS},
         "evaluation_start_ns 5000000 is before replay_start_ns 10000000"),
        ({"end_ns": 20 * MS},
         "end_ns 20000000 must be after evaluation_start_ns 20000000"),
        ({"end_ns": -1}, "'end_ns' must be an integer from 0 to 2^64 - 1"),
        ({"end_ns": 2**64}, "'end_ns' must be an integer from 0 to 2^64 - 1"),
        ({"end_ns": 5e7}, "'end_ns' must be an integer from 0 to 2^64 - 1"),
        ({"end_ns": True}, "'end_ns' must be an integer from 0 to 2^64 - 1"),
        ({"end_ns": None}, "missing key(s) 'end_ns'"),
        ({"extra": 1}, "unknown key(s) 'extra'"),
        ({"sil_replay_window": 2}, "'sil_replay_window' must be 1"),
        ({"channels": []}, "'channels' must be a non-empty array"),
        ({"channels": ["a", "a"]}, "channel 'a' is selected twice"),
        ({"channels": ["zzz"]}, "channel 'zzz' is not in the source"),
        ({"hold_initial": ["stamp"]},
         "hold_initial channel 'stamp' is not a selected channel"),
        ({"max_gap_ns": 0}, "'max_gap_ns' must be an integer from 1"),
        ({"max_gap_ns": -5}, "'max_gap_ns' must be an integer from 1"),
    ])
    def test_an_inconsistent_window_is_rejected(
        self, tmp_path, source, overrides, reason
    ):
        assert reason in rejected(tmp_path, source, window(**overrides))

    def test_a_duplicate_key_is_rejected(self, tmp_path, source):
        text = json.dumps(window())[:-1] + ', "end_ns": 40000000}'
        assert "duplicate key 'end_ns'" in rejected(tmp_path, source, text)


class TestCoverage:
    def test_a_replay_start_before_the_source_is_missing_history(
        self, tmp_path
    ):
        late = tmp_path / "late.csv"
        late.write_text("t,a,b\n20,1,\n30,2,2\n")
        mapping_path = tmp_path / "mapping.json"
        mapping_path.write_text(json.dumps(MAPPING))
        src = tmp_path / "late.mcap"
        convert(mapping_path, late, src)
        message = rejected(tmp_path, src, window(end_ns=31 * MS))
        assert ("missing history: channel 'a' starts at 20000000 ns, after "
                "replay_start_ns 10000000") in message

    def test_each_channel_must_cover_the_window_on_its_own(self, tmp_path):
        # `a` covers the window from 0 ms; `b` starts only at 20 ms.
        late = tmp_path / "late.csv"
        late.write_text("t,a,b\n0,1,\n10,2,\n20,3,3\n30,4,4\n")
        mapping_path = tmp_path / "mapping.json"
        mapping_path.write_text(json.dumps(MAPPING))
        src = tmp_path / "late.mcap"
        convert(mapping_path, late, src)
        message = rejected(tmp_path, src, window(end_ns=30 * MS))
        assert ("missing history: channel 'b' starts at 20000000 ns, after "
                "replay_start_ns 10000000") in message

    def test_an_end_past_the_source_is_insufficient_coverage(
        self, tmp_path, source
    ):
        message = rejected(tmp_path, source, window(end_ns=52 * MS))
        assert ("insufficient coverage: channel 'a' ends at 50000000 ns, "
                "before the window's last instant 51999999 ns") in message

    def test_a_declared_gap_limit_also_bounds_the_end(self, tmp_path, source):
        # Both Channels end at 50 ms: 5 ms before 55 ms is within 20 ms.
        receipt, _ = select(tmp_path, source,
                            window(end_ns=55 * MS, max_gap_ns=20 * MS), "near")
        assert receipt["duration_ns"] == 45 * MS
        message = rejected(tmp_path, source,
                           window(end_ns=71 * MS, max_gap_ns=20 * MS))
        assert ("insufficient coverage: channel 'a' ends at 50000000 ns, more "
                "than max_gap_ns 20000000 before end_ns 71000000") in message

    def test_the_end_may_be_one_past_the_last_message(self, tmp_path, source):
        _, out = select(tmp_path, source, window(end_ns=50 * MS + 1))
        assert decoded(out)[-2:] == [("a", 40 * MS, {"v": 7.0}),
                                     ("b", 40 * MS, {"v": 50.0})]

    def test_a_channel_without_a_message_in_the_window_is_rejected(
        self, tmp_path, source
    ):
        message = rejected(tmp_path, source, window(
            replay_start_ns=40 * MS, evaluation_start_ns=40 * MS))
        assert "channel 'b' has no Message in the window" in message

    def test_a_gap_longer_than_declared_is_rejected(self, tmp_path, source):
        message = rejected(tmp_path, source, window(max_gap_ns=15 * MS))
        assert "channel 'b' has no Message from 10000000 ns to 30000000 ns" \
            in message
        assert "longer than max_gap_ns 15000000" in message

    def test_the_trailing_interval_counts_as_a_gap(self, tmp_path, source):
        # `b` has its last selected Message at 30 ms; the window ends at 50.
        message = rejected(tmp_path, source, window(
            max_gap_ns=19 * MS, channels=["b"],
            replay_start_ns=30 * MS, evaluation_start_ns=30 * MS,
            source_origin_ns=30 * MS))
        assert "from 30000000 ns to 50000000 ns" in message


class TestHeldInitialValue:
    def test_a_held_value_is_published_at_the_replay_start(
        self, tmp_path, source
    ):
        receipt, out = select(tmp_path, source, window(
            source_origin_ns=20 * MS, replay_start_ns=20 * MS,
            evaluation_start_ns=30 * MS, hold_initial=["b"]))
        # `b`'s latest Message before 20 ms is 20.0 at 10 ms. It is published
        # first, at the replay start, ahead of the window's own Messages.
        assert decoded(out)[:3] == [
            ("b", 0, {"v": 20.0}), ("a", 0, {"v": 3.0}), ("a", 0, {"v": 4.0})]
        assert receipt["channels"]["b"]["held"] == {"source_ns": 10 * MS}
        assert receipt["channels"]["a"]["held"] is None

    def test_nothing_is_held_when_a_message_is_at_the_replay_start(
        self, tmp_path, source
    ):
        receipt, out = select(tmp_path, source, window(hold_initial=["b"]))
        assert [c for c, t, _ in decoded(out) if t == 0] == ["a", "b"]
        assert receipt["channels"]["b"]["held"] is None

    def test_a_held_value_without_earlier_history_is_rejected(
        self, tmp_path, source
    ):
        message = rejected(tmp_path, source, window(
            source_origin_ns=5 * MS, replay_start_ns=5 * MS,
            evaluation_start_ns=5 * MS, hold_initial=["b"]))
        assert ("missing history: channel 'b' starts at 10000000 ns, after "
                "replay_start_ns 5000000") in message

    def test_nothing_is_held_unless_declared(self, tmp_path, source):
        _, out = select(tmp_path, source, window(
            source_origin_ns=20 * MS, replay_start_ns=20 * MS,
            evaluation_start_ns=30 * MS))
        assert [(c, t) for c, t, _ in decoded(out) if c == "b"] == [
            ("b", 10 * MS)]


class TestReceipt:
    def test_the_receipt_identifies_both_intervals_and_their_coverage(
        self, tmp_path, source
    ):
        receipt, out = select(tmp_path, source, window())
        window_bytes = (tmp_path / "window.json").read_bytes()
        assert receipt["sil_window_receipt"] == 1
        assert receipt["preparer"]["name"] == "sil-window"
        assert receipt["source"] == {
            "file": "source.mcap",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
        assert receipt["window"] == {
            "file": "window.json",
            "sha256": hashlib.sha256(window_bytes).hexdigest()}
        assert receipt["recording"] == {
            "file": "window.mcap",
            "sha256": hashlib.sha256(out.read_bytes()).hexdigest()}
        assert receipt["source_origin_ns"] == 10 * MS
        assert receipt["intervals"] == {
            "warm_up": {"source": {"start_ns": 10 * MS, "end_ns": 20 * MS},
                        "virtual": {"start_ns": 0, "end_ns": 10 * MS}},
            "evaluation": {"source": {"start_ns": 20 * MS, "end_ns": 50 * MS},
                           "virtual": {"start_ns": 10 * MS,
                                       "end_ns": 40 * MS}},
        }
        assert receipt["duration_ns"] == 40 * MS
        assert receipt["evaluation_window"] == {"from_ns": 10 * MS,
                                                "to_ns": 40 * MS - 1}
        assert receipt["channels"] == {
            "a": {"schema": "s.A", "held": None, "largest_gap_ns": 10 * MS,
                  "source_span": {"first_ns": 0, "last_ns": 50 * MS},
                  "warm_up": {"messages": 1, "first_ns": 0, "last_ns": 0},
                  "evaluation": {"messages": 4, "first_ns": 10 * MS,
                                 "last_ns": 30 * MS}},
            "b": {"schema": "s.B", "held": None, "largest_gap_ns": 20 * MS,
                  "source_span": {"first_ns": 10 * MS, "last_ns": 50 * MS},
                  "warm_up": {"messages": 1, "first_ns": 0, "last_ns": 0},
                  "evaluation": {"messages": 1, "first_ns": 20 * MS,
                                 "last_ns": 20 * MS}},
        }

    def test_an_interval_without_messages_reports_none(
        self, tmp_path, source
    ):
        receipt, _ = select(tmp_path, source, window(evaluation_start_ns=10 * MS))
        assert receipt["channels"]["a"]["warm_up"] == {
            "messages": 0, "first_ns": None, "last_ns": None}
        assert receipt["intervals"]["warm_up"]["virtual"] == {
            "start_ns": 0, "end_ns": 0}


class TestIdentity:
    def test_preparation_repeats_byte_identically(self, tmp_path, source):
        first, out = select(tmp_path, source, window(), "first")
        second, again = select(tmp_path, source, window(), "second")
        assert out.read_bytes() == again.read_bytes()
        assert first["recording"]["sha256"] == second["recording"]["sha256"]

    def test_a_changed_window_is_a_different_recording(self, tmp_path, source):
        # The selected Messages are the same; only the declared warm-up moves.
        first, _ = select(tmp_path, source, window(), "first")
        second, _ = select(tmp_path, source,
                           window(evaluation_start_ns=30 * MS), "second")
        assert first["recording"]["sha256"] != second["recording"]["sha256"]

    def test_the_recording_names_its_source_and_window(self, tmp_path, source):
        receipt, out = select(tmp_path, source, window())
        with open(out, "rb") as f:
            (metadata,) = list(make_reader(f).iter_metadata())
        assert metadata.name == "sil_window"
        assert metadata.metadata == {
            "source_sha256": receipt["source"]["sha256"],
            "window_sha256": receipt["window"]["sha256"],
        }


class TestCommandLine:
    def test_the_receipt_is_written_where_asked(self, tmp_path, source):
        doc = tmp_path / "w.json"
        doc.write_text(json.dumps(window()))
        receipt_path = tmp_path / "receipt.json"
        code = main([str(doc), str(source), "-o", str(tmp_path / "w.mcap"),
                     "--receipt", str(receipt_path)])
        assert code == 0
        assert json.loads(receipt_path.read_text())["duration_ns"] == 40 * MS

    def test_a_rejected_window_exits_2_with_a_diagnostic(
        self, tmp_path, source
    ):
        doc = tmp_path / "w.json"
        doc.write_text(json.dumps(window(end_ns=5 * MS)))
        proc = subprocess.run(
            [sys.executable, "-m", "sil.replay_window", str(doc), str(source),
             "-o", str(tmp_path / "w.mcap")],
            capture_output=True, text=True)
        assert proc.returncode == 2
        assert proc.stderr.startswith("sil-window: error: ")
        assert "end_ns 5000000" in proc.stderr
        assert not (tmp_path / "w.mcap").exists()
