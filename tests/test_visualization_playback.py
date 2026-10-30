"""Headless playback control over an indexed Recording (issue #348, spec
#345 s3 "Headless playback", "Seek reconstruction" and resource bounds).

Every test drives time with `FakeClock` and work with `Playback.pump()`;
nothing waits on wall time. Expected deliveries come from a sequential scan
of the source (`reference_records`), never from the index under test.
"""

from __future__ import annotations

import random
import threading

import pytest
from conftest import ROOT
from visualization_fixture import write_qualification_source, write_source

from sil.visualization.navigation import Limits, prepare_navigation
from sil.visualization.playback import (Backfill, Commit, InvalidCommand,
                                        Message, Playback, QueueLimits,
                                        RequestConflict,
                                        StateChanged, default_cache_dir,
                                        open_playback)
from sil.visualization.source import open_source

EVENT = {"e.Event": {"fields": [{"name": "value", "type": "u64"}]}}
MS = 1_000_000
S = 1_000_000_000
BEFORE_START = (S, -1)  # before every Message of the first instant, S


class FakeClock:
    """A monotonic clock in integer nanoseconds that moves only when told."""

    def __init__(self, now: int = 7 * S):
        self.now = now

    def __call__(self) -> int:
        return self.now

    def advance(self, ns: int) -> None:
        self.now += ns


def reference_records(path):
    return list(open_source(path).messages())


def ordered(records, channels, after, until):
    """Messages of `channels` after (time, record_index) `after` and at or
    before time `until`, in (time, record_index) order."""
    return sorted((r for r in records if r.channel in channels
                   and (r.publication_time_ns, r.record_index) > after
                   and r.publication_time_ns <= until),
                  key=lambda r: (r.publication_time_ns, r.record_index))


def latest(records, channels, time_ns):
    """The last Message per Channel at or before `time_ns` (stored order
    breaks no ties: the highest record_index wins), absent without one."""
    found = {}
    for record in records:
        if record.channel in channels and record.publication_time_ns <= time_ns:
            held = found.get(record.channel)
            if held is None or (record.publication_time_ns, record.record_index) > (
                    held.publication_time_ns, held.record_index):
                found[record.channel] = record
    return sorted(found.values(),
                  key=lambda r: (r.publication_time_ns, r.record_index))


def settle(playback, rounds=50):
    """Pump until no reconstruction is pending and no work is due now."""
    for _ in range(rounds):
        playback.pump()
    return playback.status()


def kinds(notifications):
    return [type(n).__name__ for n in notifications]


def records_of(notifications, kind):
    return [n.message for n in notifications if isinstance(n, kind)]


@pytest.fixture
def clock():
    return FakeClock()


class Interposed:
    """A Navigator that runs a test's command once, inside its next
    snapshot: a command that arrives while a reconstruction reads."""

    def __init__(self, nav):
        self._nav = nav
        self.during_snapshot = None

    def __getattr__(self, name):
        return getattr(self._nav, name)

    def snapshot(self, channels, time_ns):
        command, self.during_snapshot = self.during_snapshot, None
        if command is not None:
            command()
        return self._nav.snapshot(channels, time_ns)


def make_playback(tmp_path, messages, clock, *, channels=None, limits=None,
                  queue_limits=None, schemas=EVENT, interpose=False):
    channels = channels or {"a": "e.Event", "b": "e.Event"}
    source = write_source(tmp_path / "run.mcap", schemas, channels, messages)
    nav = prepare_navigation(source, cache_dir=tmp_path / "cache",
                             limits=limits)
    if interpose:
        nav = Interposed(nav)
        return Playback(nav, clock=clock, queue_limits=queue_limits), source, nav
    return Playback(nav, clock=clock, queue_limits=queue_limits), source


def signal(count=21, step=100 * MS):
    """`a` every 100 ms from 1 s, `b` every 300 ms."""
    out = []
    for i in range(count):
        out.append(("a", S + i * step, {"value": i}))
        if i % 3 == 0:
            out.append(("b", S + i * step, {"value": 1000 + i}))
    return out


