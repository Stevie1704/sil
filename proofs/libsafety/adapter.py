"""Drive opendbc's safety library as a SiL Process participant (issue #193).

The library has its own C API and no `sil_participant_init`. `binding.py`
binds that API; this adapter maps the upstream replay policy
(`proofs/public-workloads/libsafety_workload.py`) onto the Step protocol:

- **Bursts.** One recorded `can` event is one Burst: every received frame
  the Replay participant published at one instant, in Publish order. The
  input Channel declares Latency 0, so a Burst is visible in the first Step
  at or after its instant. The Step period must be shorter than the shortest
  event interval: a Step that holds Bursts of two instants fails the Run,
  because one observation per Step could no longer name its event.
- **Time.** The library reads no clock but `set_timer`. Each Burst sets it
  from the instant the Burst names, not from the Step:
  `((timer_origin_ns + publish_ns) // timer_unit_ns) % 0xFFFFFFFF`. The
  origin is the recording's first `logMonoTime`; the unit is 1000 ns, the
  library's microsecond counter.
- **Warm-up.** `safety_tick` runs before the frames only when the Burst is
  more than 1 s from both the first and the last event of the segment.
- **Frames.** Per frame, `safety_fwd_hook(src, address)`, then
  `safety_rx_hook` with bus `src % 4`, as upstream does.
- **Observation.** After a Burst, one output Message: the Burst's instant
  (`event_ns`), the accepted and rejected frame counts, and every state field
  the binding reads. A Step without a Burst publishes nothing.
- **Initial state.** `set_safety_hooks(mode, param)` must return 0, then
  `set_alternative_experience`. A refusal is a Manifest error (exit 2).

The library keeps its state in C globals, so one process is one instance.
The kernel starts a new process for every participant of every Run.

    python3 adapter.py libsafety.so --input can.rx --output libsafety.state \\
        --period-ns 1000000 --mode 2 --param 73 --alternative-experience 0 \\
        --timer-origin-ns 18054876797669 --timer-unit-ns 1000 \\
        --first-event-ns 0 --last-event-ns 59990294747
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
from dataclasses import dataclass

from binding import BindingError, Libsafety

from sil.participant import (
    Input,
    ManifestError,
    ParticipantFailure,
    StepParticipant,
    run,
)

# Upstream replay's counter modulus and its one-second tick margin.
TIMER_MODULUS = 0xFFFFFFFF
TICK_MARGIN_NS = 1_000_000_000
FRAME_FIELDS = ("address", "src", "length", *(f"d{i}" for i in range(8)))


@dataclass(frozen=True)
class EventPolicy:
    """How one Burst's instant becomes the library's time and tick."""

    timer_origin_ns: int
    timer_unit_ns: int
    first_event_ns: int
    last_event_ns: int

    def timer(self, event_ns: int) -> int:
        return ((self.timer_origin_ns + event_ns) // self.timer_unit_ns
                % TIMER_MODULUS)

    def ticks(self, event_ns: int) -> bool:
        return (event_ns - self.first_event_ns > TICK_MARGIN_NS
                and self.last_event_ns - event_ns > TICK_MARGIN_NS)


class LibsafetyParticipant(StepParticipant):
    """One library instance behind one frame Channel and one state Channel."""

    def __init__(self, *, bind, input_channel: str, output_channel: str,
                 period_ns: int, policy: EventPolicy):
        self._bind = bind
        self._input_channel = input_channel
        self._output_channel = output_channel
        self._period_ns = period_ns
        self._policy = policy
        self._library = None

    def on_init(self, init: dict) -> None:
        _check_channel(init, self._input_channel, "in", FRAME_FIELDS)
        self.bind_library()

    def bind_library(self) -> None:
        self._library = self._bind()

    def on_step(self, t: int, dt: int, inputs: list[Input]):
        if dt != self._period_ns:
            raise ParticipantFailure(
                f"the adapter is configured for a {self._period_ns} ns "
                f"period, but it is stepped every {dt} ns; make --period-ns "
                "the participant's step_period_ns")
        frames = [m for m in inputs if m.channel == self._input_channel]
        instants = sorted({m.publish_ns for m in frames})
        if not instants:
            return []
        if len(instants) > 1:
            raise ParticipantFailure(
                f"the Step at t={t} ns holds two Bursts, at {instants} ns; "
                "the Step period must be shorter than the shortest interval "
                "between recorded events")
        return [(self._output_channel, self._process(instants[0], frames))]

    def _process(self, event_ns: int, frames: list[Input]) -> dict:
        library = self._library
        library.set_timer(self._policy.timer(event_ns))
        if self._policy.ticks(event_ns):
            library.tick()
        accepted = 0
        for message in frames:
            frame = message.data
            payload = bytes(frame[f"d{i}"] for i in range(frame["length"]))
            library.forward(frame["src"], frame["address"])
            accepted += library.receive(frame["address"], frame["src"] % 4,
                                        payload)
        return {"event_ns": event_ns, "accepted": accepted,
                "rejected": len(frames) - accepted, **library.state()}


def _check_channel(init: dict, channel: str, direction: str,
                   fields: tuple) -> None:
    declared = init["channels"].get(channel)
    if declared is None or declared["direction"] != direction:
        raise ManifestError(
            f"channel {channel!r} must be declared {direction!r} for this "
            "participant")
    schema = init["schemas"][declared["schema"]]
    names = [field["name"] for field in schema["fields"]]
    if names != list(fields):
        raise ManifestError(
            f"channel {channel!r} has fields {names}, but the adapter reads "
            f"{list(fields)}")


def library(args: argparse.Namespace):
    """Binds and initializes the library, or raises a Manifest error."""
    path = args.library
    contract = (args.mode, args.param, args.alternative_experience)

    def bind() -> Libsafety:
        try:
            library = Libsafety(ctypes.CDLL(path))
            library.init(*contract)
        except OSError as error:
            raise ManifestError(f"cannot load library {path!r}: {error}") from error
        except BindingError as error:
            raise ManifestError(f"library {path!r}: {error}") from error
        return library
    return bind


def _reserve_protocol_stdout() -> None:
    """Give the Step protocol a descriptor the library cannot write to."""
    protocol_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = os.fdopen(protocol_fd, "w")


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("library", help="path to libsafety.so")
    parser.add_argument("--input", required=True, metavar="CHANNEL",
                        help="the Channel of received CAN frames")
    parser.add_argument("--output", required=True, metavar="CHANNEL",
                        help="the Channel one observation per Burst goes to")
    parser.add_argument("--period-ns", type=int, required=True,
                        help="the participant's step_period_ns")
    for name in ("mode", "param", "alternative-experience"):
        parser.add_argument(f"--{name}", type=int, required=True,
                            help="recorded carParams safety contract")
    parser.add_argument("--timer-origin-ns", type=int, required=True,
                        help="recorded logMonoTime at Virtual time 0")
    parser.add_argument("--timer-unit-ns", type=int, required=True,
                        help="nanoseconds per library timer count")
    parser.add_argument("--first-event-ns", type=int, required=True,
                        help="Virtual time of the segment's first event")
    parser.add_argument("--last-event-ns", type=int, required=True,
                        help="Virtual time of the segment's last event")
    return parser


def participant(args: argparse.Namespace, bind) -> LibsafetyParticipant:
    return LibsafetyParticipant(
        bind=bind, input_channel=args.input, output_channel=args.output,
        period_ns=args.period_ns,
        policy=EventPolicy(timer_origin_ns=args.timer_origin_ns,
                           timer_unit_ns=args.timer_unit_ns,
                           first_event_ns=args.first_event_ns,
                           last_event_ns=args.last_event_ns))


def serve(participant: LibsafetyParticipant) -> None:
    _reserve_protocol_stdout()
    run(participant)


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    serve(participant(args, library(args)))


if __name__ == "__main__":
    main()
