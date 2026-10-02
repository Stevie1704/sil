"""The order in which the computation timing replays the nominal Run."""
import struct

import pytest
from native_acceptance import cost_records


def frame(event_ns: int, tag: int) -> bytes:
    """A can.TimedFrame stand-in: its event_ns, then one distinguishing byte."""
    return struct.pack("<QB", event_ns, tag)


def test_the_candidates_of_an_event_follow_its_frames():
    frames = [frame(0, 1), frame(0, 2), frame(10, 3), frame(20, 4)]
    transmit = [frame(0, 5), frame(20, 6), frame(20, 7)]
    assert cost_records(frames, transmit) == [
        b"\x00" + frame(0, 1), b"\x00" + frame(0, 2), b"\x01" + frame(0, 5),
        b"\x00" + frame(10, 3),
        b"\x00" + frame(20, 4), b"\x01" + frame(20, 6), b"\x01" + frame(20, 7)]


def test_a_candidate_without_an_event_at_its_instant_is_refused():
    with pytest.raises(RuntimeError, match="1 candidate instants have no event"):
        cost_records([frame(0, 1)], [frame(5, 2)])