# --- startup and state ------------------------------------------------------


def test_playback_starts_paused_at_the_first_instant(tmp_path, clock):
    playback, source = make_playback(tmp_path, signal(), clock)

    state = playback.status()

    assert (state.status, state.playing, state.pending) == ("paused", False, False)
    assert (state.start_ns, state.end_ns, state.cursor_ns) == (S, 3 * S, S)
    assert state.rate == 1.0
    assert state.generation == 0
    assert state.source_sha256 == open_source(source).sha256
    assert state.session_id
    assert state.subscription == ()
    assert [c.name for c in playback.description.channels] == ["a", "b"]
    clock.advance(5 * S)
    assert settle(playback).cursor_ns == S
    assert playback.drain() == []


# --- pacing -----------------------------------------------------------------


def play_all(playback):
    playback.set_subscription(["a", "b"])
    settle(playback)
    playback.drain()
    return playback.configure("play", playing=True)


def test_play_advances_by_elapsed_clock_time_times_the_rate(tmp_path, clock):
    playback, source = make_playback(tmp_path, signal(), clock)
    records = reference_records(source)
    play_all(playback)

    clock.advance(250 * MS)
    state = settle(playback)
    first = playback.drain()
    playback.configure("fast", rate=4.0)
    clock.advance(100 * MS)
    second = settle(playback)

    assert (state.status, state.cursor_ns) == ("playing", S + 250 * MS)
    assert records_of(first, Message) == ordered(
        records, "ab", BEFORE_START, S + 250 * MS)
    assert second.cursor_ns == S + 650 * MS
    assert records_of(playback.drain(), Message) == ordered(
        records, "ab", (S + 250 * MS, 2**63), S + 650 * MS)


def test_a_rate_change_between_pumps_keeps_the_elapsed_time_at_the_old_rate(
        tmp_path, clock):
    playback, _ = make_playback(tmp_path, signal(), clock)
    play_all(playback)

    clock.advance(200 * MS)
    playback.configure("half", rate=0.5)  # no pump: 200 ms at rate 1 so far
    clock.advance(200 * MS)

    assert settle(playback).cursor_ns == S + 300 * MS


def test_pause_keeps_the_cursor_and_play_resumes_from_it(tmp_path, clock):
    playback, source = make_playback(tmp_path, signal(), clock)
    records = reference_records(source)
    play_all(playback)
    clock.advance(300 * MS)
    settle(playback)

    paused = playback.configure("pause", playing=False)
    clock.advance(10 * S)
    held = settle(playback)
    playback.configure("resume", playing=True)
    clock.advance(100 * MS)
    resumed = settle(playback)

    assert (paused.status, held.status) == ("paused", "paused")
    assert held.cursor_ns == S + 300 * MS
    assert resumed.cursor_ns == S + 400 * MS
    assert records_of(playback.drain(), Message) == ordered(
        records, "ab", BEFORE_START, S + 400 * MS)


def test_rates_outside_the_range_and_bad_values_leave_state_unchanged(
        tmp_path, clock):
    playback, _ = make_playback(tmp_path, signal(), clock)
    before = playback.status()

    for bad in ({"rate": 0.09}, {"rate": 10.5}, {"rate": float("nan")},
                {"rate": float("inf")}, {"rate": True}, {"rate": "1"},
                {"position_ns": S - 1}, {"position_ns": 3 * S + 1},
                {"position_ns": 1.5e9}, {"playing": 1},
                {"position_ns": 2 * S, "rate": 0}):
        with pytest.raises(InvalidCommand) as refused:
            playback.configure("bad", **bad)
        assert refused.value.state == before, bad
    assert playback.status() == before
    assert playback.configure("edge", rate=0.1).rate == 0.1
    assert playback.configure("edge2", rate=10).rate == 10.0


# --- end, empty and single instant -------------------------------------------


