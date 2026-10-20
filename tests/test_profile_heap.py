"""Normalization of Heaptrack's interpreted data file (#317).

The input is hand-written in Heaptrack's interpreted format, file version 3
(the format of Heaptrack 1.4 and 1.5, proofs/profiling/README.md). Each
expected value below is counted by hand from the listed events, not by the
code under test:

    + 1          retain_site   65536   live 65536
    + 0          alloc_site     1024   live 66560
    + 0          alloc_site     1024   live 67584  <- peak
    - 0          directly after "+ 0": temporary 1      live 66560
    - 0          after a free: not temporary            live 65536
    + 2, - 2     churn (inlined helper)    32: temporary 2
    + 2, - 2     churn                     32: temporary 3
    + 3          no module                 16: outstanding
    + 4          native, no function name   8: outstanding
    + 5, - 5     alloc_site, recursive   1024: temporary 4

8 allocations, 68696 bytes, 5 frees, 4 temporary, 3 outstanding
(65536 + 16 + 8 = 65560 bytes), peak 67584 tracked bytes.
"""

from __future__ import annotations

import pytest

from sil.profile_heap import HeapDecodeError, normalize_heap

CHURN = "churn_site(int, char)"
DATA = "\n".join([
    "v 10500 3",
    "X /w/sil-run-diagnostics manifest.json -o run.mcap",
    "I 1000 7a120",
    "s a /w/sil-run",            # 1
    "s f /w/libnative.so",       # 2
    "s 4 main",                  # 3
    "s a alloc_site",            # 4
    "s b retain_site",           # 5
    "s e inlined_helper",        # 6
    "s 8 native.c",              # 7
    f"s {len(CHURN):x} {CHURN}",  # 8
    "i 1000 1 3 0 0",            # ip 1: main, no source line
    "i 2000 2 4 7 a",            # ip 2: alloc_site native.c:10
    "i 3000 2 5 7 14",           # ip 3: retain_site native.c:20
    "i 4000 2 6 7 1e 8 7 28",    # ip 4: inlined_helper:30 in churn_site:40
    "i 5000 0",                  # ip 5: no module
    "i 6000 2",                  # ip 6: native, no function name
    "t 1 0",                     # trace 1: main
    "t 2 1",                     # trace 2: alloc_site <- main
    "t 3 1",                     # trace 3: retain_site <- main
    "t 4 1",                     # trace 4: churn <- main
    "t 5 1",                     # trace 5: unknown <- main
    "t 6 2",                     # trace 6: native ? <- alloc_site <- main
    "t 2 2",                     # trace 7: alloc_site <- alloc_site <- main
    "a 400 2", "a 10000 3", "a 20 4", "a 10 5", "a 8 6", "a 400 7",
    "+ 1", "+ 0", "+ 0", "c a", "- 0", "- 0",
    "+ 2", "- 2", "+ 2", "- 2", "R 40",
    "+ 3", "+ 4", "+ 5", "- 5",
    "c 14",
]) + "\n"

BUILD_IDS = {"/w/libnative.so": "abcd01"}


def build_id(path: str):
    found = BUILD_IDS.get(path)
    return found, None if found else "module_unavailable"


def analysis(text: str = DATA, **options):
    return normalize_heap(text.encode(), build_id=build_id, **options)


def row(rows, symbol):
    found = [r for r in rows if r["symbol"] == symbol]
    assert len(found) == 1, (symbol, rows)
    return found[0]


def test_totals_count_each_allocation_event_once():
    totals = analysis().record["totals"]

    assert totals == {
        "allocations": 8, "allocated_bytes": 68696, "frees": 5,
        "temporary_allocations": 4, "outstanding_allocations": 3,
        "outstanding_bytes": 65560, "peak_tracked_bytes": 67584}


