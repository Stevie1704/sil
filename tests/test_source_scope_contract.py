"""The #360 source-scope fixtures agree with their contract.

docs/proposals/native-source-scopes.md defines the files. The expected
normalized outputs are worked examples, derived by hand; the future decoder
must reproduce them. These tests keep the raw inputs, the map and the worked
examples consistent with each other, so a fixture edit cannot silently break
the contract that the decoder ticket implements.
"""
from collections import Counter
import json
from pathlib import Path
import struct
import uuid

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures/source_scopes"
CASES = sorted(path.name for path in FIXTURES.iterdir() if path.is_dir())
HEADER = struct.Struct("<8sHHI16sIIQ16x")
RECORD = struct.Struct("<QIHBB")
ENTRY, RETURN = 0, 1
UNAVAILABLE_REASONS = {"observations_incomplete", "events_lost", "not_completed",
                       "unbalanced_scopes", "crossing_call"}
UNATTRIBUTED_REASONS = {"worker_thread", "outside_activations", "crossing_interval",
                        "other_participant", "missing_return", "missing_entry",
                        "observations_incomplete"}


def load(case):
    folder = FIXTURES / case
    observations = [json.loads(line) for line in
                    (folder / "observations.jsonl").read_text().splitlines()]
    return {"map": json.loads((FIXTURES / "map.json").read_text()),
            "raw": json.loads((folder / "events.json").read_text()),
            "bytes": (folder / "events.bin").read_bytes(),
            "observations": observations,
            "expected": json.loads((folder / "expected.json").read_text())}


def records(data):
    body = data["bytes"][HEADER.size:]
    return [RECORD.unpack_from(body, offset)
            for offset in range(0, len(body), RECORD.size)]


def test_every_kind_of_outcome_has_a_worked_example():
    outcomes = {case: load(case)["expected"] for case in CASES}
    assert any("error" in result for result in outcomes.values())
    assert any(result.get("status") == "complete" for result in outcomes.values())
    assert any(result.get("status") == "partial" for result in outcomes.values())


@pytest.mark.parametrize("case", CASES)
def test_raw_file_matches_its_metadata(case):
    data = load(case)
    raw = data["raw"]
    magic, version, record_bytes, flags, capture, pid, _, capacity = \
        HEADER.unpack_from(data["bytes"])
    assert (magic, version, record_bytes, flags) == (b"SILSCOPE", 1, 16, 0)
    assert uuid.UUID(bytes=capture) == uuid.UUID(raw["capture_id"])
    assert (pid, capacity) == (raw["pid"], raw["capacity_records"])
    assert raw["sil_source_scope_raw"] == 1
    assert raw["write_status"] == "complete"
    assert len(data["bytes"]) == raw["bytes_written"] == \
        HEADER.size + RECORD.size * raw["records"]
    assert raw["records"] <= capacity
    assert raw["dropped_full"] == 0 or raw["records"] == capacity


@pytest.mark.parametrize("case", CASES)
def test_records_name_mapped_scopes_in_per_thread_time_order(case):
    data = load(case)
    scopes = {scope["id"] for scope in data["map"]["scopes"]}
    last = {}
    for ns, tid, scope, edge, reserved in records(data):
        assert scope in scopes and edge in (ENTRY, RETURN) and reserved == 0
        assert ns >= last.get(tid, 0)
        last[tid] = ns


@pytest.mark.parametrize("case", CASES)
def test_build_id_mismatch_has_an_error_example(case):
    data = load(case)
    matches = data["raw"]["library"]["build_id"] == data["map"]["library"]["build_id"]
    assert matches == ("error" not in data["expected"])
    if not matches:
        assert data["expected"] == {"error": "identity_mismatch"}


def decoded_cases():
    return [case for case in CASES if "error" not in load(case)["expected"]]