def test_the_end_emits_the_final_messages_then_ended_and_play_restarts(
        tmp_path, clock):
    playback, source = make_playback(tmp_path, signal(), clock)
    records = reference_records(source)
    play_all(playback)

    clock.advance(60 * S)
    ended = settle(playback)
    tail = playback.drain()
    restarted = playback.configure("again", playing=True)
    settle(playback)
    replay = playback.drain()

    assert (ended.status, ended.playing, ended.cursor_ns) == ("ended", False, 3 * S)
    assert records_of(tail, Message) == ordered(records, "ab", BEFORE_START, 3 * S)
    assert isinstance(tail[-1], StateChanged)
    assert tail[-1].state.status == "ended"
    assert restarted.playing and restarted.seek_ns == S
    # The restart begins before the first instant: no Backfill, and every
    # Message at S plays again.
    assert kinds(replay) == ["Reset", "Commit", "Message", "Message"]
    assert replay[0].position_ns == S
    assert replay[1].state.cursor_ns == S
    assert records_of(replay, Message) == ordered(records, "ab", BEFORE_START, S)


def test_every_message_of_the_first_instant_is_played_also_after_a_restart(
        tmp_path, clock):
    first_instant = [("a", S, {"value": 1}), ("b", S, {"value": 2}),
                     ("a", S, {"value": 3}), ("a", S + MS, {"value": 4})]
    playback, source = make_playback(tmp_path, first_instant, clock)
    records = reference_records(source)
    playback.set_subscription(["a", "b"])
    settle(playback)
    reconstructed = playback.drain()
    playback.configure("play", playing=True)
    clock.advance(S)
    settle(playback)
    first = playback.drain()
    playback.configure("again", playing=True)  # after the end: restart
    settle(playback)
    clock.advance(S)
    settle(playback)
    second = playback.drain()

    assert kinds(reconstructed) == ["Commit"]
    assert records_of(first, Message) == records
    assert kinds(second)[:2] == ["Reset", "Commit"]
    assert records_of(second, Message) == records
    assert records_of(second, Backfill) == []


def test_an_explicit_seek_to_the_first_instant_backfills_then_plays_after_it(
        tmp_path, clock):
    first_instant = [("a", S, {"value": 1}), ("a", S, {"value": 3}),
                     ("a", S + MS, {"value": 4})]
    playback, source = make_playback(tmp_path, first_instant, clock)
    records = reference_records(source)
    playback.set_subscription(["a"])
    settle(playback)
    playback.drain()

    playback.configure("seek", position_ns=S, playing=True)
    settle(playback)
    clock.advance(S)
    settle(playback)
    delivered = playback.drain()

    assert records_of(delivered, Backfill) == [records[1]]
    assert records_of(delivered, Message) == [records[2]]


def test_an_empty_source_has_no_range_and_play_ends_at_once(tmp_path, clock):
    playback, _ = make_playback(tmp_path, [], clock)

    state = playback.status()
    with pytest.raises(InvalidCommand, match="no Messages"):
        playback.configure("seek", position_ns=0)
    playback.set_subscription(["a"])
    settled = settle(playback)
    played = playback.configure("play", playing=True)

    assert (state.start_ns, state.end_ns, state.cursor_ns) == (None, None, None)
    assert (settled.status, settled.pending) == ("paused", False)
    assert (played.status, played.playing) == ("ended", False)
    assert records_of(playback.drain(), Backfill) == []


def test_a_single_instant_source_plays_every_message_and_ends(tmp_path, clock):
    playback, source = make_playback(tmp_path, [
        ("a", 5 * S, {"value": 1}), ("a", 5 * S, {"value": 2}),
        ("b", 5 * S, {"value": 3})], clock)
    records = reference_records(source)

    playback.set_subscription(["a", "b"])
    settle(playback)
    backfill = playback.drain()
    playback.configure("play", playing=True)
    ended = settle(playback)

    assert kinds(backfill) == ["Commit"]  # nothing precedes the first instant
    assert (ended.status, ended.cursor_ns) == ("ended", 5 * S)
    assert records_of(playback.drain(), Message) == records


# --- seek -------------------------------------------------------------------


