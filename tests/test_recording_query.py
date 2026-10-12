"""Bounded investigation through the CLI and Python Recording read seam."""

import hashlib
import json
import struct

import pytest

from mcap.writer import CompressionType, Writer

from sil import cli
from sil.recording_query import Predicate, QueryError, RecordingQuery, render


def recording(path, messages, spec=None, *, chunk_size=1024):
    spec = spec or {"fields": [
        {"name": "value", "type": "f64"},
        {"name": "samples", "type": "i16", "count": 3},
        {"name": "frame", "type": "u8", "count": 4},
    ]}
    with path.open("wb") as stream:
        writer = Writer(stream, compression=CompressionType.NONE,
                        chunk_size=chunk_size)
        writer.start(profile="sil")
        schema = writer.register_schema("Signal", "sil_pod",
                                        json.dumps(spec).encode())
        channel = writer.register_channel("signal", "sil_pod", schema)
        for t, data in messages:
            writer.add_message(channel, log_time=t, publish_time=t, data=data)
        writer.finish()
    return path


def payload(value):
    return struct.pack("<d3h4s", value, -1, 2, 3, b"\xde\xad\xbe\xef")


def test_projected_rows_have_source_identity_and_distinguish_equal_time_messages(
        tmp_path, capsys):
    path = recording(tmp_path / "signal.mcap", [
        (0, payload(0)), (10, payload(1)), (10, payload(2)), (20, payload(3))])

    report = RecordingQuery(path).query(
        "signal", ["value", "samples[1]", "frame[2]"], start_ns=10, end_ns=20)

    assert report["source"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert report["status"] == "complete"
    assert report["scan"]["messages"] == 2
    assert [row["fields"] for row in report["results"]] == [
        {"value": 1.0, "samples[1]": 2, "frame[2]": 190},
        {"value": 2.0, "samples[1]": 2, "frame[2]": 190}]
    assert [row["message_index"] for row in report["results"]] == [0, 1]
    assert all(row["channel"] == "signal" and row["t_ns"] == 10
               for row in report["results"])

    code = cli.main(["recording", "query", str(path), "--channel", "signal",
                     "--field", "samples[1]", "--from", "10", "--to", "20"])
    out, err = capsys.readouterr()
    assert code == 0 and err == ""
    assert json.loads(out)["results"][1]["fields"] == {"samples[1]": 2}


def test_numeric_summary_counts_nonfinite_values_and_cites_first_extremum(tmp_path):
    path = recording(tmp_path / "signal.mcap", [
        (0, payload(float("nan"))), (10, payload(2)), (10, payload(-3)),
        (10, payload(-3)), (20, payload(float("inf"))),
        (30, payload(float("-inf")))])

    report = RecordingQuery(path).query("signal", ["value"], mode="summary")

    assert report["results"] == [{
        "channel": "signal", "field": "value", "count": 6,
        "finite_count": 3, "nonfinite_count": 3,
        "min": {"value": -3.0, "channel": "signal", "field": "value",
                "t_ns": 10, "message_index": 2},
        "max": {"value": 2.0, "channel": "signal", "field": "value",
                "t_ns": 10, "message_index": 1}}]
    empty = RecordingQuery(path).query("signal", ["value"], mode="summary",
                                       start_ns=10, end_ns=10)
    assert empty["status"] == "complete"
    assert empty["results"][0] == {
        "channel": "signal", "field": "value", "count": 0,
        "finite_count": 0, "nonfinite_count": 0, "min": None, "max": None}


def test_crossing_finds_a_one_message_excursion_and_discloses_window_context(tmp_path):
    path = recording(tmp_path / "signal.mcap", [
        (0, payload(8)), (10, payload(5)), (10, payload(6)), (10, payload(4)),
        (20, payload(float("nan"))), (30, payload(8)), (40, payload(5)),
        (50, payload(7))])

    report = RecordingQuery(path).query("signal", ["value"], start_ns=10,
                                       where=Predicate("value", "cross-up", 5))

    assert [row["message_index"] for row in report["results"]] == [1, 6]
    assert report["results"][0]["previous"] == {
        "channel": "signal", "field": "value", "value": 5.0,
        "t_ns": 10, "message_index": 0}
    assert report["crossing_context"] == {
        "outside_window": False, "first_message": "no_crossing",
        "nonfinite": "breaks_adjacency"}
    assert report["scan"]["predicate_nonfinite"] == 1
    assert report["scan"]["matches"] == 2

    # No predecessor is read at t=0; starting already above the threshold is
    # not a crossing. The non-finite Message likewise breaks adjacency.
    selected = RecordingQuery(path).query(
        "signal", ["value"], start_ns=30, end_ns=40,
        where=Predicate("value", "cross-up", 5))
    assert selected["results"] == []
    comparison = RecordingQuery(path).query(
        "signal", ["samples[0]"], start_ns=10, end_ns=20,
        where=Predicate("value", "gt", 5))
    assert [row["message_index"] for row in comparison["results"]] == [1]


def test_result_limits_do_not_stop_exact_scan_and_scan_limits_do(tmp_path, capsys):
    path = recording(tmp_path / "signal.mcap", [(t, payload(t)) for t in range(8)])
    source = RecordingQuery(path)
    limited = source.query("signal", ["value"], max_results=1, max_bytes=4096)

    assert limited["status"] == "limited"
    assert limited["scan"]["complete"] is True
    assert limited["scan"]["messages"] == limited["scan"]["matches"] == 8
    assert limited["output"]["returned"] == 1
    assert limited["output"]["omitted"] == 7
    assert len(render(limited).encode()) <= 4096
    assert limited["output"]["serialized_bytes"] == len(render(limited).encode())

    partial = source.query("signal", ["value"], max_messages=2,
                           where=Predicate("value", "gt", 6))
    assert partial["status"] == "incomplete"
    assert partial["scan"]["complete"] is False
    assert partial["scan"]["messages"] == 2
    assert partial["scan"]["matches"] == 0
    assert partial["scan"]["first_t_ns"] == 0
    assert partial["scan"]["last_t_ns"] == 1
    assert partial["continuation"] is None
    exact = source.query("signal", ["value"], max_messages=8)
    assert exact["status"] == "complete"

    for extra, expected in [(["--max-results", "1"], 4),
                            (["--max-messages", "2"], 3),
                            (["--field", "absent"], 2)]:
        code = cli.main(["recording", "query", str(path), "--channel", "signal",
                         "--field", "value", *extra])
        out, _ = capsys.readouterr()
        assert code == expected
        assert json.loads(out)["status"] == {2: "refused", 3: "incomplete",
                                             4: "limited"}[expected]


@pytest.mark.parametrize("type_,data", [
    ("u8", b"\xff" * 100000), ("f64", struct.pack("<d", 1.0) * 100000)])
def test_a_single_oversized_array_has_an_explicit_bounded_outcome(tmp_path, type_, data):
    path = recording(tmp_path / "large.mcap", [(1, data)],
                     {"fields": [{"name": "large", "type": type_, "count": 100000}]})
    report = RecordingQuery(path).query("signal", ["large"], max_bytes=4096)

    assert report["status"] == "limited"
    assert report["scan"]["complete"] is True
    assert report["output"]["omitted"] == 1
    assert report["output"]["first_omitted"] == {
        "channel": "signal", "t_ns": 1, "message_index": 0,
        "reason": "oversized_result"}
    assert report["results"] == []
    assert len(render(report).encode()) <= 4096
    element = RecordingQuery(path).query("signal", ["large[99999]"], max_bytes=4096)
    assert element["results"][0]["fields"] == {"large[99999]": 255 if type_ == "u8" else 1.0}


@pytest.mark.parametrize("options", [
    {"fields": []}, {"fields": ["value", "value"]}, {"fields": ["unknown"]},
    {"fields": ["samples[3]"]}, {"fields": ["samples[-1]"]},
    {"fields": ["value[0]"]}, {"fields": ["samples"], "mode": "summary"},
    {"where": Predicate("samples", "gt", 1)},
    {"where": Predicate("value", "expression", 1)},
    {"where": Predicate("value", "gt", float("inf"))},
    {"where": Predicate("value", "gt", True)},
    {"where": Predicate("samples[0]", "eq", 2.0)},
    {"where": Predicate("samples[0]", "eq", 32768)},
    {"start_ns": -1}, {"start_ns": 2, "end_ns": 1},
    {"max_results": 0}, {"max_messages": 0}, {"max_bytes": 512}, {"mode": "sql"},
])
def test_invalid_fields_types_and_limits_are_refused(tmp_path, options):
    path = recording(tmp_path / "signal.mcap", [(0, payload(1))])
    with pytest.raises(QueryError):
        RecordingQuery(path).query("signal", **{"fields": ["value"], **options})


def test_query_object_rejects_a_changed_file_even_when_size_is_unchanged(tmp_path):
    path = recording(tmp_path / "signal.mcap", [(0, payload(1))])
    source = RecordingQuery(path)
    first = source.query("signal", ["value"])
    assert source.query("signal", ["samples[0]"])["source"] == first["source"]
    recording(path, [(0, payload(2))])
    assert path.stat().st_size == first["source"]["size_bytes"]
    with pytest.raises(QueryError, match="source changed"):
        source.query("signal", ["value"])


@pytest.mark.parametrize("operator,indices", [
    ("eq", [1]), ("ne", [0, 2]), ("lt", [0]), ("le", [0, 1]),
    ("gt", [2]), ("ge", [1, 2]), ("cross-up", [2]), ("cross-down", [3]),
])
def test_predicate_equality_direction_and_nonfinite_behavior(tmp_path, operator, indices):
    path = recording(tmp_path / "signal.mcap", [
        (0, payload(4)), (10, payload(5)), (10, payload(6)), (20, payload(4)),
        (30, payload(float("nan"))), (40, payload(float("inf"))),
        (50, payload(float("-inf")))])
    end_ns = None if operator.startswith("cross-") else 20
    report = RecordingQuery(path).query("signal", ["value"], end_ns=end_ns,
                                       where=Predicate("value", operator, 5))
    assert [r["message_index"] for r in report["results"]] == indices


def test_uint64_comparisons_and_extrema_keep_integer_precision(tmp_path):
    path = recording(tmp_path / "integer.mcap", [
        (0, struct.pack("<Q", 2**64 - 2)), (0, struct.pack("<Q", 2**64 - 1))],
        {"fields": [{"name": "integer", "type": "u64"}]})
    report = RecordingQuery(path).query(
        "signal", ["integer"], where=Predicate("integer", "eq", 2**64 - 1))
    assert report["results"][0]["message_index"] == 1
    summary = RecordingQuery(path).query("signal", ["integer"], mode="summary")
    assert summary["results"][0]["min"]["value"] == 2**64 - 2
    assert summary["results"][0]["max"]["value"] == 2**64 - 1


def test_nonfinite_only_and_empty_channel_summaries_have_no_extrema(tmp_path):
    path = recording(tmp_path / "signal.mcap", [(0, payload(float("nan")))])
    summary = RecordingQuery(path).query("signal", ["value"], mode="summary")
    assert summary["results"][0]["min"] is summary["results"][0]["max"] is None
    assert summary["results"][0]["nonfinite_count"] == 1
    empty_path = recording(tmp_path / "empty.mcap", [])
    empty = RecordingQuery(empty_path).query("signal", ["value"], mode="summary")
    assert empty["results"][0]["count"] == 0
    assert empty["scan"]["first_t_ns"] is empty["scan"]["last_t_ns"] is None


def test_crossings_refuse_unsorted_times_and_summaries_choose_earliest_ties(tmp_path):
    path = recording(tmp_path / "signal.mcap", [(20, payload(5)), (10, payload(5))])
    summary = RecordingQuery(path).query("signal", ["value"], mode="summary")
    assert summary["results"][0]["min"]["t_ns"] == 10
    assert summary["results"][0]["min"]["message_index"] == 1
    with pytest.raises(QueryError, match="nondecreasing"):
        RecordingQuery(path).query("signal", ["value"],
                                   where=Predicate("value", "cross-up", 4))


def test_a_malformed_message_cannot_hide_behind_a_result_limit(tmp_path):
    path = recording(tmp_path / "signal.mcap", [(0, payload(1)), (1, b"short")])
    with pytest.raises(QueryError, match="does not match"):
        RecordingQuery(path).query("signal", ["value"], max_results=1)


def test_byte_budget_counts_escaped_json_metadata_and_semantic_refusals(tmp_path, capsys):
    path = recording(tmp_path / "signal.mcap", [(0, payload(1))])
    code = cli.main(["recording", "query", str(path), "--channel", "signal",
                     "--field", "unknown" * 10000, "--max-bytes", "2048"])
    out, err = capsys.readouterr()
    report = json.loads(out)
    assert code == 2 and report["status"] == "refused"
    assert len(out.encode()) <= 2048
    assert len(err.encode()) <= 2048
    assert report["request_sha256"]
    assert report["source"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("extra", [["--max-bytes", "oops"], ["--where", "value"],
                                   ["--unexpected"]])
def test_cli_syntax_refusals_are_versioned_reports(extra, tmp_path, capsys):
    code = cli.main(["recording", "query", str(tmp_path / "missing.mcap"),
                     "--channel", "signal", "--field", "value", *extra])
    out, _ = capsys.readouterr()
    assert code == 2
    assert json.loads(out)["status"] == "refused"


def test_retained_adas_regression_crossing_matches_known_event_and_direct_scan():
    from pathlib import Path

    from mcap.reader import make_reader

    root = Path(__file__).resolve().parents[1]
    path = root / ("proofs/agent-investigation/evidence/trial-1/ci/cases/"
                   "delay-regression/runs/delay-regression/run-1.mcap")
    with path.open("rb") as stream:
        # Independent decoder of the published C command layout, without
        # the query module or its Schema projection code.
        direct = [(message.log_time, struct.unpack_from("<q", message.data, 28)[0])
                  for _, _, message in make_reader(stream).iter_messages(
                      topics=["cadence.command"], log_time_order=False)]
    assert direct[8:11] == [(80000000, 40000000), (90000000, 50000000),
                            (100000000, 60000000)]
    report = RecordingQuery(path).query(
        "cadence.command", ["radar_age_ns", "mode"],
        where=Predicate("radar_age_ns", "cross-up", 40000000))
    assert report["scan"]["complete"] is True
    assert [(r["t_ns"], r["message_index"], r["fields"]["radar_age_ns"])
            for r in report["results"]] == [(90000000, 9, 50000000)]
    assert report["results"][0]["fields"]["mode"] == 2


def test_long_recording_proof_checks_events_nearby_inputs_and_retained_measurements(
        tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(root / "proofs/recording-query/verify.py"),
                             "--out", str(tmp_path / "proof"), "--counts", "1000"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    evidence = json.loads((tmp_path / "proof/evidence.json").read_text())
    assert len(evidence["measurements"]) == 2
    for row in evidence["measurements"]:
        assert row["reference"]["every_100th_message_matches"] == 0
        assert row["report"]["results"][0]["message_index"] == 501
        assert row["output_bytes"] <= 4096
        assert row["peak_rss_bytes"] > 0


def test_declared_field_names_are_preserved_before_parsing_array_suffixes(tmp_path):
    path = recording(tmp_path / "names.mcap", [(0, struct.pack("<d3hB", 2.5, 1, 2, 3, 9))],
                     {"fields": [{"name": "plant.temperature", "type": "f64"},
                                 {"name": "rädar values", "type": "i16", "count": 3},
                                 {"name": "rädar values[1]", "type": "u8"}]})
    source = RecordingQuery(path)
    projected = source.query("signal", ["plant.temperature", "rädar values[1]",
                                        "rädar values[2]"])
    assert projected["results"][0]["fields"] == {
        "plant.temperature": 2.5, "rädar values[1]": 9, "rädar values[2]": 3}
    summary = source.query("signal", ["plant.temperature"], mode="summary",
                           where=Predicate("rädar values[2]", "eq", 3))
    assert summary["results"][0]["max"]["value"] == 2.5


@pytest.mark.parametrize("op,literal", [("eq", 6), ("ne", 5), ("lt", 7),
                                      ("le", 6), ("gt", 5), ("ge", 6)])
def test_match_evidence_includes_predicate_value_outside_projected_fields(
        tmp_path, capsys, op, literal):
    path = recording(tmp_path / "signal.mcap", [(10, payload(6))])
    code = cli.main(["recording", "query", str(path), "--channel", "signal",
                     "--field", "samples[1]", "--where", "value", op, str(literal)])
    out, err = capsys.readouterr()
    assert code == 0 and err == ""
    row = json.loads(out)["results"][0]
    assert row["fields"] == {"samples[1]": 2}
    assert row["match"] == {"channel": "signal", "field": "value", "value": 6.0,
                             "t_ns": 10, "message_index": 0}


def test_partial_unsorted_scan_reports_positions_without_claiming_time_coverage(tmp_path):
    path = recording(tmp_path / "signal.mcap", [
        (30, payload(1)), (10, payload(2)), (20, payload(3)), (15, payload(4))])
    report = RecordingQuery(path).query("signal", ["value"], start_ns=10, end_ns=40,
                                       max_messages=2)
    assert report["status"] == "incomplete"
    scan = report["scan"]
    assert scan["inspected_message_range"] == [0, 2]
    assert scan["first_message"] == {"channel": "signal", "t_ns": 30, "message_index": 0}
    assert scan["last_message"] == {"channel": "signal", "t_ns": 10, "message_index": 1}
    assert scan["next_message"] == {"channel": "signal", "t_ns": 20, "message_index": 2}
    assert (scan["first_t_ns"], scan["last_t_ns"]) == (10, 30)


@pytest.mark.parametrize("transport,slots", [("inline", 1), ("shm", 1), ("shm", 2)])
def test_query_preserves_actual_participant_bursts_and_finds_same_slot_crossing(
        run_sil, tmp_path, transport, slots):
    import sys

    from conftest import ROOT
    from sil.manifest import Manifest
    from test_run_boundary import ARRAY_SCHEMAS

    manifest = Manifest(duration_ns=30000000)
    manifest.add_schemas(ARRAY_SCHEMAS)
    manifest.add_channel("payload", schema="big.Payload", transport=transport,
                         slots=slots if transport == "shm" else None)
    manifest.add_process("publisher",
                         command=[sys.executable, str(ROOT / "tests/participants/burst_array_source.py")],
                         step_period_ns=10000000, publishes=["payload"])
    run = run_sil(manifest.write(tmp_path / "burst.json").path)
    assert run.returncode == 0, run.stderr
    source = RecordingQuery(run.mcap_path)
    rows = source.query("payload", ["id"])["results"]
    assert [(r["t_ns"], r["message_index"], r["fields"]["id"]) for r in rows] == [
        (0, 0, 0), (0, 1, 1), (10000000, 2, 2), (10000000, 3, 3),
        (20000000, 4, 4), (20000000, 5, 5)]
    crossing = source.query("payload", ["blob[0]"], where=Predicate("id", "cross-up", 2))
    assert crossing["results"][0]["match"] == {
        "channel": "payload", "t_ns": 10000000, "message_index": 3, "field": "id", "value": 3}
    assert crossing["results"][0]["previous"]["message_index"] == 2
    assert crossing["results"][0]["fields"] == {"blob[0]": 3}


@pytest.mark.parametrize("spec", [{"fields": None}, {"fields": [None]},
                                   {"fields": [{"name": "value", "type": []}]}])
def test_malformed_schema_is_refused_at_the_recording_input_boundary(tmp_path, spec):
    path = recording(tmp_path / "schema.mcap", [(0, b"")], spec)
    with pytest.raises(QueryError, match="invalid SiL Schema"):
        RecordingQuery(path).query("signal", ["value"])


def test_an_enormous_array_index_has_a_bounded_cli_refusal(tmp_path, capsys):
    path = recording(tmp_path / "signal.mcap", [(0, payload(1))])
    code = cli.main(["recording", "query", str(path), "--channel", "signal",
                     "--field", "samples[" + "1" * 5000 + "]", "--max-bytes", "2048"])
    out, err = capsys.readouterr()
    assert code == 2
    assert json.loads(out)["status"] == "refused"
    assert len(out.encode()) <= 2048
    assert len(err.encode()) <= 2048


def test_a_python_integer_literal_beyond_json_conversion_limits_is_refused(tmp_path):
    path = recording(tmp_path / "signal.mcap", [(0, payload(1))])
    with pytest.raises(QueryError, match="JSON number"):
        RecordingQuery(path).query("signal", ["value"],
                                   where=Predicate("value", "gt", 10**5000))
