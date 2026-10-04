"""The port binding example's Test participant: it checks every output.

It computes, in Python and without the library, what gap_monitor must
publish. The inputs are held the way the adapter states it: the initial
values until the first Message is visible, the last Message of a Burst
wins, and an input without a new Message keeps its value. The schedule is
the one the binding declares: `track` every 10 ms from 0 ms, `report` every
30 ms from 10 ms, `track` first when both are due. `monitor.gap` must carry
exactly one output at every `track` Step, `monitor.report` exactly one at
every `report` Step and none at any other Step. The first difference fails
the Run with the time, the field, and both values.

It sees the replayed inputs with the same Latencies and Period as the
library, so at each Step it has exactly the inputs the library had.
"""

from __future__ import annotations

import argparse

from sil.participant import ParticipantFailure, StepParticipant, run

MS = 1_000_000
TRACK_PERIOD_NS = 10 * MS
REPORT_PERIOD_NS = 30 * MS
REPORT_OFFSET_NS = 10 * MS


def due(t: int, period_ns: int, offset_ns: int = 0) -> bool:
    return t >= offset_ns and (t - offset_ns) % period_ns == 0


class Expectation:
    """gap_monitor, computed independently of the library."""

    def __init__(self, warning_gap_s: float):
        self._warning_gap_s = warning_gap_s
        self.gap = {"time_gap_s": 0.0, "object_id": 0, "track_cycles": 0}
        self.report = {"min_time_gap_s": 0.0, "warnings": 0,
                       "report_cycles": 0}
        self._window: list[float] = []

    def track(self, speed_mps: float, range_m: float, object_id: int) -> None:
        time_gap_s = range_m / speed_mps
        self.gap = {"time_gap_s": time_gap_s, "object_id": object_id,
                    "track_cycles": self.gap["track_cycles"] + 1}
        self._window.append(time_gap_s)

    def report_cycle(self) -> None:
        window = self._window or [self.gap["time_gap_s"]]
        self.report = {
            "min_time_gap_s": min(window),
            "warnings": sum(g < self._warning_gap_s for g in self._window),
            "report_cycles": self.report["report_cycles"] + 1,
        }
        self._window = []


class GapTest(StepParticipant):
    def __init__(self, *, warning_gap_s: float, ego: dict, radar: dict):
        self._held = {"ego.motion": ego, "radar.object": radar}
        self._model = Expectation(warning_gap_s)

    def on_step(self, t, dt, inputs):
        seen = {"monitor.gap": [], "monitor.report": []}
        for message in inputs:
            if message.channel in self._held:
                self._held[message.channel] = message.data
            else:
                seen[message.channel].append(message)
        ego, radar = self._held["ego.motion"], self._held["radar.object"]
        expected = {"monitor.gap": None, "monitor.report": None}
        if due(t, TRACK_PERIOD_NS):
            self._model.track(ego["speed_mps"], radar["range_m"],
                              radar["object_id"])
            expected["monitor.gap"] = self._model.gap
        if due(t, REPORT_PERIOD_NS, REPORT_OFFSET_NS):
            self._model.report_cycle()
            expected["monitor.report"] = self._model.report
        for channel, fields in expected.items():
            self._check(channel, t, fields, seen[channel])

    @staticmethod
    def _check(channel: str, t: int, expected: dict | None,
               messages: list) -> None:
        times = [m.publish_ns for m in messages]
        if times != ([t] if expected is not None else []):
            count = "one output" if expected is not None else "no output"
            raise ParticipantFailure(
                f"{channel}: expected {count} published at t={t} ns, "
                f"got {times}"
            )
        if expected is None:
            return
        got = messages[0].data
        for name, value in expected.items():
            if got[name] != value:
                raise ParticipantFailure(
                    f"{channel} at t={t} ns: {name} {got[name]!r} differs "
                    f"from the independently computed {value!r}"
                )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--warning-gap-s", type=float, required=True)
    parser.add_argument("--initial-speed-mps", type=float, required=True)
    parser.add_argument("--initial-range-m", type=float, required=True)
    parser.add_argument("--initial-object-id", type=int, required=True)
    args = parser.parse_args(argv)
    run(GapTest(
        warning_gap_s=args.warning_gap_s,
        ego={"speed_mps": args.initial_speed_mps},
        radar={"range_m": args.initial_range_m,
               "object_id": args.initial_object_id},
    ))


if __name__ == "__main__":
    main()
