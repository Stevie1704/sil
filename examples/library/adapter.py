"""Drive a shared library with its own C API as a SiL Process participant.

The library does not implement `sil_participant_init`; it exposes its own
init/step/output/terminate calls. `binding.py` binds those calls, and this
adapter maps them onto the Step protocol:

- **Lifecycle.** The init line loads the library, binds it and calls its
  init with the period and parameters from the command line. Every Step
  runs one library cycle and publishes its output at the Step's time.
  `shutdown` calls the library's terminate. One process is one lifecycle:
  the kernel starts a new process for every participant of every Run, so each
  instance starts from fresh library globals.
- **Period.** The library is configured with `--period-ns`, which must be
  the participant's `step_period_ns` in the Manifest. The init line does not
  carry the period, so the first Step checks it: a `dt` that differs fails
  the Run instead of running the library at a period it was not given.
- **Inputs.** Each input is held until a newer Message replaces it. Before
  the first Message, the `--initial` values are the input. Nothing is
  interpolated.
- **stdout.** The Step protocol owns stdout, and C libraries print. Before
  the library loads, the real stdout moves to a private descriptor and file
  descriptor 1 points at stderr, so library output stays visible as
  diagnostics and never lands between two protocol lines.
- **Failures.** A library that cannot load, lacks a symbol the binding
  calls, or rejects its configuration fails initialization as a Manifest
  error (exit 2) with the library path and the reason. A cycle the library
  refuses is a Run failure (exit 1). A library that crashes or hangs takes
  the process with it; the kernel reports the child's exit, or the missed
  `--participant-timeout-ms` response deadline, as a Run failure.

Run it as a Manifest command:

    python3 adapter.py speed_filter.so --input ego.speed \\
        --output filter.fast --period-ns 10000000 \\
        --parameter time_constant_s=0.02 --parameter initial_speed_mps=0 \\
        --initial speed_mps=0
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys

from binding import Binding, BindingError

from sil.participant import (
    ManifestError,
    ParticipantFailure,
    StepParticipant,
    run,
)

NANOS_PER_SECOND = 1_000_000_000


class LibraryParticipant(StepParticipant):
    """One library lifecycle behind one input and one output Channel."""

    def __init__(
        self,
        *,
        library_path: str,
        input_channel: str,
        output_channel: str,
        period_ns: int,
        parameters: dict[str, float],
        initial_inputs: dict[str, float],
    ):
        self._library_path = library_path
        self._input_channel = input_channel
        self._output_channel = output_channel
        self._period_ns = period_ns
        self._parameters = parameters
        self._inputs = dict(initial_inputs)
        self._binding: Binding | None = None

    # -- initialization ----------------------------------------------------
    def on_init(self, init: dict) -> None:
        self._check_names("parameter", self._parameters, Binding.PARAMETERS)
        self._check_names("initial input", self._inputs, Binding.INPUTS)
        self._check_channel(init, self._input_channel, "in", Binding.INPUTS)
        self._check_channel(
            init, self._output_channel, "out", Binding.OUTPUTS
        )
        binding = self._bind()
        try:
            binding.init(self._period_ns / NANOS_PER_SECOND, self._parameters)
        except BindingError as error:
            raise ManifestError(
                f"library {self._library_path!r} rejected its configuration "
                f"{self._parameters}: {error}"
            ) from error
        self._binding = binding

    def _bind(self) -> Binding:
        try:
            library = ctypes.CDLL(self._library_path)
        except OSError as error:
            raise ManifestError(
                f"cannot load library {self._library_path!r}: {error}"
            ) from error
        try:
            return Binding(library)
        except BindingError as error:
            raise ManifestError(str(error)) from error

    @staticmethod
    def _check_names(kind: str, given: dict, expected: tuple) -> None:
        if set(given) != set(expected):
            raise ManifestError(
                f"the binding takes {kind}s {sorted(expected)}, "
                f"but the command line gives {sorted(given)}"
            )

    @staticmethod
    def _check_channel(
        init: dict, channel: str, direction: str, fields: tuple
    ) -> None:
        declared = init["channels"].get(channel)
        if declared is None or declared["direction"] != direction:
            raise ManifestError(
                f"channel {channel!r} must be declared {direction!r} for "
                "this participant"
            )
        schema = init["schemas"][declared["schema"]]
        names = [field["name"] for field in schema["fields"]]
        if sorted(names) != sorted(fields):
            raise ManifestError(
                f"channel {channel!r} has fields {sorted(names)}, but the "
                f"binding maps {sorted(fields)}"
            )

    # -- stepping ----------------------------------------------------------
    def on_step(self, t: int, dt: int, inputs: list) -> list[tuple[str, dict]]:
        if dt != self._period_ns:
            raise ParticipantFailure(
                f"the library is configured for a {self._period_ns} ns "
                f"period, but it is stepped every {dt} ns; make --period-ns "
                "the participant's step_period_ns"
            )
        for message in inputs:
            self._inputs.update(message.data)
        try:
            self._binding.step(self._inputs)
        except BindingError as error:
            raise ParticipantFailure(f"at t={t} ns: {error}") from error
        return [(self._output_channel, self._binding.output())]

    def close(self) -> None:
        """End the library lifecycle this process started, if it started."""
        if self._binding is not None:
            self._binding.terminate()
            self._binding = None


def _reserve_protocol_stdout() -> None:
    """Give the Step protocol a descriptor the library cannot write to."""
    protocol_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = os.fdopen(protocol_fd, "w")


def _assignment(value: str) -> tuple[str, float]:
    name, separator, number = value.partition("=")
    if not separator or not name:
        raise argparse.ArgumentTypeError(
            f"expected <name>=<number>, got {value!r}"
        )
    try:
        return name, float(number)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{name!r} needs a number, got {number!r}"
        ) from None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("library", help="path to the shared library")
    parser.add_argument("--input", required=True, metavar="CHANNEL",
                        help="the Channel whose fields are the library input")
    parser.add_argument("--output", required=True, metavar="CHANNEL",
                        help="the Channel the library output is published on")
    parser.add_argument("--period-ns", type=int, required=True,
                        help="the participant's step_period_ns")
    parser.add_argument("--parameter", dest="parameters", action="append",
                        type=_assignment, default=[], metavar="NAME=VALUE",
                        help="one library configuration value")
    parser.add_argument("--initial", dest="initial_inputs", action="append",
                        type=_assignment, default=[], metavar="NAME=VALUE",
                        help="one input value before the first Message")
    args = parser.parse_args(argv)
    _reserve_protocol_stdout()
    participant = LibraryParticipant(
        library_path=args.library,
        input_channel=args.input,
        output_channel=args.output,
        period_ns=args.period_ns,
        parameters=dict(args.parameters),
        initial_inputs=dict(args.initial_inputs),
    )
    try:
        run(participant)
    finally:
        participant.close()


if __name__ == "__main__":
    main()