def test_a_combined_seek_and_play_commits_backfill_then_plays_after_it(
        tmp_path, clock):
    playback, source = make_playback(tmp_path, signal(), clock)
    records = reference_records(source)
    play_all(playback)
    playback.configure("pause", playing=False)

    accepted = playback.configure("seek-7", position_ns=S + 450 * MS,
                                  playing=True)
    clock.advance(10 * S)  # time spent pending does not count
    playback.pump()
    committed = playback.drain()
    clock.advance(200 * MS)
    playing = settle(playback)

    assert (accepted.status, accepted.pending, accepted.seek_ns) == (
        "buffering", True, S + 450 * MS)
    assert accepted.cursor_ns == S and accepted.request_id == "seek-7"
    assert accepted.generation > 0
    assert kinds(committed) == ["Reset", "Backfill", "Backfill", "Commit"]
    assert committed[0].position_ns == S + 450 * MS
    assert records_of(committed, Backfill) == latest(records, "ab", S + 450 * MS)
    state = committed[-1].state
    assert (state.status, state.pending, state.cursor_ns, state.seek_ns) == (
        "playing", False, S + 450 * MS, None)
    assert state.request_id == "seek-7"
    assert playing.cursor_ns == S + 650 * MS
    assert records_of(playback.drain(), Message) == ordered(
        records, "ab", (S + 450 * MS, 2**63), S + 650 * MS)


def test_a_backward_seek_flushes_queued_data_and_backfills_no_future_value(
        tmp_path, clock):
    playback, source = make_playback(tmp_path, signal(), clock)
    records = reference_records(source)
    play_all(playback)
    clock.advance(1500 * MS)
    settle(playback)  # Messages up to 2.5 s queued, not drained

    playback.configure("back", position_ns=S + 150 * MS)
    settle(playback)
    after_seek = playback.drain()

    assert kinds(after_seek) == ["Reset", "Backfill", "Backfill", "Commit"]
    assert records_of(after_seek, Backfill) == latest(records, "ab", S + 150 * MS)
    assert all(r.publication_time_ns <= S + 150 * MS
               for r in records_of(after_seek, Backfill))


def test_a_seek_before_a_sparse_channels_first_message_leaves_it_absent(
        tmp_path, clock):
    playback, source = make_playback(tmp_path, [
        ("a", S, {"value": 1}), ("b", 2 * S, {"value": 2}),
        ("a", 3 * S, {"value": 3})], clock)
    playback.set_subscription(["a", "b"])
    playback.configure("late", position_ns=3 * S)
    settle(playback)
    playback.drain()

    playback.configure("early", position_ns=2 * S - 1)
    settle(playback)
    backfill = records_of(playback.drain(), Backfill)

    assert [(r.channel, r.publication_time_ns) for r in backfill] == [("a", S)]


def test_rapid_seeks_replace_the_pending_one_and_stale_reads_are_dropped(
        tmp_path, clock):
    playback, source, nav = make_playback(tmp_path, signal(), clock,
                                          interpose=True)
    records = reference_records(source)
    playback.set_subscription(["a", "b"])
    settle(playback)
    playback.drain()

    playback.configure("s1", position_ns=2 * S)

    def newer_seeks():
        playback.configure("s2", position_ns=S + 500 * MS)
        playback.configure("s3", position_ns=S + 100 * MS)
    nav.during_snapshot = newer_seeks
    playback.pump()  # reconstructs s1; s2 and s3 arrive meanwhile
    stale = playback.drain()
    playback.pump()
    fresh = playback.drain()

    assert stale == []
    assert kinds(fresh) == ["Reset", "Backfill", "Backfill", "Commit"]
    assert fresh[0].position_ns == S + 100 * MS
    assert records_of(fresh, Backfill) == latest(records, "ab", S + 100 * MS)
    assert fresh[-1].state.request_id == "s3"
    assert settle(playback).cursor_ns == S + 100 * MS
    assert playback.drain() == []