def test_self_rows_attribute_each_allocation_to_its_innermost_frame():
    result = analysis().record
    rows = result["hotspots"]["self"]

    alloc = row(rows, "alloc_site")
    assert (alloc["allocations"], alloc["allocated_bytes"],
            alloc["temporary_allocations"], alloc["outstanding_allocations"],
            alloc["outstanding_bytes"]) == (3, 3072, 2, 0, 0)
    assert alloc["source_lines"] == [
        {"file": "native.c", "line": 10, "allocations": 3}]
    retain = row(rows, "retain_site")
    assert (retain["allocations"], retain["outstanding_allocations"],
            retain["outstanding_bytes"]) == (1, 1, 65536)
    # An inlined function is the innermost frame of its address.
    helper = row(rows, "inlined_helper")
    assert (helper["allocations"], helper["allocated_bytes"],
            helper["temporary_allocations"]) == (2, 64, 2)
    assert helper["source_lines"] == [
        {"file": "native.c", "line": 30, "allocations": 2}]
    assert CHURN not in {r["symbol"] for r in rows}
    unknown = [r for r in rows if r["symbol"] is None]
    assert sorted((r["module"] is None, r["allocations"],
                   r["allocated_bytes"]) for r in unknown) == [
        (False, 1, 8), (True, 1, 16)]
    # Every allocation is in exactly one self row.
    assert sum(r["allocations"] for r in rows) == 8
    assert result["hotspots"]["self_remainder"] == {
        "allocations": 0, "allocated_bytes": 0, "temporary_allocations": 0,
        "outstanding_allocations": 0, "outstanding_bytes": 0}


def test_inclusive_rows_count_a_recursive_function_once_per_allocation():
    rows = analysis().record["hotspots"]["inclusive"]

    main = row(rows, "main")
    assert (main["allocations"], main["allocated_bytes"]) == (8, 68696)
    # a0 (2 x 1024), a4 (8 below it) and a5 (alloc_site twice in its stack).
    alloc = row(rows, "alloc_site")
    assert (alloc["allocations"], alloc["allocated_bytes"]) == (4, 3080)
    churn = row(rows, CHURN)
    assert (churn["allocations"], churn["allocated_bytes"]) == (2, 64)


def test_modules_carry_path_build_id_and_leaf_counts():
    record = analysis().record
    modules = {m["path"]: m for m in record["modules"]}

    native = modules["/w/libnative.so"]
    assert native["build_id"] == "abcd01"
    assert (native["allocations_leaf"],
            native["allocations_unknown_symbol"]) == (7, 1)
    # Heaptrack does not report which debug file it used, if any.
    assert native["debug"] is None
    assert native["unavailable"] == {"debug": "not_reported_by_backend"}
    app = modules["/w/sil-run"]
    assert app["build_id"] is None
    assert app["unavailable"] == {"build_id": "module_unavailable",
                                  "debug": "not_reported_by_backend"}
    assert app["allocations_leaf"] == 0
    coverage = record["coverage"]
    assert (coverage["allocations_unknown_module"],
            coverage["allocations_unknown_symbol"],
            coverage["allocations_with_caller"]) == (1, 1, 8)
    assert coverage["undecodable_records"] == 0
    assert coverage["truncated"] is False


def test_backend_identity_comes_from_the_data_file():
    record = analysis().record

    assert record["file_version"] == 3
    assert record["heaptrack_version"] == "1.5.0"
    assert record["traced_executable"] == \
        "/w/sil-run-diagnostics manifest.json -o run.mcap"


def test_a_limit_keeps_the_top_rows_of_each_metric_and_a_remainder():
    hotspots = analysis(limit=1).record["hotspots"]

    # Top by bytes and by outstanding bytes: retain_site; by allocations
    # and by temporary allocations: alloc_site.
    assert [r["symbol"] for r in hotspots["self"]] == [
        "retain_site", "alloc_site"]
    assert hotspots["self_remainder"] == {
        "allocations": 4, "allocated_bytes": 88, "temporary_allocations": 2,
        "outstanding_allocations": 2, "outstanding_bytes": 24}


