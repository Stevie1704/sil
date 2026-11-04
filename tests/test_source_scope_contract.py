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
HEADER = struct.Struct("<8sHHHH16sIIQQQQQ3QQQB40s95x")
HEADER_FIELDS = ("magic", "version", "header_bytes", "record_bytes", "state",
                 "capture", "pid", "flags", "capacity", "records", "dropped_full",
                 "dropped_contended", "clock_errors", "bracket_0", "bracket_1",
                 "bracket_2", "pidns_device", "pidns_inode", "build_id_bytes",
                 "build_id")
RECORD = struct.Struct("<QIHBB")
SITE = struct.Struct("<4sHHI64s180s")
FINALIZED = 2
ENTRY, RETURN = 0, 1
UNAVAILABLE_REASONS = {"observations_incomplete", "events_lost", "not_completed",
                       "unbalanced_scopes", "crossing_call"}
UNATTRIBUTED_REASONS = {"worker_thread", "outside_activations", "crossing_interval",
                        "other_participant", "missing_return", "missing_entry",
                        "observations_incomplete"}


def header(data):
    raw = dict(zip(HEADER_FIELDS, HEADER.unpack_from(data)))
    raw["capture_id"] = str(uuid.UUID(bytes=raw["capture"]))
    raw["build_id"] = raw["build_id"][:raw["build_id_bytes"]].hex()
    return raw


def load(case):
    folder = FIXTURES / case
    observations = [json.loads(line) for line in
                    (folder / "observations.jsonl").read_text().splitlines()]
    data = (folder / "events.bin").read_bytes()
    return {"map": json.loads((FIXTURES / "map.json").read_text()),
            "raw": header(data), "bytes": data, "observations": observations,
            "expected": json.loads((folder / "expected.json").read_text())}


def records(data):
    return [RECORD.unpack_from(data["bytes"], HEADER.size + RECORD.size * index)
            for index in range(data["raw"]["records"])]


def text(field):
    return field.rstrip(b"\0").decode()


def test_every_kind_of_outcome_has_a_worked_example():
    outcomes = {case: load(case)["expected"] for case in CASES}
    assert any("error" in result for result in outcomes.values())
    assert any(result.get("status") == "complete" for result in outcomes.values())
    assert any(result.get("status") == "partial" for result in outcomes.values())


def test_scope_map_groups_the_section_sites_by_id():
    section = (FIXTURES / "scope-section.bin").read_bytes()
    assert SITE.size == 256 and len(section) % SITE.size == 0
    sites = {}
    for offset in range(0, len(section), SITE.size):
        magic, version, scope, line, name, file = SITE.unpack_from(section, offset)
        assert (magic, version) == (b"SSC1", 1)
        entry = sites.setdefault(scope, {"id": scope, "name": text(name), "sites": set()})
        assert entry["name"] == text(name)
        entry["sites"].add((text(file), line))
    expected = [{**entry, "sites": [{"file": f, "line": n} for f, n in sorted(entry["sites"])]}
                for _, entry in sorted(sites.items())]
    assert json.loads((FIXTURES / "map.json").read_text())["scopes"] == expected


@pytest.mark.parametrize("case", CASES)
def test_raw_header_is_finalized_and_sized_by_capacity(case):
    data = load(case)
    raw = data["raw"]
    assert (raw["magic"], raw["version"], raw["header_bytes"], raw["record_bytes"],
            raw["flags"]) == (b"SILSCOPE", 1, HEADER.size, RECORD.size, 0)
    assert raw["state"] == FINALIZED
    assert len(data["bytes"]) == HEADER.size + RECORD.size * raw["capacity"]
    assert raw["records"] <= raw["capacity"]
    assert raw["dropped_full"] == 0 or raw["records"] == raw["capacity"]
    assert raw["bracket_0"] <= raw["bracket_1"] <= raw["bracket_2"]
    unused = data["bytes"][HEADER.size + RECORD.size * raw["records"]:]
    assert unused == bytes(len(unused))


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
    matches = data["raw"]["build_id"] == data["map"]["library"]["build_id"]
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


def test_ordinary_runner_binary_has_no_scope_observer(sil_run):
    """Q18: no symbol, string or link of the observer in sil-run."""
    binary = sil_run.read_bytes()
    for name in (b"sil_source_scope", b"SIL_SOURCE_SCOPE",
                 b"libsil_source_scope"):
        assert name not in binary