def test_pause_while_a_seek_is_pending_cancels_the_automatic_resume(
        tmp_path, clock):
    playback, _ = make_playback(tmp_path, signal(), clock)
    playback.set_subscription(["a"])

    playback.configure("seek", position_ns=2 * S, playing=True)
    playback.configure("pause", playing=False)
    clock.advance(S)
    state = settle(playback)

    assert (state.status, state.cursor_ns) == ("paused", 2 * S)
    assert records_of(playback.drain(), Message) == []


def test_close_while_a_seek_is_pending_ends_the_session(tmp_path, clock):
    playback, _, nav = make_playback(tmp_path, signal(), clock,
                                     interpose=True)
    playback.set_subscription(["a"])
    playback.configure("seek", position_ns=2 * S, playing=True)

    nav.during_snapshot = playback.close
    playback.pump()
    closed = playback.status()

    assert closed.status == "closed"
    assert kinds(playback.drain()) == ["StateChanged"]
    with pytest.raises(InvalidCommand, match="closed"):
        playback.configure("again", playing=True)
    playback.pump()
    assert playback.drain() == []


# --- subscriptions ----------------------------------------------------------


def test_a_new_subscription_is_reconstructed_at_the_cursor_without_future_data(
        tmp_path, clock):
    playback, source = make_playback(tmp_path, signal(), clock)
    records = reference_records(source)
    playback.set_subscription(["a"])
    settle(playback)
    playback.configure("play", playing=True)
    clock.advance(250 * MS)
    settle(playback)
    queued_a = playback.drain()

    changed = playback.set_subscription(["a", "b"])
    clock.advance(S)  # advancement is suspended until the commit
    playback.pump()
    extended = playback.drain()
    clock.advance(100 * MS)
    resumed = settle(playback)

    assert records_of(queued_a, Message) == ordered(
        records, "a", BEFORE_START, S + 250 * MS)
    assert (changed.status, changed.pending, changed.playing) == (
        "buffering", True, True)
    assert kinds(extended) == ["Backfill", "Commit"]  # no Reset: history stays
    assert records_of(extended, Backfill) == latest(records, "b", S + 250 * MS)
    assert extended[-1].state.status == "playing"
    assert resumed.cursor_ns == S + 350 * MS
    assert records_of(playback.drain(), Message) == ordered(
        records, "ab", (S + 250 * MS, 2**63), S + 350 * MS)


def test_unsubscribing_drops_queued_messages_of_that_channel(tmp_path, clock):
    playback, source = make_playback(tmp_path, signal(), clock)
    records = reference_records(source)
    play_all(playback)
    clock.advance(500 * MS)
    settle(playback)

    state = playback.set_subscription(["a"])

    assert not state.pending
    assert records_of(playback.drain(), Message) == ordered(
        records, "a", BEFORE_START, S + 500 * MS)


def test_time_spent_buffering_is_not_played_after_a_subscription_change(
        tmp_path, clock):
    playback, _ = make_playback(tmp_path, signal(), clock,
                                queue_limits=QueueLimits(max_messages=2))
    play_all(playback)
    clock.advance(400 * MS)
    playback.pump()
    playback.pump()
    stalled = playback.status()
    clock.advance(10 * S)
    playback.drain()

    playback.set_subscription(["a"])
    resumed = settle(playback)

    assert stalled.status == "buffering"
    assert resumed.cursor_ns == stalled.cursor_ns


def test_unknown_or_repeated_channels_are_refused(tmp_path, clock):
    playback, _ = make_playback(tmp_path, signal(), clock)
    before = playback.status()
    for bad in (["c"], ["a", "a"]):
        with pytest.raises(InvalidCommand) as refused:
            playback.set_subscription(bad)
        assert refused.value.state == before


