"""The replay policies a SiL adapter must reproduce, without loading the library."""
import pytest
from libsafety_workload import (TRANSMIT_ECHO_SOURCE, first_divergence, received,
                                ticks, timer_us)


def test_timer_is_the_upstream_microsecond_counter():
    assert timer_us(1_234_567_999) == 1_234_567
    assert timer_us(0xFFFFFFFF * 1000) == 0


def test_safety_tick_skips_one_second_at_both_ends():
    first, last = 10_000_000_000, 70_000_000_000
    assert not ticks(first + 1_000_000_000, first, last)
    assert ticks(first + 1_000_000_001, first, last)
    assert not ticks(last - 1_000_000_000, first, last)


def test_transmit_echoes_are_not_received():
    frames = [(0x260, 0, b"a"), (0x260, TRANSMIT_ECHO_SOURCE, b"b"), (0x2E4, 130, b"c")]
    assert received(frames) == [(0x260, 0, b"a")]


def row(t, controls):
    return {"t_ns": t, "accepted": 3, "rejected": 0, "controls_allowed": controls}


def test_identical_traces_have_no_divergence():
    trace = [row(0, True), row(10, True)]
    assert first_divergence(trace, list(trace)) is None


def test_first_divergence_names_instant_field_and_values():
    divergence = first_divergence([row(0, True), row(10, True)], [row(0, True), row(10, False)])
    assert divergence == {"index": 1, "t_ns": 10, "field": "controls_allowed",
                          "expected": True, "actual": False}


@pytest.mark.parametrize("candidate", [[row(0, True)], [row(0, True), row(10, True), row(20, True)]])
def test_missing_or_extra_final_rows_diverge(candidate):
    divergence = first_divergence([row(0, True), row(10, True)], candidate)
    assert divergence["field"] == "coverage"
