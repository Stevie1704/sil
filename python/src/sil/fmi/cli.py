"""The command line, and the participant it describes.

`python -m sil.fmi` is one command for two participants: an FMU named on its
own, and a group declared with `--instance`. Which of the two a Manifest asked
for is decided here and nowhere else, and arguments that describe neither are
answered with a participant that rejects the Run through the step protocol.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sil.participant import (
    ManifestError,
    ParticipantFailure,
    StepParticipant,
    run,
)

from sil.fmi.group import FmuGroupParticipant
from sil.fmi.single import FmuParticipant


class _Rejection(StepParticipant):
    """A participant that rejects the Run its own arguments cannot describe.

    The kernel calls a Manifest error what a participant reports before it is
    ready, so an argument mistake is reported there rather than by exiting
    before the handshake and leaving the kernel to guess what went wrong.
    """

    def __init__(self, reason: str):
        self.name = ""
        self._reason = reason

    def on_init(self, init: dict) -> None:
        raise ManifestError(self._reason)

    def close(self) -> None:
        pass


def _close_after_failure(participant: StepParticipant) -> None:
    """Drop the FMUs on the way out of a failure that is already reported.

    Terminating an FMU that has already failed may fail in turn; that second
    diagnostic must not replace the first one.
    """
    try:
        participant.close()
    except ParticipantFailure:
        pass


def _arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m sil.fmi",
        description="Drive an FMI 3.0 co-simulation FMU as a participant.",
    )
    parser.add_argument(
        "fmu", nargs="?",
        help="path to the FMU archive, for a participant that is one FMU",
    )
    parser.add_argument(
        "--instance", action="append", default=[], nargs=2,
        metavar=("NAME", "PATH"),
        help="declare one FMU of a connected group under a name of this "
             "Run's own; every other argument spells a variable of it as "
             "'<name>.<variable>'. The path is its own argument so the kernel "
             "resolves it against the Manifest and digests it",
    )
    parser.add_argument(
        "--connect", action="append", default=[],
        metavar="NAME.TERMINAL=NAME.TERMINAL",
        help="connect two network terminals of a group: each end's Tx_Data "
             "becomes the other end's Rx_Data, in the same instant",
    )
    parser.add_argument(
        "--bus-profile", default="", metavar="MEDIA-TYPE",
        help="the media type every connected terminal's buffers carry, "
             "without its parameters; required by --instance",
    )
    parser.add_argument(
        "--bind", action="append", default=[], metavar="CHANNEL:FIELD=VARIABLE",
        help="bind one Channel schema field to one FMU variable; declaring "
             "any binding makes the bindings the whole mapping",
    )
    parser.add_argument(
        "--start", action="append", default=[], metavar="VARIABLE=VALUE",
        help="set one FMU variable before initialization mode is entered; a "
             "structural parameter goes inside Configuration Mode, and a "
             "Binary value is hexadecimal",
    )
    return parser.parse_args(argv)


def _participant(args: argparse.Namespace) -> StepParticipant:
    """The participant these arguments describe, or one that rejects the Run.

    An argument mistake is reported through the step protocol rather than by
    exiting before the handshake, so the kernel calls it the Manifest error it
    is and names the participant that found it.
    """
    if args.instance and args.fmu is not None:
        return _Rejection(
            f"this participant is one FMU {str(args.fmu)!r} and a group of "
            f"{len(args.instance)} --instance declarations at once; it is "
            f"either"
        )
    if args.instance:
        return FmuGroupParticipant(
            args.instance, connects=args.connect, binds=args.bind,
            starts=args.start, profile=args.bus_profile,
        )
    for argument, declared in (("--connect", args.connect),
                               ("--bus-profile", args.bus_profile)):
        if declared:
            return _Rejection(
                f"{argument} describes a group of connected FMUs, and this "
                f"participant declares no --instance"
            )
    if args.fmu is None:
        return _Rejection(
            "this participant drives no FMU: name one archive, or declare a "
            "group with --instance <name>=<path>"
        )
    return FmuParticipant(
        Path(args.fmu), binds=args.bind, starts=args.start
    )


def main(argv: list[str] | None = None) -> None:
    args = _arguments(sys.argv[1:] if argv is None else argv)
    participant = _participant(args)
    try:
        run(participant)
    except BaseException:
        _close_after_failure(participant)
        raise
    try:
        participant.close()
    except ParticipantFailure as error:
        # The step protocol is over by the time the FMU is terminated, so this
        # failure cannot travel as a `fail` line and the kernel sees only a
        # nonzero exit. Name the participant, the call and the status here.
        raise SystemExit(
            f"participant {participant.name!r} failed: {error}"
        ) from error
