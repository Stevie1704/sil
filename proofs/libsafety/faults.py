"""The crash and timeout controls: the adapter with a fault at one event.

The library's own code is unchanged. This control wraps the bound library
and, when the adapter sets the timer for event `--at-event` (counted from
0), either kills its own process with SIGSEGV, as a crash in the library
would, or never returns, as a hang in the library would. The Run must then
fail for that reason: the kernel reports the child's signal, or the missed
`--participant-timeout-ms` response deadline, as a Run failure (exit 1).

    python3 faults.py --fault crash --at-event 100 <adapter arguments>
"""

from __future__ import annotations

import argparse
import os
import signal

import adapter

FAULTS = ("crash", "hang")


class FaultAtEvent:
    """The bound library, with one fault when event `at_event` starts."""

    def __init__(self, library, fault: str, at_event: int):
        self._library = library
        self._fault = fault
        self._at_event = at_event
        self._events = 0

    def set_timer(self, microseconds: int) -> None:
        if self._events == self._at_event:
            if self._fault == "crash":
                os.kill(os.getpid(), signal.SIGSEGV)
            while True:
                signal.pause()
        self._events += 1
        self._library.set_timer(microseconds)

    def __getattr__(self, name):
        return getattr(self._library, name)


def main(argv: list[str] | None = None) -> None:
    parser = adapter.parser()
    parser.add_argument("--fault", choices=FAULTS, required=True)
    parser.add_argument("--at-event", type=int, required=True)
    args: argparse.Namespace = parser.parse_args(argv)
    bind = adapter.library(args)
    adapter.serve(adapter.participant(
        args, lambda: FaultAtEvent(bind(), args.fault, args.at_event)))


if __name__ == "__main__":
    main()
