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

`--binding` opts into the port contract instead of `binding.py`'s: several
input and output Channels, each bound to one port of the binding with
`--port`, and several cyclic entry points. The rules above apply; what the
port contract adds is stated on `PortLibraryParticipant`:

    python3 adapter.py gap_monitor.so --binding gap_binding.py \\
        --port ego=ego.motion --port radar=radar.object \\
        --port gap=monitor.gap --port report=monitor.report \\
        --period-ns 10000000 --parameter warning_gap_s=1.2 \\
        --initial ego.speed_mps=20 --initial radar.range_m=50 \\
        --initial radar.object_id=0
"""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import os
import struct
import sys
from pathlib import Path

from binding import Binding, BindingError

from sil import schema
from sil.participant import (
    ManifestError,
    ParticipantFailure,
    StepParticipant,
    run,
)

NANOS_PER_SECOND = 1_000_000_000
INTEGER_TYPES = ("u8", "u16", "u32", "u64", "i8", "i16", "i32", "i64")


class _LibraryLifecycle(StepParticipant):
    """One library lifecycle: load, bind, init, Period check, terminate."""

    def __init__(self, *, library_path: str, binding_class: type,
                 binding_error: type[Exception], period_ns: int,
                 parameters: dict[str, float]):
        self._library_path = library_path
        self._binding_class = binding_class
        self._binding_error = binding_error
        self._period_ns = period_ns
        self._parameters = parameters
        self._binding = None

    def _start(self) -> None:
        binding = self._bind()
        try:
            binding.init(self._period_ns / NANOS_PER_SECOND, self._parameters)
        except self._binding_error as error:
            raise ManifestError(
                f"library {self._library_path!r} rejected its configuration "
                f"{self._parameters}: {error}"
            ) from error
        self._binding = binding

    def _bind(self):
        try:
            library = ctypes.CDLL(self._library_path)
        except OSError as error:
            raise ManifestError(
                f"cannot load library {self._library_path!r}: {error}"
            ) from error
        try:
            return self._binding_class(library)
        except self._binding_error as error:
            raise ManifestError(
                f"library {self._library_path!r} {error}"
            ) from error

    def _check_period(self, dt: int) -> None:
        if dt != self._period_ns:
            raise ParticipantFailure(
                f"the library is configured for a {self._period_ns} ns "
                f"period, but it is stepped every {dt} ns; make --period-ns "
                "the participant's step_period_ns"
            )

    def close(self) -> None:
        """End the library lifecycle this process started, if it started."""
        if self._binding is not None:
            self._binding.terminate()
            self._binding = None


class LibraryParticipant(_LibraryLifecycle):
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
        super().__init__(library_path=library_path, binding_class=Binding,
                         binding_error=BindingError, period_ns=period_ns,
                         parameters=parameters)
        self._input_channel = input_channel
        self._output_channel = output_channel
        self._inputs = dict(initial_inputs)

    def on_init(self, init: dict) -> None:
        _check_names("parameter", self._parameters, Binding.PARAMETERS)
        _check_names("initial input", self._inputs, Binding.INPUTS)
        self._check_channel(init, self._input_channel, "in", Binding.INPUTS)
        self._check_channel(
            init, self._output_channel, "out", Binding.OUTPUTS
        )
        self._start()

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
        schema_fields = init["schemas"][declared["schema"]]["fields"]
        names = [field["name"] for field in schema_fields]
        if sorted(names) != sorted(fields):
            raise ManifestError(
                f"channel {channel!r} has fields {sorted(names)}, but the "
                f"binding maps {sorted(fields)}"
            )

    def on_step(self, t: int, dt: int, inputs: list) -> list[tuple[str, dict]]:
        self._check_period(dt)
        for message in inputs:
            self._inputs.update(message.data)
        try:
            self._binding.step(self._inputs)
        except BindingError as error:
            raise ParticipantFailure(f"at t={t} ns: {error}") from error
        return [(self._output_channel, self._binding.output())]


class PortLibraryParticipant(_LibraryLifecycle):
    """One library lifecycle behind several Channels and entry points.

    The binding declares `INPUT_PORTS`, `OUTPUT_PORTS` and `ENTRY_POINTS`
    (see `ports.py`); `--port` binds each port to one Channel. Before the
    library loads, every declaration, the schedule against `--period-ns`,
    every Channel's direction and Schema, and every initial value is checked;
    a mismatch is a Manifest error. Each Step then:

    1. replaces each input port's held value with each delivered Message on
       its Channel, in delivery order, so the last Message of a Burst wins;
    2. writes every input port's held value, in declared order;
    3. runs every due entry point, in declared order, once;
    4. publishes, in declared order, every output port whose entry point ran.

    An input port holds its `--initial` value until the first Message on its
    Channel is visible. Nothing is interpolated, and no entry point runs once
    per Message or catches up on a missed due time.
    """

    def __init__(self, *, library_path: str, binding, ports: dict[str, str],
                 period_ns: int, parameters: dict[str, float],
                 initial_inputs: dict[str, str]):
        super().__init__(library_path=library_path,
                         binding_class=binding.Binding,
                         binding_error=binding.BindingError,
                         period_ns=period_ns, parameters=parameters)
        self._channels = ports
        self._initial = initial_inputs
        self._held: dict[str, dict] = {}
        self._port_of: dict[str, str] = {}

    # -- initialization ----------------------------------------------------
    def on_init(self, init: dict) -> None:
        declared = self._binding_class
        _check_names("parameter", self._parameters, declared.PARAMETERS)
        self._check_entry_points(declared.ENTRY_POINTS)
        self._check_ports(declared)
        types = schema.load(init["schemas"])
        for port in declared.INPUT_PORTS:
            self._check_port(init, port, "input", "in")
        for port in declared.OUTPUT_PORTS:
            self._check_port(init, port, "output", "out")
        unbound = sorted(set(init["channels"]) - set(self._channels.values()))
        if unbound:
            raise ManifestError(
                f"channel {unbound[0]!r} is declared for this participant, "
                "but no port binds it"
            )
        self._held = self._initial_values(declared.INPUT_PORTS, init, types)
        self._port_of = {self._channels[port.name]: port.name
                         for port in declared.INPUT_PORTS}
        self._start()

    def _check_entry_points(self, entries: tuple) -> None:
        names = set()
        for entry in entries:
            where = f"entry point {entry.name!r}"
            if entry.name in names:
                raise ManifestError(f"{where} is declared twice")
            names.add(entry.name)
            if entry.period_ns <= 0:
                raise ManifestError(
                    f"{where}: period_ns must be greater than 0, got "
                    f"{entry.period_ns}"
                )
            if entry.period_ns % self._period_ns:
                raise ManifestError(
                    f"{where}: period_ns {entry.period_ns} is not a multiple "
                    f"of the Step Period {self._period_ns} ns"
                )
            if not 0 <= entry.offset_ns < entry.period_ns:
                raise ManifestError(
                    f"{where}: offset_ns {entry.offset_ns} must be at least 0 "
                    f"and less than period_ns {entry.period_ns}"
                )
            if entry.offset_ns % self._period_ns:
                raise ManifestError(
                    f"{where}: offset_ns {entry.offset_ns} is not a multiple "
                    f"of the Step Period {self._period_ns} ns"
                )

    def _check_ports(self, declared) -> None:
        names = set()
        for port in (*declared.INPUT_PORTS, *declared.OUTPUT_PORTS):
            if port.name in names:
                raise ManifestError(f"port {port.name!r} is declared twice")
            names.add(port.name)
        entries = {entry.name for entry in declared.ENTRY_POINTS}
        for port in declared.OUTPUT_PORTS:
            if port.entry not in entries:
                raise ManifestError(
                    f"output port {port.name!r} names entry point "
                    f"{port.entry!r}, which the binding does not declare"
                )
        if names != set(self._channels):
            raise ManifestError(
                f"the binding has ports {sorted(names)}, but the command "
                f"line binds {sorted(self._channels)}"
            )
        owner: dict[str, str] = {}
        for port, channel in self._channels.items():
            if channel in owner:
                first, second = sorted((owner[channel], port))
                raise ManifestError(
                    f"ports {first!r} and {second!r} are both bound to "
                    f"channel {channel!r}"
                )
            owner[channel] = port

    def _check_port(self, init: dict, port, kind: str,
                    direction: str) -> None:
        channel = self._channels[port.name]
        declared = init["channels"].get(channel)
        if declared is None or declared["direction"] != direction:
            raise ManifestError(
                f"{kind} port {port.name!r} needs channel {channel!r} "
                f"declared {direction!r} for this participant"
            )
        name = declared["schema"]
        given = [(f["name"], f["type"], f.get("count", 1))
                 for f in init["schemas"][name]["fields"]]
        expected = [(f.name, f.type, f.count) for f in port.fields]
        if given != expected:
            raise ManifestError(
                f"port {port.name!r} on channel {channel!r}: Schema {name!r} "
                f"has fields {_layout(given)}, but the binding declares "
                f"{_layout(expected)}"
            )

    def _initial_values(self, inputs: tuple, init: dict,
                        types: dict) -> dict[str, dict]:
        expected = {_initial_name(port, field)
                    for port in inputs for field in port.fields}
        if set(self._initial) != expected:
            raise ManifestError(
                "every input field needs one --initial value: missing "
                f"{sorted(expected - set(self._initial))}, unknown "
                f"{sorted(set(self._initial) - expected)}"
            )
        held = {}
        for port in inputs:
            values = {
                field.name: _initial_value(
                    _initial_name(port, field), field,
                    self._initial[_initial_name(port, field)])
                for field in port.fields
            }
            channel = self._channels[port.name]
            try:
                types[init["channels"][channel]["schema"]].pack(**values)
            except struct.error as error:
                raise ManifestError(
                    f"initial value of port {port.name!r} does not fit its "
                    f"Schema: {error}"
                ) from error
            held[port.name] = values
        return held

    # -- stepping ----------------------------------------------------------
    def on_step(self, t: int, dt: int, inputs: list) -> list[tuple[str, dict]]:
        self._check_period(dt)
        for message in inputs:
            self._held[self._port_of[message.channel]] = message.data
        declared = self._binding_class
        try:
            for port in declared.INPUT_PORTS:
                self._binding.write(port.name, self._held[port.name])
            ran = set()
            for entry in declared.ENTRY_POINTS:
                if t >= entry.offset_ns and (
                        (t - entry.offset_ns) % entry.period_ns == 0):
                    self._binding.run(entry.name)
                    ran.add(entry.name)
            return [(self._channels[port.name], self._binding.read(port.name))
                    for port in declared.OUTPUT_PORTS if port.entry in ran]
        except self._binding_error as error:
            raise ParticipantFailure(f"at t={t} ns: {error}") from error


def _check_names(kind: str, given: dict, expected: tuple) -> None:
    if set(given) != set(expected):
        raise ManifestError(
            f"the binding takes {kind}s {sorted(expected)}, "
            f"but the command line gives {sorted(given)}"
        )


def _layout(fields: list[tuple[str, str, int]]) -> list[str]:
    return [f"{name}:{kind}" + (f"[{count}]" if count != 1 else "")
            for name, kind, count in fields]


def _initial_name(port, field) -> str:
    """The `--initial` name of one field of one input port."""
    return f"{port.name}.{field.name}"


def _initial_value(name: str, field, text: str):
    """One `--initial` value as its field's Python value: a number, a list
    of `count` comma-separated numbers, or bytes for a `u8` array."""
    parse = int if field.type in INTEGER_TYPES else float
    items = text.split(",")
    if len(items) != field.count:
        raise ManifestError(
            f"initial value {name}={text!r} needs {field.count} "
            f"comma-separated values, got {len(items)}"
        )
    try:
        numbers = [parse(item) for item in items]
    except ValueError:
        raise ManifestError(
            f"initial value {name}={text!r} is not a {field.type}"
        ) from None
    if field.count == 1:
        return numbers[0]
    if field.type != "u8":
        return numbers
    try:
        return bytes(numbers)
    except ValueError:
        raise ManifestError(
            f"initial value {name}={text!r} is not a {field.type} array"
        ) from None


def _load_binding(path: str):
    """The binding module at `path`, importing its neighbours like a script."""
    directory = str(Path(path).resolve().parent)
    if directory not in sys.path:
        sys.path.insert(0, directory)
    spec = importlib.util.spec_from_file_location(Path(path).stem, path)
    if spec is None or spec.loader is None:
        raise ImportError("not a Python file")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _reserve_protocol_stdout() -> None:
    """Give the Step protocol a descriptor the library cannot write to."""
    protocol_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = os.fdopen(protocol_fd, "w")


def _pair(value: str) -> tuple[str, str]:
    name, separator, rest = value.partition("=")
    if not separator or not name:
        raise argparse.ArgumentTypeError(
            f"expected <name>=<value>, got {value!r}"
        )
    return name, rest


def _assignment(value: str) -> tuple[str, float]:
    name, number = _pair(value)
    try:
        return name, float(number)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{name!r} needs a number, got {number!r}"
        ) from None


def _participant(parser: argparse.ArgumentParser, args) -> _LibraryLifecycle:
    """The port contract with `--binding`, `binding.py`'s contract without."""
    initial = dict(args.initial_inputs)
    if args.binding is None:
        if args.ports or args.input is None or args.output is None:
            parser.error("binding.py binds one --input and one --output "
                         "Channel, and takes no --port")
        try:
            initial = {name: float(v) for name, v in initial.items()}
        except ValueError:
            parser.error(f"every --initial value needs a number: {initial}")
        return LibraryParticipant(
            library_path=args.library, input_channel=args.input,
            output_channel=args.output, period_ns=args.period_ns,
            parameters=dict(args.parameters), initial_inputs=initial,
        )
    if args.input is not None or args.output is not None:
        parser.error("--binding binds Channels with --port, not "
                     "--input or --output")
    ports = dict(args.ports)
    if len(ports) != len(args.ports):
        parser.error("each --port names its port once")
    try:
        binding = _load_binding(args.binding)
    except Exception as error:  # noqa: BLE001 — any import error is the file's
        parser.error(f"cannot load binding {args.binding!r}: {error!r}")
    if not hasattr(getattr(binding, "Binding", None), "ENTRY_POINTS"):
        parser.error(f"{args.binding} declares no ENTRY_POINTS; it is not "
                     "a port binding")
    return PortLibraryParticipant(
        library_path=args.library, binding=binding, ports=ports,
        period_ns=args.period_ns, parameters=dict(args.parameters),
        initial_inputs=initial,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("library", help="path to the shared library")
    parser.add_argument("--input", metavar="CHANNEL",
                        help="the Channel whose fields are the library input")
    parser.add_argument("--output", metavar="CHANNEL",
                        help="the Channel the library output is published on")
    parser.add_argument("--binding", metavar="FILE",
                        help="a port binding to use instead of binding.py")
    parser.add_argument("--port", dest="ports", action="append", type=_pair,
                        default=[], metavar="PORT=CHANNEL",
                        help="the Channel of one port of the --binding")
    parser.add_argument("--period-ns", type=int, required=True,
                        help="the participant's step_period_ns")
    parser.add_argument("--parameter", dest="parameters", action="append",
                        type=_assignment, default=[], metavar="NAME=VALUE",
                        help="one library configuration value")
    parser.add_argument("--initial", dest="initial_inputs", action="append",
                        type=_pair, default=[], metavar="NAME=VALUE",
                        help="one input value before the first Message; "
                             "<port>.<field> with --binding")
    args = parser.parse_args(argv)
    participant = _participant(parser, args)
    _reserve_protocol_stdout()
    try:
        run(participant)
    finally:
        participant.close()


if __name__ == "__main__":
    main()
