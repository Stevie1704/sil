"""The libsafety replay's Manifest and its failing controls (issue #193).

A Replay participant publishes the windowed frame Recording on `can.rx`.
One Process participant runs `adapter.py` with the pinned `libsafety.so` and
publishes one observation per Burst on `libsafety.state`. Everything that
decides the Run is explicit here:

- Step period 1 ms, shorter than the shortest recorded event interval
  (8.75 ms), so each Step holds at most one Burst;
- `can.rx` Latency 0: a Burst is processed in the first Step at or after the
  instant it names, never a Step later;
- `libsafety.state` Latency 0; it has no subscriber and is only recorded;
- the route capacity of `can.rx` is the largest recorded Burst: a Burst
  waits less than one Step and the next one is at least 8.75 ms later;
- the initial state is the recorded carParams contract, the timer origin is
  the recording's first `logMonoTime` and its unit is 1 us;
- the Duration runs the Step that observes the last event.

Each control changes exactly one of these:

| Control | Change | Must fail because |
| --- | --- | --- |
| `input-one-step-late` | `can.rx` Latency one Step | no observation is at its Slot |
| `timer-in-ns` | timer unit 1 ns | the state diverges (#178: event 100) |
| `wrong-param` | param 73 + the alternative-brake flag | the state diverges |
| `crash` | SIGSEGV at event 100 | the library process dies |
| `hang` | no return at event 100 | the response deadline is missed |

`native_manifest` declares the Native form (#232) of the same Run. Two
Replay participants publish the frames on `can.rx` and the transmit
candidates on `can.tx`, both as `can.TimedFrame`, with Latency 0. One Native
participant, `native_adapter.c` built as a shared library, loads the pinned
`libsafety.so` from its config and publishes `libsafety.state` as
`libsafety.NativeState`. Its Task period is the same 1 ms. Its controls:

| Control | Change | Must fail because |
| --- | --- | --- |
| `timer-in-ns` | timer unit 1 ns | the state diverges (#178: event 100) |
| `stock-longitudinal` | param 73 + the stock-longitudinal flag | ACC_CONTROL candidates are rejected |
| `second-instance` | a second participant of the adapter | a Manifest error: the library's globals allow one instance |

The crash and hang controls are adapter builds of their own; they run as
sealed bundles under `sil bundle matrix`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from workload import (
    FRAME_CHANNEL,
    FRAME_SCHEMA,
    NATIVE_STATE_SCHEMA,
    SCHEMAS,
    STATE_CHANNEL,
    STATE_SCHEMA,
    STEP_PERIOD_NS,
    TIMED_FRAME_SCHEMA,
    TRANSMIT_CHANNEL,
    duration_ns,
)

from sil.manifest import Manifest, SubscriberRoute

HERE = Path(__file__).resolve().parent
TIMER_UNIT_NS = 1000
FAILURE_EVENT = 100
# opendbc's TOYOTA_PARAM_ALT_BRAKE: read the brake from another message.
TOYOTA_ALT_BRAKE = 1 << 8
# opendbc's TOYOTA_PARAM_STOCK_LONGITUDINAL: only cancel requests on 0x343.
TOYOTA_STOCK_LONGITUDINAL = 2 << 8

CONTROLS = {
    "input-one-step-late": {"frame_latency_ns": STEP_PERIOD_NS},
    "timer-in-ns": {"timer_unit_ns": 1},
    "wrong-param": {"param_flags": TOYOTA_ALT_BRAKE},
    "crash": {"failure": "crash"},
    "hang": {"failure": "hang"},
}


NATIVE_CONTROLS = {
    "timer-in-ns": {"timer_unit_ns": 1},
    "stock-longitudinal": {"param_flags": TOYOTA_STOCK_LONGITUDINAL},
    "second-instance": {"instances": 2},
}


@dataclass(frozen=True)
class Segment:
    """What the Manifest takes from the recording and its carParams."""

    first_log_mono_ns: int
    last_event_ns: int
    mode: int
    param: int
    alternative_experience: int
    largest_burst: int
    largest_transmit_burst: int = 0


def replay_manifest(recording: Path, library: Path, segment: Segment, *,
                    frame_latency_ns: int = 0,
                    timer_unit_ns: int = TIMER_UNIT_NS,
                    param_flags: int = 0,
                    failure: str | None = None) -> Manifest:
    m = Manifest(duration_ns=duration_ns(segment.last_event_ns))
    m.add_schemas({name: SCHEMAS[name] for name in (FRAME_SCHEMA, STATE_SCHEMA)})
    m.add_channel(FRAME_CHANNEL, schema=FRAME_SCHEMA, latency_ns=frame_latency_ns)
    m.add_channel(STATE_CHANNEL, schema=STATE_SCHEMA, latency_ns=0)
    m.add_replay("replay", recording=Path(recording).resolve(),
                 channels=[FRAME_CHANNEL])
    adapter = [
        str(Path(library).resolve()),
        "--input", FRAME_CHANNEL, "--output", STATE_CHANNEL,
        "--period-ns", str(STEP_PERIOD_NS),
        "--mode", str(segment.mode), "--param", str(segment.param | param_flags),
        "--alternative-experience", str(segment.alternative_experience),
        "--timer-origin-ns", str(segment.first_log_mono_ns),
        "--timer-unit-ns", str(timer_unit_ns),
        "--first-event-ns", "0",
        "--last-event-ns", str(segment.last_event_ns),
    ]
    if failure is None:
        command = ["python3", str(HERE / "adapter.py"), *adapter]
    else:
        command = ["python3", str(HERE / "library_failures.py"),
                   "--failure", failure, "--at-event", str(FAILURE_EVENT),
                   *adapter]
    m.add_process(
        "libsafety", command=command, step_period_ns=STEP_PERIOD_NS,
        subscribes=[SubscriberRoute(FRAME_CHANNEL,
                                    capacity=segment.largest_burst)],
        publishes=[STATE_CHANNEL])
    return m


def native_manifest(frames: Path, transmit: Path, adapter: Path, library: Path,
                    segment: Segment, *, timer_unit_ns: int = TIMER_UNIT_NS,
                    param_flags: int = 0, instances: int = 1) -> Manifest:
    """The Native form: `adapter` is the built `native_adapter.c`."""
    m = Manifest(duration_ns=duration_ns(segment.last_event_ns))
    m.add_schemas({name: SCHEMAS[name]
                   for name in (TIMED_FRAME_SCHEMA, NATIVE_STATE_SCHEMA)})
    m.add_channel(FRAME_CHANNEL, schema=TIMED_FRAME_SCHEMA, latency_ns=0)
    m.add_channel(TRANSMIT_CHANNEL, schema=TIMED_FRAME_SCHEMA, latency_ns=0)
    m.add_replay("replay", recording=Path(frames).resolve(),
                 channels=[FRAME_CHANNEL])
    m.add_replay("transmit", recording=Path(transmit).resolve(),
                 channels=[TRANSMIT_CHANNEL])
    for index in range(instances):
        name = "libsafety" if index == 0 else f"libsafety-{index + 1}"
        state = STATE_CHANNEL if index == 0 else f"{STATE_CHANNEL}.{index + 1}"
        m.add_channel(state, schema=NATIVE_STATE_SCHEMA, latency_ns=0)
        m.add_native(
            name, library=str(Path(adapter).resolve()),
            config={
                "library": str(Path(library).resolve()),
                "frames": FRAME_CHANNEL, "transmit": TRANSMIT_CHANNEL,
                "state": state, "period_ns": STEP_PERIOD_NS,
                "mode": segment.mode, "param": segment.param | param_flags,
                "alternative_experience": segment.alternative_experience,
                "timer_origin_ns": segment.first_log_mono_ns,
                "timer_unit_ns": timer_unit_ns,
                "first_event_ns": 0, "last_event_ns": segment.last_event_ns,
            },
            subscribes=[
                SubscriberRoute(FRAME_CHANNEL, capacity=segment.largest_burst),
                SubscriberRoute(TRANSMIT_CHANNEL,
                                capacity=segment.largest_transmit_burst)],
            publishes=[state])
    return m