def test_the_allocations_file_holds_every_allocation_info_and_its_stack():
    document = analysis().allocations_document

    assert document["sil_profile_heap_allocations"] == 1
    rows = document["allocations"]
    assert len(rows) == 6
    frames, stacks = document["frames"], document["stacks"]

    def symbols(stack):
        return [frames[i]["symbol"] for i in stacks[stack]]

    # [stack, size_bytes, allocations, temporary, outstanding]
    assert rows[1][1:] == [65536, 1, 0, 1]
    assert symbols(rows[1][0]) == ["retain_site", "main"]
    assert symbols(rows[2][0]) == ["inlined_helper", CHURN, "main"]
    assert symbols(rows[5][0]) == ["alloc_site", "alloc_site", "main"]
    assert frames[stacks[rows[2][0]][0]]["address"] == "0x4000"
    assert frames[stacks[rows[0][0]][0]]["source"] == {"file": "native.c",
                                                       "line": 10}


def test_the_interpreter_trailer_is_not_an_undecodable_record():
    record = analysis(DATA + "\n# strings: 8\n# ips: 6\n").record

    assert record["coverage"]["undecodable_records"] == 0


def test_a_cut_last_record_is_reported_and_skipped():
    record = analysis(DATA + "+ 3").record

    assert record["coverage"]["truncated"] is True
    assert record["totals"]["allocations"] == 8


def test_an_unknown_allocation_index_is_counted_not_trusted():
    record = analysis(DATA + "+ 99\n- 3\n- 3\n").record

    # "- 3" frees the outstanding 16 bytes once; the second has no live
    # allocation left.
    assert record["coverage"]["undecodable_records"] == 2
    assert record["totals"]["frees"] == 6
    assert record["totals"]["outstanding_bytes"] == 65544


def test_a_string_cut_by_the_end_of_the_data_is_a_truncation():
    # The string of 5 bytes would need 3 more bytes and its newline.
    record = analysis(DATA + "s 5 xy\n").record

    assert record["coverage"]["truncated"] is True
    assert record["totals"]["allocations"] == 8


@pytest.mark.parametrize("bad", ["s 2 abc", "s zz abc", "s 3abc"])
def test_a_malformed_string_record_does_not_decode(bad):
    # A lost string moves every later string index: no symbol is trusted.
    text = DATA.replace("s 4 main", bad, 1)

    with pytest.raises(HeapDecodeError, match="string record"):
        analysis(text)


@pytest.mark.parametrize("header", ["v 10500 2", "", "x"])
def test_data_without_file_version_3_does_not_decode(header):
    text = DATA.replace("v 10500 3", header, 1)

    with pytest.raises(HeapDecodeError):
        analysis(text)


# A custom pool: arena_create takes one block from malloc, and arena_take
# gives out objects from it with no allocation call. Heaptrack sees the
# block only, so the analysis has one allocation of 65536 bytes and no row
# for arena_take.
ARENA = "\n".join([
    "v 10500 3",
    "X /w/sil-run-diagnostics manifest.json",
    "I 1000 7a120",
    "s a /w/sil-run",            # 1
    "s f /w/libnative.so",       # 2
    "s 4 main",                  # 3
    "s c arena_create",          # 4
    "s a arena_take",            # 5
    "i 1000 1 3 0 0",            # ip 1: main
    "i 2000 2 4 0 0",            # ip 2: arena_create
    "i 3000 2 5 0 0",            # ip 3: arena_take
    "t 1 0",                     # trace 1: main
    "t 2 1",                     # trace 2: arena_create <- main
    "t 3 1",                     # trace 3: arena_take <- main
    "a 10000 2",
    "+ 0",
    "c a",
]) + "\n"


def test_a_custom_pool_shows_only_its_backing_allocation():
    record = analysis(ARENA).record

    assert (record["totals"]["allocations"],
            record["totals"]["allocated_bytes"],
            record["totals"]["outstanding_allocations"]) == (1, 65536, 1)
    symbols = {r["symbol"] for r in record["hotspots"]["self"]}
    assert symbols == {"arena_create"}
