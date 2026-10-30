"""Indexed Recording navigation and exact snapshots (issue #347, spec #345
s1 and s3 "Indexing and resource bounds").

Sources are `sil_pod` MCAP written by tests/visualization_fixture.py. The
reference for every range and snapshot is a plain sequential scan of the
source in stored order (`reference_records`), so expected values never come
from the index under test.
"""

from __future__ import annotations

import hashlib
import os
import random
import sqlite3
import struct
import threading

import pytest
from mcap.reader import make_reader
from mcap.writer import CompressionType
from visualization_fixture import write_source

from sil.visualization.navigation import (ContinuationError, IndexCorrupt,
                                         IndexStorageError, InvalidRequest,
                                         LimitExceeded, Limits, PageLimits,
                                         PreparationCancelled,
                                         prepare_navigation)
from sil.visualization.source import SelectionError, SourceError, open_source

EVENT = {"e.Event": {"fields": [{"name": "value", "type": "u64"}]}}
TWO = {"a": "e.Event", "b": "e.Event"}
MAX_TIME = 2**64 - 1


def reference_records(path):
    """Every Message in stored order, from a sequential scan of the source
    with the existing stored-order reader."""
    return list(open_source(path).messages())


def reference_range(records, channels, start_ns, end_ns):
    return sorted((r for r in records if r.channel in channels
                   and start_ns <= r.publication_time_ns < end_ns),
                  key=lambda r: (r.publication_time_ns, r.record_index))


def reference_snapshot(records, channels, time_ns):
    latest = {name: None for name in channels}
    for record in records:  # stored order: a later equal-time Message wins
        if record.channel in latest and record.publication_time_ns <= time_ns:
            held = latest[record.channel]
            if held is None or record.publication_time_ns >= held.publication_time_ns:
                latest[record.channel] = record
    return latest


def all_pages(nav, channels, start_ns, end_ns, limits=None):
    """Every page of a request, followed through its continuations."""
    pages = [nav.messages(channels, start_ns, end_ns, limits=limits)]
    while not pages[-1].complete:
        pages.append(nav.messages(channels, start_ns, end_ns,
                                  pages[-1].continuation, limits))
    return pages


THREE = {"a": "e.Event", "b": "e.Event", "sparse": "e.Event"}