@pytest.mark.parametrize("case", decoded_cases())
def test_calls_reconcile_with_raw_edges(case):
    data = load(case)
    result, edges = data["expected"], Counter(row[3] for row in records(data))
    totals = result["totals"]
    assert (totals["entries"], totals["returns"]) == (edges[ENTRY], edges[RETURN])
    exact = [a for a in result["activations"] if a["status"] == "exact"]
    assert totals["attributed_calls"] == sum(len(a["calls"]) for a in exact)
    assert totals["withheld_calls"] == sum(
        a["withheld_calls"] for a in result["activations"])
    rows = result["unattributed"]["rows"]
    assert result["unattributed"]["rows_remainder"] == 0
    assert totals["unattributed"] == len(rows) == \
        sum(result["unattributed"]["reasons"].values())
    assert Counter(row["reason"] for row in rows) == \
        Counter(result["unattributed"]["reasons"])
    assert set(result["unattributed"]["reasons"]) <= UNATTRIBUTED_REASONS
    paired = [row for row in rows if row["entry_ns"] and row["return_ns"]]
    assert totals["attributed_calls"] + totals["withheld_calls"] + len(paired) == \
        totals["paired_calls"]
    assert sum(row["return_ns"] is None for row in rows) == \
        totals["entries"] - totals["paired_calls"]
    assert sum(row["entry_ns"] is None for row in rows) == \
        totals["returns"] - totals["paired_calls"]


@pytest.mark.parametrize("case", decoded_cases())
def test_every_listed_edge_is_a_distinct_raw_record(case):
    data = load(case)
    unused = Counter((tid, scope, edge, ns) for ns, tid, scope, edge, _ in records(data))
    runner = data["observations"][0]["runner_tid"]
    listed = [(runner, call) for activation in data["expected"]["activations"]
              for call in activation["calls"]]
    listed += [(row["tid"], row) for row in data["expected"]["unattributed"]["rows"]]
    for tid, call in listed:
        for edge, key in ((ENTRY, "entry_ns"), (RETURN, "return_ns")):
            if call[key] is not None:
                record = (tid, call["scope"], edge, int(call[key]))
                assert unused[record] > 0, record
                unused[record] -= 1


@pytest.mark.parametrize("case", decoded_cases())
def test_activations_follow_the_observation_stream(case):
    data = load(case)
    result = data["expected"]
    assert result["sil_source_scope_calls"] == 1
    assert result["capture_id"] == data["raw"]["capture_id"]
    assert (result["status"] == "partial") == bool(result["problems"])
    keys = ("participant", "task", "index", "virtual_time_ns", "begin_ns",
            "end_ns", "outcome")
    observed = [{key: row[key] for key in keys} for row in data["observations"]
                if row.get("kind") == "activation"
                and row["participant"] == result["participant"]
                and row["task_kind"] == "native_task"]
    assert [{key: a[key] for key in keys} for a in result["activations"]] == observed


@pytest.mark.parametrize("case", decoded_cases())
def test_exact_activations_hold_contained_nested_calls(case):
    for activation in load(case)["expected"]["activations"]:
        if activation["status"] != "exact":
            assert activation["reason"] in UNAVAILABLE_REASONS
            assert (activation["calls"], activation["counts"]) == ([], None)
            continue
        assert activation["reason"] is None and activation["withheld_calls"] == 0
        begin, end = int(activation["begin_ns"]), int(activation["end_ns"])
        calls = activation["calls"]
        for index, call in enumerate(calls):
            entry, ret = int(call["entry_ns"]), int(call["return_ns"])
            assert begin <= entry <= ret < end
            assert int(call["elapsed_ns"]) == ret - entry
            parent = call["parent"]
            if parent is None:
                assert call["depth"] == 0
            else:
                assert parent < index
                assert call["depth"] == calls[parent]["depth"] + 1
                assert int(calls[parent]["entry_ns"]) <= entry
                assert ret <= int(calls[parent]["return_ns"])
        assert activation["counts"] == {
            str(scope): count for scope, count in
            Counter(call["scope"] for call in calls).items()}


def test_ordinary_runner_sources_have_no_scope_observer():
    kernel = FIXTURES.parents[2] / "kernel"
    sources = [path for path in kernel.rglob("*") if path.suffix in
               (".c", ".cpp", ".h", ".hpp", ".txt", ".cmake")]
    assert sources
    assert not [path for path in sources
                if "sil_source_scope" in path.read_text(errors="replace")]
