"""The adapter's Step behavior against a recording stand-in for the library.

The stand-in records every call the adapter makes, so each test states the
upstream replay policy in the order the library sees it.
"""
import pytest
from adapter import EventPolicy, LibsafetyParticipant

from sil.participant import Input, ParticipantFailure

PERIOD_NS = 1_000_000
ORIGIN_NS = 18_054_876_797_669
STATE = {"controls_allowed": True, "gas_pressed_prev": False,
         "brake_pressed_prev": False, "cruise_engaged_prev": True,
         "vehicle_moving": True, "acc_main_on": False,
         "vehicle_speed_min": 22.5, "vehicle_speed_max": 23.25}


class RecordingLibrary:
    def __init__(self, rejected=()):
        self.calls = []
        self._rejected = set(rejected)

    def set_timer(self, microseconds):
        self.calls.append(("set_timer", microseconds))

    def tick(self):
        self.calls.append(("tick",))

    def forward(self, bus, address):
        self.calls.append(("forward", bus, address))

    def receive(self, address, bus, data):
        self.calls.append(("receive", address, bus, data))
        return address not in self._rejected

    def state(self):
        return dict(STATE)


def participant(library, timer_unit_ns=1000):
    policy = EventPolicy(timer_origin_ns=ORIGIN_NS, timer_unit_ns=timer_unit_ns,
                         first_event_ns=0, last_event_ns=60_000_000_000)
    adapter = LibsafetyParticipant(
        bind=lambda: library, input_channel="can.rx",
        output_channel="libsafety.state", period_ns=PERIOD_NS, policy=policy)
    adapter.bind_library()
    return adapter


def frame(publish_ns, address, src, payload):
    data = {"address": address, "src": src, "length": len(payload)}
    data.update({f"d{i}": b for i, b in enumerate(payload.ljust(8, b"\x00"))})
    return Input("can.rx", publish_ns, data)


def test_a_burst_is_timed_by_its_own_instant_and_observed_once():
    library = RecordingLibrary(rejected={0x2C1})
    burst = [frame(9_763_346, 0x260, 0, b"\x01\x02"),
             frame(9_763_346, 0x2C1, 5, b"\x03")]

    out = participant(library).on_step(10_000_000, PERIOD_NS, burst)

    assert library.calls == [
        # 18_054_886_561 us, less 4 * 0xFFFFFFFF
        ("set_timer", 875_017_381),
        ("forward", 0, 0x260), ("receive", 0x260, 0, b"\x01\x02"),
        ("forward", 5, 0x2C1), ("receive", 0x2C1, 1, b"\x03"),
    ]
    assert out == [("libsafety.state",
                    {"event_ns": 9_763_346, "accepted": 1, "rejected": 1,
                     **STATE})]


def test_a_step_without_a_burst_calls_nothing_and_publishes_nothing():
    library = RecordingLibrary()
    assert participant(library).on_step(0, PERIOD_NS, []) == []
    assert library.calls == []


@pytest.mark.parametrize("event_ns, ticks", [
    (1_000_000_000, False), (1_000_000_001, True),
    (58_999_999_999, True), (59_000_000_000, False)])
def test_safety_tick_runs_only_more_than_one_second_from_both_ends(event_ns, ticks):
    library = RecordingLibrary()
    participant(library).on_step(event_ns, PERIOD_NS,
                                 [frame(event_ns, 0x260, 0, b"\x00")])
    assert (("tick",) in library.calls) is ticks
    assert library.calls.index(("forward", 0, 0x260)) > 0


def test_the_timer_unit_scales_the_counter_and_keeps_the_upstream_modulus():
    library = RecordingLibrary()
    participant(library, timer_unit_ns=1).on_step(
        0, PERIOD_NS, [frame(0, 0x260, 0, b"\x00")])
    assert library.calls[0] == ("set_timer", ORIGIN_NS % 0xFFFFFFFF)


def test_two_bursts_in_one_step_fail_the_run():
    with pytest.raises(ParticipantFailure, match="two Bursts"):
        participant(RecordingLibrary()).on_step(
            10_000_000, PERIOD_NS,
            [frame(9_000_000, 0x260, 0, b"\x00"),
             frame(9_500_000, 0x260, 0, b"\x00")])


def test_a_step_at_another_period_fails_the_run():
    with pytest.raises(ParticipantFailure, match="period"):
        participant(RecordingLibrary()).on_step(0, 2 * PERIOD_NS, [])