def burst_messages():
    """Equal-time Bursts across both Channels, stored times that decrease
    and a gap."""
    out = []
    for i in range(40):
        t = S + (i // 4) * 10 * MS
        out.append(("ab"[i % 2], t, {"value": i}))
    out += [("a", S + 5 * MS, {"value": 100}), ("b", S, {"value": 101}),
            ("a", 2 * S, {"value": 102})]
    return out


def test_a_subscription_added_inside_a_burst_backfills_only_what_precedes(
        tmp_path, clock):
    playback, source = make_playback(
        tmp_path, burst_messages(), clock,
        queue_limits=QueueLimits(max_messages=3))
    records = reference_records(source)
    playback.set_subscription(["a"])
    settle(playback)
    playback.configure("play", playing=True)
    clock.advance(30 * MS)
    playback.pump()  # three of the a-Messages at 1.000 s and 1.005 s
    queued = playback.drain()
    position = (queued[-1].message.publication_time_ns,
                queued[-1].message.record_index)

    playback.set_subscription(["a", "b"])
    playback.pump()
    extended = playback.drain()
    clock.advance(S)
    delivered = []
    for _ in range(100):
        playback.pump()
        delivered += playback.drain()

    expected_backfill = [r for r in latest(
        [r for r in records if (r.publication_time_ns, r.record_index) <= position],
        "b", position[0])]
    assert records_of(extended, Backfill) == expected_backfill
    assert records_of(delivered, Message) == ordered(
        [r for r in records if r.channel == "b" or (
            r.publication_time_ns, r.record_index) > position],
        "ab", position, 2 * S)


def test_continuous_playback_delivers_every_message_in_order_through_a_small_queue(
        tmp_path, clock):
    playback, source = make_playback(
        tmp_path, burst_messages(), clock,
        queue_limits=QueueLimits(max_messages=4, max_bytes=24))
    records = reference_records(source)
    play_all(playback)
    playback.configure("fast", rate=10.0)

    delivered, statuses, peak = [], set(), 0
    for step in range(400):
        clock.advance(MS)
        playback.pump()
        statuses.add(playback.status().status)
        if step % 3 == 0:  # a slow consumer
            batch = playback.drain(max_items=2)
            peak = max(peak, len(records_of(batch, Message)))
            delivered += batch
        held = playback.drain(max_items=0)
        assert held == []
    delivered += playback.drain()

    messages = records_of(delivered, Message)
    assert messages == ordered(records, "ab", BEFORE_START, 2 * S)
    assert "buffering" in statuses
    assert playback.status().status == "ended"


def test_the_queue_never_holds_more_than_its_bounds(tmp_path, clock):
    playback, _ = make_playback(
        tmp_path, burst_messages(), clock,
        queue_limits=QueueLimits(max_messages=5, max_bytes=24))
    play_all(playback)
    clock.advance(10 * S)

    for _ in range(20):
        playback.pump()
    queued = playback.drain()

    assert len(records_of(queued, Message)) == 3  # 24 bytes of 8-byte payloads
    assert playback.status().status == "buffering"


BLOB = {"e.Event": EVENT["e.Event"],
        "b.Blob": {"fields": [{"name": "raw", "type": "u8", "count": 64}]}}


def test_an_oversize_message_is_refused_in_its_place_and_playback_continues(
        tmp_path, clock):
    playback, source = make_playback(tmp_path, [
        ("a", S, {"value": 1}), ("blob", S, {"raw": bytes(64)}),
        ("a", S + MS, {"value": 2}), ("blob", S + 2 * MS, {"raw": bytes(64)}),
        ("a", S + 3 * MS, {"value": 3})], clock,
        channels={"a": "e.Event", "blob": "b.Blob"}, schemas=BLOB,
        limits=Limits(max_message_bytes=32))
    playback.set_subscription(["a", "blob"])
    settle(playback)
    playback.drain()
    playback.configure("seek", position_ns=S)  # backfills the first instant
    settle(playback)
    backfill = playback.drain()
    playback.configure("play", playing=True)
    clock.advance(S)
    played = []
    for _ in range(10):
        playback.pump()
        played += [n for n in playback.drain() if not isinstance(n, StateChanged)]
    state = playback.status()

    assert kinds(backfill) == ["Reset", "Backfill", "Refused", "Commit"]
    refused = backfill[2]
    assert (refused.channel, refused.record_index, refused.size_bytes,
            refused.backfill) == ("blob", 1, 64, True)
    assert kinds(played) == ["Message", "Refused", "Message"]
    assert (played[0].message.record_index, played[1].record_index,
            played[2].message.record_index) == (2, 3, 4)
    assert state.status == "ended"


def test_a_message_larger_than_the_queue_is_refused_not_skipped(tmp_path, clock):
    playback, _ = make_playback(tmp_path, [
        ("a", S, {"value": 1}), ("blob", S + MS, {"raw": bytes(64)}),
        ("a", S + 2 * MS, {"value": 2})], clock,
        channels={"a": "e.Event", "blob": "b.Blob"}, schemas=BLOB,
        queue_limits=QueueLimits(max_bytes=32))
    play_all_channels = ["a", "blob"]
    playback.set_subscription(play_all_channels)
    settle(playback)
    playback.drain()
    playback.configure("play", playing=True)
    clock.advance(S)
    played = []
    for _ in range(10):  # the Refused waits until the queue drained
        playback.pump()
        played += [n for n in playback.drain() if not isinstance(n, StateChanged)]

    assert kinds(played) == ["Message", "Refused", "Message"]
    assert played[1].record_index == 1 and not played[1].backfill
    assert playback.status().status == "ended"


# --- request IDs --------------------------------------------------------------


def test_a_repeated_request_returns_its_cached_result_without_acting_again(
        tmp_path, clock):
    playback, _ = make_playback(tmp_path, signal(), clock)
    playback.set_subscription(["a"])
    settle(playback)
    playback.drain()

    first = playback.configure("seek-1", position_ns=2 * S)
    settle(playback)
    playback.drain()
    repeated = playback.configure("seek-1", position_ns=2 * S)

    assert repeated == first
    assert playback.status().generation == first.generation
    assert playback.drain() == []  # no second reconstruction


def test_changed_content_under_a_cached_request_id_is_refused(tmp_path, clock):
    playback, _ = make_playback(tmp_path, signal(), clock)
    playback.configure("r", rate=2.0)
    before = playback.status()

    with pytest.raises(RequestConflict) as refused:
        playback.configure("r", rate=3.0)

    assert refused.value.state == before
    assert playback.status().rate == 2.0


def test_request_ids_expire_after_256_newer_ones(tmp_path, clock):
    playback, _ = make_playback(tmp_path, signal(), clock)
    playback.configure("old", rate=2.0)
    for i in range(255):
        playback.configure(f"r{i}")
    with pytest.raises(RequestConflict):
        playback.configure("old", rate=3.0)  # still cached

    playback.configure("r255")
    reused = playback.configure("old", rate=3.0)  # expired: a new request

    assert reused.rate == 3.0


def test_refused_requests_are_not_cached(tmp_path, clock):
    playback, _ = make_playback(tmp_path, signal(), clock)
    with pytest.raises(InvalidCommand):
        playback.configure("r", rate=20.0)
    with pytest.raises(InvalidCommand, match="request_id"):
        playback.configure(7, rate=2.0)

    assert playback.configure("r", rate=2.0).rate == 2.0


# --- disconnect, failure and the worker loop --------------------------------------


def test_disconnect_pauses_and_a_reconnect_gets_a_fresh_reconstruction(
        tmp_path, clock):
    playback, source = make_playback(tmp_path, signal(), clock)
    records = reference_records(source)
    play_all(playback)
    playback.configure("r1", rate=2.0)
    clock.advance(400 * MS)
    settle(playback)  # Messages queued for the client that goes away

    gone = playback.disconnect()
    clock.advance(S)
    idle = settle(playback)
    playback.set_subscription(["b"])
    playback.pump()
    fresh = playback.drain()
    reused = playback.configure("r1", rate=3.0)  # the cache is cleared

    assert (gone.status, gone.playing, gone.subscription) == ("paused", False, ())
    assert idle.cursor_ns == S + 800 * MS
    assert kinds(fresh) == ["Reset", "Backfill", "Commit"]
    assert fresh[0].position_ns == S + 800 * MS
    assert records_of(fresh, Backfill) == latest(records, "b", S + 800 * MS)
    assert fresh[-1].state.status == "paused"
    assert reused.rate == 3.0


def test_a_seek_pending_at_disconnect_is_where_the_reconnect_starts(
        tmp_path, clock):
    playback, _ = make_playback(tmp_path, signal(), clock)
    playback.set_subscription(["a"])
    playback.configure("seek", position_ns=2 * S)

    playback.disconnect()
    playback.set_subscription(["a"])
    settle(playback)
    fresh = playback.drain()

    assert kinds(fresh) == ["Reset", "Backfill", "Commit"]
    assert fresh[-1].state.cursor_ns == 2 * S


def test_a_source_that_changes_during_playback_fails_the_session(tmp_path, clock):
    playback, source = make_playback(tmp_path, signal(), clock)
    play_all(playback)
    with open(source, "ab") as stream:
        stream.write(b"x")
    clock.advance(S)

    playback.pump()
    failed = playback.status()

    assert (failed.status, failed.playing) == ("failed", False)
    assert "changed" in failed.diagnostic
    notifications = playback.drain()
    assert isinstance(notifications[-1], StateChanged)
    assert notifications[-1].state.status == "failed"
    with pytest.raises(InvalidCommand, match="failed"):
        playback.configure("play", playing=True)
    playback.close()
    assert playback.status().status == "closed"


def test_run_works_until_close_on_its_own_thread(tmp_path, clock):
    playback, source = make_playback(tmp_path, signal(), clock)
    records = reference_records(source)
    worker = threading.Thread(target=playback.run, daemon=True)
    worker.start()

    playback.set_subscription(["a", "b"])
    received = []
    while not any(isinstance(n, Commit) for n in received):
        received += playback.drain(timeout=10)
    playback.close()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert records_of(received, Backfill) == []  # nothing precedes the start
    assert received[-1].state.cursor_ns == records[0].publication_time_ns


# --- random seeks against the sequential reference --------------------------------


def test_random_seeks_and_plays_match_the_sequential_reference(tmp_path, clock):
    rng = random.Random(348)
    playback, source = make_playback(tmp_path, burst_messages(), clock)
    records = reference_records(source)
    playback.set_subscription(["a", "b"])
    settle(playback)
    playback.drain()

    for _ in range(60):
        position = rng.randrange(S, 2 * S + 1)
        playback.configure(None, position_ns=position, playing=False)
        settle(playback)
        committed = playback.drain()
        assert records_of(committed, Backfill) == latest(records, "ab", position)
        playback.configure(None, playing=True, rate=rng.choice([0.5, 1.0, 7.0]))
        step = rng.randrange(0, 50 * MS)
        clock.advance(step)
        cursor = settle(playback).cursor_ns
        assert records_of(playback.drain(), Message) == ordered(
            records, "ab", (position, 2**63), cursor)


# --- opening a Recording ----------------------------------------------------


def test_open_playback_keeps_its_index_in_the_user_cache_by_default(
        tmp_path, clock, monkeypatch):
    source = write_source(tmp_path / "run.mcap", EVENT, {"a": "e.Event"},
                          [("a", S, {"value": 1})])
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))

    with open_playback(source, clock=clock) as playback:
        state = playback.status()

    assert default_cache_dir() == tmp_path / "xdg" / "sil" / "navigation"
    assert list(default_cache_dir().glob(f"{state.source_sha256}.*.sqlite"))


def test_without_xdg_cache_home_the_cache_is_under_the_home_directory(
        tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert default_cache_dir() == tmp_path / ".cache" / "sil" / "navigation"


def test_the_documented_example_runs_without_a_viewer(
        tmp_path, monkeypatch, capsys):
    section = (ROOT / "docs" / "visualization.md").read_text().partition(
        "## Play a Recording from Python")[2]
    example = section.partition("```python\n")[2].partition("```")[0]
    write_qualification_source(tmp_path / "run.mcap")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    exec(compile(example, "visualization.md", "exec"), {})

    lines = capsys.readouterr().out.splitlines()
    assert lines[-1] == "cursor 2000000000"
    assert "sparse 5000000000" not in lines  # no future value
    assert "vehicle/speed 2000000000" in lines