def irregular_messages(count=3000, seed=345):
    """Equal-time Bursts, decreasing stored times, 64-bit extremes and a
    Channel with one Message, in a fixed seeded Publish order."""
    rng = random.Random(seed)
    times = [0, 1, 2**53 + 1, 2**63 - 1, 2**63, 2**63 + 1, MAX_TIME - 1, MAX_TIME]
    messages, t = [], 1_000
    for step in range(count):
        if step % 97 == 0:
            t = rng.randrange(0, 2_000_000)  # stored times decrease
        elif step % 5:
            t += rng.choice((0, 0, 1, 10, 1000))  # Bursts share a time
        messages.append((rng.choice("ab"), t, {"value": 2**64 - 1 - step}))
        if step == count // 2:
            messages.append(("sparse", 1_500_000, {"value": 7}))
        if step % 400 == 0:
            messages.append((rng.choice("ab"), times[step // 400 % len(times)],
                             {"value": step}))
    return messages


@pytest.fixture(scope="module")
def irregular(tmp_path_factory):
    directory = tmp_path_factory.mktemp("irregular")
    source = write_source(directory / "run.mcap", EVENT, THREE,
                          irregular_messages(), chunk_size=4096)
    return source, reference_records(source)


def test_describe_reports_identity_catalog_counts_and_bounds(tmp_path):
    source = write_source(tmp_path / "run.mcap", EVENT, TWO, [
        ("a", 5, {"value": 1}), ("b", 3, {"value": 2}), ("a", 9, {"value": 3}),
    ], metadata={"manifest_hash": "m-hash"})

    with prepare_navigation(source, cache_dir=tmp_path / "cache") as nav:
        described = nav.describe()

    assert described.source.sha256 == hashlib.sha256(
        source.read_bytes()).hexdigest()
    assert described.source.manifest_hash == "m-hash"
    assert [(c.name, c.schema_name, c.message_count)
            for c in described.channels] == [("a", "e.Event", 2),
                                              ("b", "e.Event", 1)]
    assert described.message_count == 3
    assert (described.start_ns, described.end_ns) == (3, 9)
    assert described.completeness["run_outcome"] == "unknown"
    assert described.index.size_bytes > 0


def test_ranges_match_a_sequential_reference_for_any_selection(irregular, tmp_path):
    source, records = irregular
    rng = random.Random(7)
    selections = [None, ["a"], ["sparse"], ["b", "sparse"], ["b", "a"]]
    with prepare_navigation(source, cache_dir=tmp_path) as nav:
        for _ in range(60):
            channels = rng.choice(selections)
            names = list(THREE) if channels is None else channels
            start = rng.choice([0, rng.randrange(0, 2_100_000), 2**63])
            end = rng.choice([start, start + rng.randrange(1, 300_000), 2**64])
            pages = all_pages(nav, channels, start, end,
                              PageLimits(max_rows=rng.randrange(1, 400)))
            got = [m for page in pages for m in page.messages]
            assert got == reference_range(records, names, start, end)
            assert pages[-1].complete and pages[-1].continuation is None


def test_snapshots_match_a_sequential_reference_on_random_seeks(irregular, tmp_path):
    source, records = irregular
    rng = random.Random(11)
    seeks = [0, 999, 1_500_000, 1_499_999, 2**63 - 1, 2**63, MAX_TIME]
    seeks += [rng.randrange(0, 2_100_000) for _ in range(150)]
    rng.shuffle(seeks)  # forward and backward seeks
    with prepare_navigation(source, cache_dir=tmp_path) as nav:
        for time_ns in seeks:
            assert nav.snapshot(None, time_ns) == reference_snapshot(
                records, THREE, time_ns), time_ns


def test_a_seek_before_the_first_message_of_a_channel_is_absent_not_zero(
        irregular, tmp_path):
    source, _ = irregular
    with prepare_navigation(source, cache_dir=tmp_path) as nav:
        assert nav.snapshot(["sparse"], 1_499_999) == {"sparse": None}
        (record,) = nav.snapshot(["sparse"], 1_500_000).values()
        assert (record.publication_time_ns, record.data) == (
            1_500_000, (7).to_bytes(8, "little"))
        assert nav.snapshot(["sparse"], MAX_TIME)["sparse"] == record


def test_snapshot_takes_no_future_value_and_the_last_of_an_equal_time_burst(
        tmp_path):
    source = write_source(tmp_path / "run.mcap", EVENT, TWO, [
        ("a", 10, {"value": 1}), ("a", 20, {"value": 2}),
        ("a", 20, {"value": 3}), ("a", 30, {"value": 4}),
    ])
    with prepare_navigation(source, cache_dir=tmp_path / "cache") as nav:
        at_20 = nav.snapshot(["a", "b"], 20)
        at_29 = nav.snapshot(["a"], 29)
        at_9 = nav.snapshot(["a"], 9)

    assert (at_20["a"].record_index, at_20["a"].data) == (
        2, (3).to_bytes(8, "little"))
    assert at_20["b"] is None
    assert at_29["a"].record_index == 2
    assert at_9 == {"a": None}


def test_locators_and_full_64_bit_times_are_unchanged_by_filtering(tmp_path):
    source = write_source(tmp_path / "run.mcap", EVENT, TWO, [
        ("a", MAX_TIME, {"value": MAX_TIME}), ("b", 2**63, {"value": 1}),
        ("a", 2**53 + 1, {"value": 2**53 + 1}), ("b", 0, {"value": 0}),
    ])
    with prepare_navigation(source, cache_dir=tmp_path / "cache") as nav:
        only_a = nav.messages(["a"], 0, 2**64).messages
        everything = nav.messages(None, 0, 2**64).messages

    assert [(m.record_index, m.publication_time_ns) for m in only_a] == [
        (2, 2**53 + 1), (0, MAX_TIME)]
    assert [(m.record_index, m.publication_time_ns) for m in everything] == [
        (3, 0), (2, 2**53 + 1), (1, 2**63), (0, MAX_TIME)]
    assert only_a[1].data == MAX_TIME.to_bytes(8, "little")


def test_an_empty_source_has_no_bounds_messages_or_values(tmp_path):
    source = write_source(tmp_path / "run.mcap", EVENT, TWO, [])
    with prepare_navigation(source, cache_dir=tmp_path / "cache") as nav:
        described = nav.describe()
        page = nav.messages(None, 0, 2**64)
        snapshot = nav.snapshot(None, MAX_TIME)

    assert (described.message_count, described.start_ns, described.end_ns) == (
        0, None, None)
    assert (page.messages, page.complete, page.continuation) == ((), True, None)
    assert snapshot == {"a": None, "b": None}


@pytest.mark.parametrize("compression", [CompressionType.ZSTD, CompressionType.LZ4])
def test_compressed_chunks_read_the_same_messages(tmp_path, compression):
    messages = irregular_messages(count=500, seed=3)
    source = write_source(tmp_path / "run.mcap", EVENT, THREE, messages,
                          chunk_size=2048, compression=compression)
    records = reference_records(source)
    with prepare_navigation(source, cache_dir=tmp_path / "cache") as nav:
        got = [m for page in all_pages(nav, None, 0, 2**64) for m in page.messages]
        assert got == reference_range(records, THREE, 0, 2**64)
        assert nav.snapshot(None, 1_000_000) == reference_snapshot(
            records, THREE, 1_000_000)


def test_pages_split_on_rows_and_bytes_without_omission_or_duplicate(
        irregular, tmp_path):
    source, records = irregular
    with prepare_navigation(source, cache_dir=tmp_path) as nav:
        pages = all_pages(nav, ["a", "b"], 0, 2**64,
                          PageLimits(max_rows=50, max_bytes=8 * 7))

    assert all(len(page.messages) == 7 for page in pages[:-1])
    got = [m.record_index for page in pages for m in page.messages]
    assert len(got) == len(set(got))
    assert got == [m.record_index
                   for m in reference_range(records, ["a", "b"], 0, 2**64)]


def test_a_row_larger_than_the_page_is_refused_not_skipped(tmp_path):
    blob = {"b.Blob": {"fields": [{"name": "raw", "type": "u8", "count": 64}]}}
    source = write_source(tmp_path / "run.mcap", blob, {"blob": "b.Blob"}, [
        ("blob", 1, {"raw": bytes(64)}), ("blob", 2, {"raw": bytes(64)})])
    with prepare_navigation(source, cache_dir=tmp_path / "cache") as nav:
        with pytest.raises(LimitExceeded, match="record_index 0"):
            nav.messages(None, 0, 10, limits=PageLimits(max_bytes=63))
        assert len(nav.messages(None, 0, 10,
                                limits=PageLimits(max_bytes=64)).messages) == 1


def first_page(nav, channels=None, start_ns=0, end_ns=2**64):
    page = nav.messages(channels, start_ns, end_ns,
                        limits=PageLimits(max_rows=10))
    assert not page.complete
    return page.continuation


@pytest.mark.parametrize("changed", [
    {"channels": ["b"]}, {"channels": None}, {"start_ns": 1},
    {"end_ns": 2**64 - 1},
])
def test_a_continuation_is_refused_for_another_selection_or_interval(
        irregular, tmp_path, changed):
    source, _ = irregular
    request = {"channels": ["a"], "start_ns": 0, "end_ns": 2**64}
    with prepare_navigation(source, cache_dir=tmp_path) as nav:
        token = first_page(nav, **request)
        with pytest.raises(ContinuationError, match="another"):
            nav.messages(**{**request, **changed}, continuation=token)


def test_a_continuation_is_refused_for_another_source(irregular, tmp_path):
    source, _ = irregular
    other = write_source(tmp_path / "other.mcap", EVENT, THREE,
                         irregular_messages(count=100, seed=1))
    with prepare_navigation(source, cache_dir=tmp_path / "c") as nav:
        token = first_page(nav)
    with prepare_navigation(other, cache_dir=tmp_path / "c") as nav:
        with pytest.raises(ContinuationError, match="source_sha256"):
            nav.messages(None, 0, 2**64, token)


@pytest.mark.parametrize("token", [
    "", "not base64!", "e30", "eyJhIjoxfQ", 42, "W10=",
])
def test_a_malformed_continuation_is_refused(irregular, tmp_path, token):
    source, _ = irregular
    with prepare_navigation(source, cache_dir=tmp_path) as nav:
        with pytest.raises(ContinuationError, match="malformed"):
            nav.messages(None, 0, 2**64, token)


def test_a_continuation_with_a_tampered_position_is_refused(irregular, tmp_path):
    import base64
    import json

    source, _ = irregular
    with prepare_navigation(source, cache_dir=tmp_path) as nav:
        body = json.loads(base64.urlsafe_b64decode(first_page(nav, start_ns=5)))
        for after in (["4", "0"], ["5", "-1"], ["05", "1"], [5, 1]):
            token = base64.urlsafe_b64encode(
                json.dumps({**body, "after": after}).encode()).decode()
            with pytest.raises(ContinuationError):
                nav.messages(None, 5, 2**64, token)


@pytest.mark.parametrize("call", [
    lambda nav: nav.messages(["missing"], 0, 1),
    lambda nav: nav.messages([], 0, 1),
    lambda nav: nav.snapshot(["a", "a"], 0),
])
def test_an_unknown_empty_or_repeated_selection_is_refused(tmp_path, call):
    source = write_source(tmp_path / "run.mcap", EVENT, TWO, [])
    with prepare_navigation(source, cache_dir=tmp_path / "cache") as nav:
        with pytest.raises(SelectionError):
            call(nav)


@pytest.mark.parametrize("call", [
    lambda nav: nav.messages(None, 2, 1),
    lambda nav: nav.messages(None, -1, 1),
    lambda nav: nav.messages(None, 0, 2**64 + 1),
    lambda nav: nav.messages(None, 0, 1.0),
    lambda nav: nav.snapshot(None, 2**64),
    lambda nav: nav.snapshot(None, True),
])
def test_an_out_of_range_time_is_refused(tmp_path, call):
    source = write_source(tmp_path / "run.mcap", EVENT, TWO, [])
    with prepare_navigation(source, cache_dir=tmp_path / "cache") as nav:
        with pytest.raises(InvalidRequest):
            call(nav)


def many_chunks(path, count=3000, **options):
    return write_source(path, EVENT, THREE, irregular_messages(count=count),
                        chunk_size=4096, **options)


def assert_nothing_published(cache_dir):
    assert sorted(p.name for p in cache_dir.iterdir()) == []


def test_preparation_reports_progress_and_a_second_preparation_reuses_the_index(
        tmp_path):
    source = many_chunks(tmp_path / "run.mcap")
    seen = []
    with prepare_navigation(source, cache_dir=tmp_path / "c",
                            progress=seen.append) as nav:
        first = nav.describe()
    with prepare_navigation(source, cache_dir=tmp_path / "c") as nav:
        second = nav.describe()

    assert len(seen) > 10
    assert [p.scanned_bytes for p in seen] == sorted(p.scanned_bytes for p in seen)
    assert seen[-1].messages == first.message_count
    assert all(p.total_bytes == source.stat().st_size for p in seen)
    assert (first.index.reused, second.index.reused) == (False, True)
    assert second.index.path == first.index.path
    assert first.index.path.name.startswith(first.source.sha256)


@pytest.mark.parametrize("cancel_after", [1, "last"])
def test_cancelled_preparation_publishes_no_index(tmp_path, cancel_after):
    source = many_chunks(tmp_path / "run.mcap")
    total = len(reference_records(source))
    cancel = threading.Event()
    calls = []

    def progress(state):
        calls.append(state)
        done = state.messages == total  # after the last chunk is indexed
        if cancel_after == len(calls) or (cancel_after == "last" and done):
            cancel.set()

    with pytest.raises(PreparationCancelled):
        prepare_navigation(source, cache_dir=tmp_path / "c",
                           progress=progress, cancel=cancel)
    assert_nothing_published(tmp_path / "c")


def test_a_source_changed_during_preparation_publishes_no_index(tmp_path):
    source = many_chunks(tmp_path / "run.mcap")

    def touch(_state):
        os.utime(source, ns=(1, 1))

    with pytest.raises(SourceError, match="changed"):
        prepare_navigation(source, cache_dir=tmp_path / "c", progress=touch)
    assert_nothing_published(tmp_path / "c")


def test_reads_refuse_a_source_changed_after_preparation(tmp_path):
    source = many_chunks(tmp_path / "run.mcap")
    with prepare_navigation(source, cache_dir=tmp_path / "c") as nav:
        nav.snapshot(None, 10)
        with open(source, "ab") as stream:
            stream.write(b"x")
        with pytest.raises(SourceError, match="changed"):
            nav.snapshot(None, 10)
        with pytest.raises(SourceError, match="changed"):
            nav.messages(None, 0, 10)


def test_an_unreadable_index_is_discarded_and_rebuilt(tmp_path):
    source = many_chunks(tmp_path / "run.mcap")
    with prepare_navigation(source, cache_dir=tmp_path / "c") as nav:
        index = nav.describe().index.path
        expected = nav.snapshot(None, 1_000_000)
    index.write_bytes(b"not a database" * 100)

    with prepare_navigation(source, cache_dir=tmp_path / "c") as nav:
        assert nav.describe().index.reused is False
        assert nav.snapshot(None, 1_000_000) == expected


def test_an_index_of_another_source_is_not_reused(tmp_path):
    source = many_chunks(tmp_path / "run.mcap")
    other = write_source(tmp_path / "other.mcap", EVENT, THREE,
                         irregular_messages(count=200, seed=9))
    with prepare_navigation(other, cache_dir=tmp_path / "o") as nav:
        foreign = nav.describe().index.path
    target = tmp_path / "c" / f"{hashlib.sha256(source.read_bytes()).hexdigest()}.v1.sqlite"
    target.parent.mkdir()
    target.write_bytes(foreign.read_bytes())

    with prepare_navigation(source, cache_dir=tmp_path / "c") as nav:
        assert nav.describe().index.reused is False
        got = [m for page in all_pages(nav, None, 0, 2**64) for m in page.messages]
    assert got == reference_range(reference_records(source), THREE, 0, 2**64)


def test_a_wrong_location_in_a_published_index_is_refused(tmp_path):
    source = many_chunks(tmp_path / "run.mcap")
    with prepare_navigation(source, cache_dir=tmp_path / "c") as nav:
        index = nav.describe().index.path
    with sqlite3.connect(index) as db:
        db.execute("UPDATE messages SET offset = offset + 31")

    with prepare_navigation(source, cache_dir=tmp_path / "c") as nav:
        with pytest.raises(IndexCorrupt):
            nav.snapshot(None, 2**64 - 1)


def test_a_full_index_budget_publishes_no_index(tmp_path):
    source = many_chunks(tmp_path / "run.mcap")
    with pytest.raises(IndexStorageError, match="full"):
        prepare_navigation(source, cache_dir=tmp_path / "c",
                           limits=Limits(max_index_bytes=16 * 4096))
    assert_nothing_published(tmp_path / "c")


def first_chunk_offset(path):
    with open(path, "rb") as stream:
        summary = make_reader(stream).get_summary()
    return min(index.chunk_start_offset for index in summary.chunk_indexes)


def patch_declared_size(path, size):
    """Overwrite the first chunk's declared uncompressed size."""
    with open(path, "r+b") as stream:
        stream.seek(first_chunk_offset(path) + 1 + 8 + 16)
        stream.write(struct.pack("<Q", size))


def test_a_chunk_above_the_decoded_limit_is_refused_before_reading(tmp_path):
    source = many_chunks(tmp_path / "run.mcap")
    with pytest.raises(LimitExceeded, match="decompresses to"):
        prepare_navigation(source, cache_dir=tmp_path / "c",
                           limits=Limits(max_decoded_bytes=2048))
    assert_nothing_published(tmp_path / "c")


def test_a_chunk_whose_stored_bytes_exceed_the_limit_is_refused(tmp_path):
    source = many_chunks(tmp_path / "run.mcap")
    patch_declared_size(source, 100)
    with pytest.raises(LimitExceeded, match="compressed bytes"):
        prepare_navigation(source, cache_dir=tmp_path / "c",
                           limits=Limits(max_decoded_bytes=3000))
    assert_nothing_published(tmp_path / "c")


def test_a_compressed_chunk_larger_than_declared_is_refused(tmp_path):
    zeros = [("a", t, {"value": 0}) for t in range(20_000)]
    source = write_source(tmp_path / "run.mcap", EVENT, TWO, zeros,
                          compression=CompressionType.ZSTD)
    patch_declared_size(source, 1000)
    with pytest.raises(SourceError, match="not the declared 1000"):
        prepare_navigation(source, cache_dir=tmp_path / "c")
    assert_nothing_published(tmp_path / "c")


def test_a_message_above_the_message_limit_is_refused_on_read(tmp_path):
    source = write_source(tmp_path / "run.mcap", EVENT, TWO,
                          [("a", 1, {"value": 1})])
    with prepare_navigation(source, cache_dir=tmp_path / "c",
                            limits=Limits(max_message_bytes=7)) as nav:
        assert nav.snapshot(["b"], 1) == {"b": None}
        with pytest.raises(LimitExceeded, match="record_index 0"):
            nav.snapshot(["a"], 1)
        with pytest.raises(LimitExceeded, match="record_index 0"):
            nav.messages(None, 0, 2)


def test_a_truncated_source_is_refused(tmp_path):
    source = many_chunks(tmp_path / "run.mcap")
    data = source.read_bytes()
    source.write_bytes(data[:len(data) // 2])
    with pytest.raises(SourceError, match="truncated"):
        prepare_navigation(source, cache_dir=tmp_path / "c")


@pytest.mark.parametrize("options", [
    {"max_decoded_bytes": 0}, {"max_message_bytes": -1},
    {"max_index_bytes": 0}, {"chunk_cache_bytes": 1024, "max_decoded_bytes": 2048},
])
def test_invalid_limits_are_refused(options):
    with pytest.raises(InvalidRequest):
        Limits(**options)


def test_a_refused_row_names_its_locator_and_size(tmp_path):
    source = write_source(tmp_path / "run.mcap", EVENT, TWO, [
        ("b", 4, {"value": 0}), ("a", 5, {"value": 1})])
    with prepare_navigation(source, cache_dir=tmp_path / "c",
                            limits=Limits(max_message_bytes=7)) as nav:
        with pytest.raises(LimitExceeded) as on_page:
            nav.messages(["a"], 0, 10)
        with pytest.raises(LimitExceeded) as on_snapshot:
            nav.snapshot(["b"], 9)

    assert (on_page.value.record_index, on_page.value.channel,
            on_page.value.publication_time_ns, on_page.value.size_bytes) == (
        1, "a", 5, 8)
    assert (on_snapshot.value.record_index, on_snapshot.value.channel,
            on_snapshot.value.publication_time_ns) == (0, "b", 4)


def test_a_continuation_after_a_position_resumes_strictly_after_it(tmp_path):
    source = write_source(tmp_path / "run.mcap", EVENT, TWO, [
        ("a", 5, {"value": 0}), ("b", 5, {"value": 1}), ("a", 5, {"value": 2}),
        ("a", 6, {"value": 3}), ("b", 9, {"value": 4})])
    with prepare_navigation(source, cache_dir=tmp_path / "c") as nav:
        mid_burst = nav.continuation_after(None, 5, 10, 5, 1)
        after_instant = nav.continuation_after(["a"], 5, 7, 5, 2**63 - 1)
        resumed = nav.messages(None, 5, 10, mid_burst).messages
        later = nav.messages(["a"], 5, 7, after_instant).messages
        with pytest.raises(ContinuationError, match="outside"):
            nav.continuation_after(None, 5, 10, 10, 0)
        with pytest.raises(InvalidRequest):
            nav.continuation_after(None, 5, 10, 5, -1)

    assert [m.record_index for m in resumed] == [2, 3, 4]
    assert [m.record_index for m in later] == [3]
