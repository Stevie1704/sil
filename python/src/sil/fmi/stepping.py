"""What a single FMU and a group of them both obey while being advanced.

An Importer of one FMU and an Importer of an FMU group schedule differently —
one FMU is stepped on the kernel's Slot grid, a group meets its peers at
communication points between two Slots — but three rules are the same on either
side, and they are stated here once:

- one event is iterated to quiescence under a declared bound;
- a declared next event time lands on the kernel's nanosecond grid the same
  way wherever it is read;
- the intervals an FMU is stepped over are contiguous from virtual time zero.

A scheduling *policy* is not here. Which instant to be in Event Mode at, and
what to do with what an event produced, stays with the Importer that owns the
grid.
"""

from __future__ import annotations

from collections.abc import Callable

from sil.participant import ParticipantFailure

from sil.fmi.runtime import NS_PER_S, CoSimulation, DiscreteStates

# How many times one event may ask for another discrete-state update before
# this importer stops asking. FMI 3.0 puts no bound on the iteration, and a
# Run that never leaves an event would hang until the response deadline
# without saying why; the bound is declared here so the failure is the same
# one on every machine.
_MAX_EVENT_ITERATIONS = 100


def run_event(fmu: CoSimulation, event_time_ns: int,
              collect: Callable[[], list]) -> tuple[list, DiscreteStates]:
    """Iterate one event to quiescence, collecting what each update produced.

    What the list holds is `collect`'s own business and differs by caller — one
    FMU collects Messages, a group collects a payload and the terminal that
    produced it — because only the caller knows what an activation belongs to.

    `collect` is called before every discrete-state update and once more after
    the update that ends the event: an update is exactly what can raise an
    output Clock, the update that ends the event included. Reading a Clock
    that is not active costs nothing, and not reading it loses the activation
    for good, because the buffer it gates is defined only while it is up.

    The iteration is bounded: an FMU that never converges fails with a
    diagnostic rather than holding the Run until its response deadline.
    """
    produced: list = []
    for _ in range(_MAX_EVENT_ITERATIONS):
        produced.extend(collect())
        states = fmu.update_discrete_states()
        if states.terminate:
            raise ParticipantFailure(
                f"fmi3UpdateDiscreteStates requested termination via "
                f"terminateSimulation at {event_time_ns} ns"
            )
        if not states.need_update:
            produced.extend(collect())
            return produced, states
    raise ParticipantFailure(
        f"fmi3UpdateDiscreteStates asked for another discrete-state "
        f"update {_MAX_EVENT_ITERATIONS} times at {event_time_ns} ns; "
        f"this importer bounds the iteration of one event"
    )


def declared_ns(next_event_time: float | None) -> int | None:
    """A declared next event time, in the kernel's own nanoseconds.

    The FMU declares a double of seconds and the Slot grid is integer
    nanoseconds, so the two are compared in nanoseconds: an event the FMU
    means to fall on a communication point must not be missed, or refused,
    because the two ways of writing that instant differ in the last bit.
    """
    if next_event_time is None:
        return None
    return round(next_event_time * NS_PER_S)


def require_contiguous(standing_ns: int, t: int) -> None:
    """Refuse to step a clocked FMU over an interval it never covered.

    The FMU was initialized at virtual time zero and is advanced one
    contiguous interval at a time. An activation that does not continue where
    the last one ended would leave an interval unstepped, and every event time
    after it would name an instant the FMU never reached.
    """
    if t != standing_ns:
        raise ParticipantFailure(
            f"the FMU stands at {standing_ns} ns and this activation is at "
            f"{t} ns; a clocked FMU is stepped over contiguous intervals from "
            f"virtual time zero"
        )
