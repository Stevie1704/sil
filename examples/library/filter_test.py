"""The library example's Test participant: it checks every library output.

It computes, in Python and without the library, what each filter instance
must publish: the same recorded input held the same way, the same initial
values, the first-order low-pass the library header describes. Each output a
library instance published is compared with that value at its publish time,
and the cycle count proves the instance started from a fresh lifecycle. The
first difference fails the Run with the time, the field, and both values.

It sees the replayed input with the same Latency and Period as the library
instances, so at each Step it has exactly the input they had. The output
Channels have zero Latency and this participant runs after the instances in
each Slot, so an output published at `t` is checked at `t`. The output of
the last Step is checked too.
"""

from __future__ import annotations

import argparse

from sil.participant import ParticipantFailure, StepParticipant, run

NANOS_PER_SECOND = 1_000_000_000
# The library computes in binary64 too; a compiler may still contract the
# update into a fused multiply-add, which differs in the last bits only.
TOLERANCE = 1e-9


class Expectation:
    """One library instance, computed independently of the library."""

    def __init__(self, period_s: float, time_constant_s: float,
                 initial_speed_mps: float):
        self._gain = period_s / (time_constant_s + period_s)
        self.filtered_speed_mps = initial_speed_mps
        self.cycles = 0

    def step(self, speed_mps: float) -> None:
        self.filtered_speed_mps += self._gain * (
            speed_mps - self.filtered_speed_mps)
        self.cycles += 1


class FilterTest(StepParticipant):
    def __init__(self, *, input_channel: str, period_ns: int,
                 initial_speed_mps: float,
                 instances: dict[str, tuple[float, float]]):
        self._input_channel = input_channel
        self._period_ns = period_ns
        self._speed_mps = initial_speed_mps
        self._models = {
            channel: Expectation(period_ns / NANOS_PER_SECOND, *parameters)
            for channel, parameters in instances.items()
        }

    def on_step(self, t, dt, inputs):
        seen = {channel: [] for channel in self._models}
        for message in inputs:
            if message.channel == self._input_channel:
                self._speed_mps = message.data["speed_mps"]
            else:
                seen[message.channel].append(message)
        for channel, model in self._models.items():
            model.step(self._speed_mps)
            self._check(channel, t, model, seen[channel])

    @staticmethod
    def _check(channel: str, t: int, model: Expectation,
               messages: list) -> None:
        if [m.publish_ns for m in messages] != [t]:
            raise ParticipantFailure(
                f"{channel}: expected one output published at t={t} ns, "
                f"got {[m.publish_ns for m in messages]}"
            )
        got = messages[0].data
        if got["cycles"] != model.cycles:
            raise ParticipantFailure(
                f"{channel} at t={t} ns: cycles {got['cycles']} differs "
                f"from the expected {model.cycles}"
            )
        speed = model.filtered_speed_mps
        if abs(got["filtered_speed_mps"] - speed) > TOLERANCE:
            raise ParticipantFailure(
                f"{channel} at t={t} ns: filtered_speed_mps "
                f"{got['filtered_speed_mps']!r} differs from the "
                f"independently computed {speed!r}"
            )


def _instance(value: str) -> tuple[str, tuple[float, float]]:
    channel, _, rest = value.partition("=")
    time_constant_s, _, initial_speed_mps = rest.partition(",")
    return channel, (float(time_constant_s), float(initial_speed_mps))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", required=True, metavar="CHANNEL")
    parser.add_argument("--period-ns", type=int, required=True)
    parser.add_argument("--initial-speed-mps", type=float, required=True)
    parser.add_argument(
        "--expect", dest="instances", action="append", type=_instance,
        required=True, metavar="CHANNEL=TIME_CONSTANT_S,INITIAL_SPEED_MPS",
        help="one library instance's output Channel and its parameters")
    args = parser.parse_args(argv)
    run(FilterTest(input_channel=args.input, period_ns=args.period_ns,
                initial_speed_mps=args.initial_speed_mps,
                instances=dict(args.instances)))


if __name__ == "__main__":
    main()
