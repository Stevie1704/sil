"""The native side of the FMU: the shared library, the instance, the buffers.

Every ctypes array, every raw pointer and every `fmi3*` entry point of this
importer is here. What the layers above hand over and take back is Python —
ints, floats and `bytes` — so a change to how a value crosses into the FMU is a
change to this module alone.

A buffer is the memory one FMI call reads or writes, and it belongs to the
object that allocated it for as long as that object lives. FMI 3.0 has the
importer own the buffers it hands over for the duration of the call, so a
buffer is allocated once at initialization rather than per step: reallocating
one on every call would put its lifetime in the garbage collector's hands. A
buffer the FMU fills is the FMU's own and valid only until its next call, so
what comes out of one is copied at once.
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable, Sequence

from sil.participant import ParticipantFailure

from sil.fmi.description import (
    BINARY,
    SCALARS,
    STRUCTURAL,
    ModelDescription,
    Variable,
)

# Virtual time is integer nanoseconds; seconds are derived from those integers
# on every step and never accumulated, so the same integer always produces the
# same double. An integer denominator keeps that true past the 53 bits an
# int-to-float conversion holds exactly.
NS_PER_S = 1_000_000_000

# fmi3Status. Only OK means the call succeeded; every other status aborts the
# Run before the importer can continue with a possibly invalid FMU state.
_FMI_STATUS_NAMES = ("OK", "Warning", "Discard", "Error", "Fatal")
_FMI_SUCCESS_STATUS = 0
_FMI_FATAL_STATUS = 4

# fmi3IntervalQualifier. `NotYetKnown` is the FMU saying it has no activation
# to ask for; `Unchanged` leaves the one already asked for standing;
# `Changed` states a new interval, counted from the event it was read in.
_INTERVAL_NOT_YET_KNOWN = 0
_INTERVAL_UNCHANGED = 1
_INTERVAL_CHANGED = 2
_INTERVAL_QUALIFIERS = ("NotYetKnown", "Unchanged", "Changed")

_LOG_CALLBACK = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p
)
_VALUE_REFERENCES = ctypes.POINTER(ctypes.c_uint32)
_SIZES = ctypes.POINTER(ctypes.c_size_t)
# fmi3Binary is `const fmi3Byte*`: the FMU fills these pointers on a get and
# reads them on a set, and the buffers they name belong to whoever produced
# them.
_BINARIES = ctypes.POINTER(ctypes.c_void_p)
_FLAG = ctypes.POINTER(ctypes.c_bool)

# The co-simulation entry points this importer drives, with their argument and
# return types.
_SIGNATURES = {
    "fmi3InstantiateCoSimulation": (
        ctypes.c_void_p,
        [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
         ctypes.c_bool, ctypes.c_bool, ctypes.c_bool, ctypes.c_bool,
         _VALUE_REFERENCES, ctypes.c_size_t,
         ctypes.c_void_p, _LOG_CALLBACK, ctypes.c_void_p],
    ),
    "fmi3EnterInitializationMode": (
        ctypes.c_int,
        [ctypes.c_void_p, ctypes.c_bool, ctypes.c_double, ctypes.c_double,
         ctypes.c_bool, ctypes.c_double],
    ),
    "fmi3ExitInitializationMode": (ctypes.c_int, [ctypes.c_void_p]),
    "fmi3EnterConfigurationMode": (ctypes.c_int, [ctypes.c_void_p]),
    "fmi3ExitConfigurationMode": (ctypes.c_int, [ctypes.c_void_p]),
    "fmi3EnterEventMode": (ctypes.c_int, [ctypes.c_void_p]),
    "fmi3EnterStepMode": (ctypes.c_int, [ctypes.c_void_p]),
    # A Clock is a scalar by definition, so its accessors take no value count.
    "fmi3GetClock": (
        ctypes.c_int,
        [ctypes.c_void_p, _VALUE_REFERENCES, ctypes.c_size_t, _FLAG],
    ),
    "fmi3SetClock": (
        ctypes.c_int,
        [ctypes.c_void_p, _VALUE_REFERENCES, ctypes.c_size_t, _FLAG],
    ),
    # A countdown Clock's interval, as the exact rational the FMU computed it
    # as. The decimal form is the same quantity already divided into a double,
    # and a Slot grid of integer nanoseconds has no use for that rounding.
    "fmi3GetIntervalFraction": (
        ctypes.c_int,
        [ctypes.c_void_p, _VALUE_REFERENCES, ctypes.c_size_t,
         ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(ctypes.c_uint64),
         ctypes.POINTER(ctypes.c_int32)],
    ),
    "fmi3UpdateDiscreteStates": (
        ctypes.c_int,
        [ctypes.c_void_p, _FLAG, _FLAG, _FLAG, _FLAG, _FLAG,
         ctypes.POINTER(ctypes.c_double)],
    ),
    "fmi3DoStep": (
        ctypes.c_int,
        [ctypes.c_void_p, ctypes.c_double, ctypes.c_double, ctypes.c_bool,
         _FLAG, _FLAG, _FLAG, ctypes.POINTER(ctypes.c_double)],
    ),
    "fmi3GetBinary": (
        ctypes.c_int,
        [ctypes.c_void_p, _VALUE_REFERENCES, ctypes.c_size_t,
         _SIZES, _BINARIES, ctypes.c_size_t],
    ),
    "fmi3SetBinary": (
        ctypes.c_int,
        [ctypes.c_void_p, _VALUE_REFERENCES, ctypes.c_size_t,
         _SIZES, _BINARIES, ctypes.c_size_t],
    ),
    "fmi3Terminate": (ctypes.c_int, [ctypes.c_void_p]),
    "fmi3FreeInstance": (None, [ctypes.c_void_p]),
}


# The native element each mapped scalar type crosses in. It is the other half
# of `description.SCALARS` — what a Channel field carries is stated there, what
# the FMI call takes is stated here — and the two are keyed alike. The
# signatures below are built by walking `SCALARS`, so a type declared there
# with no element here fails when this module is imported rather than on the
# first Run that binds one.
_ELEMENTS = {"Float64": ctypes.c_double, "Boolean": ctypes.c_bool}


def _scalar_signatures() -> dict:
    """Get and Set for every scalar type: one shape, named once per type."""
    signatures = {}
    for kind in SCALARS:
        element = _ELEMENTS[kind]
        accessor = (
            ctypes.c_int,
            [ctypes.c_void_p, _VALUE_REFERENCES, ctypes.c_size_t,
             ctypes.POINTER(element), ctypes.c_size_t],
        )
        signatures[f"fmi3Get{kind}"] = accessor
        signatures[f"fmi3Set{kind}"] = accessor
    return signatures


_SIGNATURES.update(_scalar_signatures())


class _Library:
    """The FMU's shared library, with the signatures ctypes cannot infer.

    An entry point is bound on first use rather than at load: FMI 3.0 has an
    FMU export every function of the interface, but binding all of them up
    front would turn an accessor a Run never reaches into a load failure. An
    instance handle is a pointer, so without a declared `c_void_p` restype
    ctypes would truncate it to an int.
    """

    def __init__(self, binary: Path):
        self._library = ctypes.CDLL(str(binary))
        self._bound: dict[str, Callable] = {}

    def __getitem__(self, name: str) -> Callable:
        entry_point = self._bound.get(name)
        if entry_point is None:
            try:
                entry_point = getattr(self._library, name)
            except AttributeError as error:
                raise ParticipantFailure(
                    f"FMU exports no {name}"
                ) from error
            restype, argtypes = _SIGNATURES[name]
            entry_point.restype = restype
            entry_point.argtypes = argtypes
            self._bound[name] = entry_point
        return entry_point


def _log_to_stderr(environment, status, category, message) -> None:
    """The FMU's logger. stdout is the step protocol, so it cannot go there."""
    print(f"fmu: {message.decode(errors='replace')}", file=sys.stderr)


def _status_name(status: int) -> str:
    """Name an FMI status without losing an unknown status to IndexError."""
    if 0 <= status < len(_FMI_STATUS_NAMES):
        return _FMI_STATUS_NAMES[status]
    return f"unknown status {status}"


def _references(*values: int):
    """One value-reference array, as every accessor takes it."""
    return (ctypes.c_uint32 * len(values))(*values)


@dataclass(frozen=True)
class DiscreteStates:
    """What one `fmi3UpdateDiscreteStates` said about the event it ended.

    `next_event_time` is the time the FMU declared its next event at, in
    seconds, and None means it declared none. An FMU that declares one is
    asking to be stepped onto it, which this importer cannot promise: the
    kernel owns the Slot grid.
    """

    need_update: bool
    terminate: bool
    next_event_time: float | None


class CoSimulation:
    """One instantiated FMU, driven through its co-simulation entry points.

    The instance owns its internal state and is given a communication point
    and a step size, which is the contract the kernel already offers a process
    participant — this class is the translation between the two, and nothing
    above it deals in ctypes.
    """

    def __init__(self, binary: Path, description: ModelDescription,
                 *, event_mode: bool = False):
        self._library = _Library(binary)
        # The FMU calls this for the life of the instance, so the ctypes
        # trampoline has to outlive this constructor.
        self._logger = _LOG_CALLBACK(_log_to_stderr)
        self._instance = self._library["fmi3InstantiateCoSimulation"](
            description.model_identifier.encode(),
            description.instantiation_token.encode(),
            None,
            False,       # visible
            True,        # loggingOn
            event_mode,  # eventModeUsed
            False,       # earlyReturnAllowed
            None, 0,
            None, self._logger, None,
        )
        if not self._instance:
            raise ParticipantFailure(
                f"fmi3InstantiateCoSimulation returned no instance for "
                f"{description.model_identifier!r}"
            )

    def initialize(self) -> None:
        """Enter and leave initialization mode at virtual time zero.

        The start time is the instant the kernel begins every Run at, and it
        is stated rather than left to the FMU's own `DefaultExperiment`, which
        is the experiment the vendor shipped and not the Run the Manifest
        declares.

        No stop time is declared: the Manifest's duration is the kernel's, and
        an FMU told a stop time it never reaches would reject the last step.

        With Event Mode in use the FMU leaves initialization *in Event Mode*,
        so the caller handles that first event before stepping; without it,
        the FMU is steppable when this returns.
        """
        self._call(
            "fmi3EnterInitializationMode", False, 0.0, 0.0, False, 0.0
        )
        self._call("fmi3ExitInitializationMode")

    def enter_event_mode(self) -> None:
        self._call("fmi3EnterEventMode")

    def enter_step_mode(self) -> None:
        self._call("fmi3EnterStepMode")

    def raise_clocks(self, references: Sequence[int]) -> None:
        """Raise several Clocks of this FMU together, in one call.

        Value references are what a caller above states, because that is all
        the Importer knows about a Clock it decides the instant of; the array
        the call takes is built and owned here.
        """
        self._set_clock(
            _references(*references),
            (ctypes.c_bool * len(references))(*(True,) * len(references)),
        )

    # The accessors below take the arrays a buffer of this module allocated
    # and owns. They are the buffer-facing half of the instance rather than
    # part of what the importer drives an FMU through, which is why a layer
    # above never reaches one: it has no array to pass.
    #
    # A buffer is handed the instance on every call rather than holding one,
    # because it outlives no instance — it predates it. A mapping is resolved
    # and its buffers allocated before any FMU is loaded, so that a Manifest
    # that cannot hold is rejected without loading one.

    def _get_clock(self, references, values) -> None:
        self._call("fmi3GetClock", references, len(references), values)

    def _set_clock(self, references, values) -> None:
        self._call("fmi3SetClock", references, len(references), values)

    def _interval_fraction(self, references, counters, resolutions,
                           qualifiers) -> None:
        """Read the countdown intervals the FMU is asking to be activated at."""
        self._call(
            "fmi3GetIntervalFraction", references, len(references),
            counters, resolutions, qualifiers,
        )

    def update_discrete_states(self) -> DiscreteStates:
        """One discrete-state update, and what the FMU said about the event."""
        need_update = ctypes.c_bool()
        terminate = ctypes.c_bool()
        nominals_changed = ctypes.c_bool()
        values_changed = ctypes.c_bool()
        next_event_defined = ctypes.c_bool()
        next_event_time = ctypes.c_double()
        self._call(
            "fmi3UpdateDiscreteStates",
            ctypes.byref(need_update), ctypes.byref(terminate),
            ctypes.byref(nominals_changed), ctypes.byref(values_changed),
            ctypes.byref(next_event_defined), ctypes.byref(next_event_time),
        )
        return DiscreteStates(
            need_update=need_update.value,
            terminate=terminate.value,
            next_event_time=(
                next_event_time.value if next_event_defined.value else None
            ),
        )

    def apply_start_values(self, starts: list[tuple[Variable, object]]) -> None:
        """Write the declared start values, before initialization mode.

        A structural parameter is written inside Configuration Mode, which is
        where FMI 3.0 has one changed; every other variable is written in the
        instantiated state, where the standard has an importer override the
        start values a description declares. An FMU none of whose variables
        is a structural parameter is never asked to configure.

        Each group keeps the order it was declared in, so a variable declared
        twice ends at its last value.
        """
        structural = [
            (variable, value) for variable, value in starts
            if variable.causality == STRUCTURAL
        ]
        if structural:
            self._call("fmi3EnterConfigurationMode")
            self._write_start_values(structural)
            self._call("fmi3ExitConfigurationMode")
        self._write_start_values([
            (variable, value) for variable, value in starts
            if variable.causality != STRUCTURAL
        ])

    def _write_start_values(self, starts) -> None:
        for variable, value in starts:
            references = _references(variable.reference)
            if variable.kind == BINARY:
                buffer = (ctypes.c_char * len(value)).from_buffer_copy(value)
                sizes = (ctypes.c_size_t * 1)(len(value))
                values = (ctypes.c_void_p * 1)(ctypes.addressof(buffer))
                self._set_binary(references, sizes, values)
            else:
                values = (_ELEMENTS[variable.kind] * 1)(value)
                self._set_values(variable.kind, references, values)

    def _set_values(self, kind: str, references, values) -> None:
        self._call(
            f"fmi3Set{kind}", references, len(references), values, len(values)
        )

    def _get_values(self, kind: str, references, values) -> None:
        self._call(
            f"fmi3Get{kind}", references, len(references), values, len(values)
        )

    def _set_binary(self, references, sizes, values) -> None:
        self._call(
            "fmi3SetBinary", references, len(references), sizes, values,
            len(references),
        )

    def _get_binary(self, references, sizes, values) -> None:
        self._call(
            "fmi3GetBinary", references, len(references), sizes, values,
            len(references),
        )

    def do_step(self, communication_point: float, step_size: float) -> bool:
        """Advance over one interval, and say whether it ended in an event.

        An early return is refused rather than accepted: this importer
        declares `earlyReturnAllowed` false, so an FMU that returns before the
        end of the interval has left part of it unstepped, and treating that
        as a completed interval would date everything after it wrongly.
        """
        event_needed = ctypes.c_bool()
        terminate = ctypes.c_bool()
        early_return = ctypes.c_bool()
        last_successful_time = ctypes.c_double()
        self._call(
            "fmi3DoStep", communication_point, step_size, True,
            ctypes.byref(event_needed), ctypes.byref(terminate),
            ctypes.byref(early_return), ctypes.byref(last_successful_time),
        )
        if terminate.value:
            raise ParticipantFailure(
                "fmi3DoStep requested termination via terminateSimulation"
            )
        if early_return.value:
            raise ParticipantFailure(
                f"fmi3DoStep returned early at {last_successful_time.value} s, "
                f"leaving the interval [{communication_point}, "
                f"{communication_point + step_size}] s incomplete; this "
                f"importer declares earlyReturnAllowed false"
            )
        return bool(event_needed.value)

    def close(self) -> None:
        """Terminate the instance and free it, even if terminating failed.

        The instance holds the FMU's memory either way, so the free is the
        half the failing path needs most — unless the FMU answered Fatal, which
        bars the free along with every other call.
        """
        if self._instance is None:
            return
        try:
            self._call("fmi3Terminate")
        finally:
            # `_call` drops the handle when a call answers Fatal, and freeing
            # is itself a call this FMU may no longer take.
            if self._instance is not None:
                self._library["fmi3FreeInstance"](self._instance)
                self._instance = None

    def _call(self, name: str, *arguments) -> None:
        """Invoke one co-simulation entry point on this instance.

        Every entry point takes the instance first and answers an fmi3Status,
        so naming it once here keeps the name in the diagnostic the same name
        that was called.
        """
        status = self._library[name](self._instance, *arguments)
        if status == _FMI_SUCCESS_STATUS:
            return
        if status == _FMI_FATAL_STATUS:
            # FMI 3.0 allows no further call on an instance that answered
            # Fatal, terminating and freeing it included. Dropping the handle
            # here is what stops `close` from calling into a dead FMU; the
            # instance's memory goes when this process does.
            self._instance = None
        raise ParticipantFailure(f"{name} returned {_status_name(status)}")


def _copied_out(length: int, address) -> bytes:
    """One Binary value, out of the pointer the FMU answered with.

    The pointer is the FMU's own and valid only until its next call, so the
    bytes are copied at once rather than held.
    """
    return ctypes.string_at(address, length) if length else b""


class ScalarBuffer:
    """The value buffer for several variables of one scalar type.

    One buffer is one FMI call however many variables it carries, and both the
    references and the values are built once because they are the same on
    every step. What crosses is Python numbers in the order the references
    were given; the FMI element they are held as is this buffer's business.
    """

    def __init__(self, kind: str, references: Sequence[int]):
        self._kind = kind
        self._references = _references(*references)
        self._values = (_ELEMENTS[kind] * len(references))()

    def write(self, fmu: CoSimulation, values: Sequence) -> None:
        self._values[:] = values
        fmu._set_values(self._kind, self._references, self._values)

    def read(self, fmu: CoSimulation) -> list:
        fmu._get_values(self._kind, self._references, self._values)
        return list(self._values)


class BinaryBuffer:
    """The arrays several Binary variables are read or written through.

    A buffer writes or reads, never both, because the Channel's direction
    decides which and it is built knowing that. A written one is given each
    variable's capacity and allocates a buffer of it, owned for as long as
    this object lives. A read one is given no capacity and allocates nothing —
    the pointers it hands over are filled by the FMU, are valid only until its
    next call, and so are copied out at once.
    """

    def __init__(self, references: Sequence[int],
                 capacities: Sequence[int] = ()):
        self._references = _references(*references)
        self._sizes = (ctypes.c_size_t * len(references))()
        self._pointers = (ctypes.c_void_p * len(references))()
        self._buffers = [
            (ctypes.c_char * capacity)() for capacity in capacities
        ]
        for index, buffer in enumerate(self._buffers):
            self._pointers[index] = ctypes.addressof(buffer)

    def write(self, fmu: CoSimulation, payloads: Sequence[bytes]) -> None:
        for index, payload in enumerate(payloads):
            self._buffers[index][:len(payload)] = payload
            self._sizes[index] = len(payload)
        fmu._set_binary(self._references, self._sizes, self._pointers)

    def read(self, fmu: CoSimulation) -> list[bytes]:
        fmu._get_binary(self._references, self._sizes, self._pointers)
        return [
            _copied_out(self._sizes[index], self._pointers[index])
            for index in range(len(self._sizes))
        ]


class _Flag:
    """One Clock's value reference, and the flag its two accessors take."""

    def __init__(self, clock: Variable):
        self._references = _references(clock.reference)
        self._value = (ctypes.c_bool * 1)()

    def read(self, fmu: CoSimulation) -> bool:
        fmu._get_clock(self._references, self._value)
        return bool(self._value[0])

    def activate(self, fmu: CoSimulation) -> None:
        self._value[0] = True
        fmu._set_clock(self._references, self._value)


class ClockedBuffer:
    """The FMU side of one Binary variable gated by a triggered Clock.

    A clocked Binary variable is defined only while its Clock is active, so
    neither end of it belongs to a Step: the Clock is read, or raised, inside
    an event. Reading and raising are the same rule seen from both ends —
    Clock first, buffer second.
    """

    def __init__(self, variable: Variable, clock: Variable, capacity: int,
                 *, incoming: bool):
        self.variable = variable
        self._clock = _Flag(clock)
        self._payload = BinaryBuffer(
            [variable.reference], (capacity,) if incoming else ()
        )

    def read(self, fmu: CoSimulation) -> bytes | None:
        """The Clock, and the buffer it gates when it reads active.

        The Clock is read exactly once here, because an FMU clears it on the
        read: reading it twice would lose the activation, and not reading it
        at all would hand on a buffer no activation stands behind.
        """
        if not self._clock.read(fmu):
            return None
        return self._payload.read(fmu)[0]

    def deliver(self, fmu: CoSimulation, payload: bytes) -> None:
        """Raise the Clock, then hand the FMU the payload it gates.

        That order is the contract, not a preference: a clocked variable may
        be accessed only while its Clock is active, and an FMU that checks
        refuses a buffer written before the Clock went up.
        """
        self._clock.activate(fmu)
        self._payload.write(fmu, [payload])


class CountdownBuffer:
    """The FMU side of one output Binary gated by a countdown input Clock.

    A countdown Clock inverts who decides when: the FMU states an interval
    after every event, and the activation at the end of it is the importer's
    to make. The buffer is defined for that activation exactly as a triggered
    Clock's is, and is read straight after the Clock goes up — before any
    discrete-state update, which is where an FMU clears it again.

    Only a group of connected FMUs can promise such an activation, because the
    instant the interval ends is one the kernel's Slot grid has no reason to
    contain; the group owns the communication points between two Slots.
    """

    def __init__(self, variable: Variable, clock: Variable):
        self.variable = variable
        self.clock = clock
        self._interval_references = _references(clock.reference)
        self._counters = (ctypes.c_uint64 * 1)()
        self._resolutions = (ctypes.c_uint64 * 1)()
        self._qualifiers = (ctypes.c_int32 * 1)()
        self._payload = BinaryBuffer([variable.reference])
        # The interval last stated, and the instant it ends at. They are kept
        # apart because an interval outlives the activation it caused: a
        # Clock the FMU leaves `Unchanged` after an activation is asking for
        # the same interval again, and one it never stated asks for nothing.
        self._interval_ns: int | None = None
        self.due_ns: int | None = None

    def refresh(self, fmu: CoSimulation, now_ns: int) -> None:
        """Read what the FMU asks for next, at the end of one of its events."""
        fmu._interval_fraction(
            self._interval_references, self._counters, self._resolutions,
            self._qualifiers,
        )
        qualifier = self._qualifiers[0]
        if qualifier == _INTERVAL_NOT_YET_KNOWN:
            self._interval_ns = None
            self.due_ns = None
        elif qualifier == _INTERVAL_CHANGED:
            self._interval_ns = self._exact_ns()
            self.due_ns = now_ns + self._interval_ns
        elif qualifier == _INTERVAL_UNCHANGED:
            if self.due_ns is None and self._interval_ns is not None:
                self.due_ns = now_ns + self._interval_ns
        else:
            raise ParticipantFailure(
                f"fmi3GetIntervalFraction answered qualifier {qualifier} for "
                f"Clock {self.clock.name!r}, which is none of "
                f"{', '.join(_INTERVAL_QUALIFIERS)}"
            )

    def _exact_ns(self) -> int:
        """The stated interval in the kernel's own nanoseconds.

        The fraction the FMU states is exact, and the Slot grid counts whole
        nanoseconds. An interval that falls between two of them is refused
        rather than rounded: the activation would happen at an instant the FMU
        did not ask for, and every event time after it would say so.
        """
        counter, resolution = self._counters[0], self._resolutions[0]
        if resolution == 0:
            raise ParticipantFailure(
                f"fmi3GetIntervalFraction stated resolution 0 for Clock "
                f"{self.clock.name!r}; an interval is counter over resolution"
            )
        nanoseconds, remainder = divmod(counter * NS_PER_S, resolution)
        if remainder:
            raise ParticipantFailure(
                f"Clock {self.clock.name!r} asks for an interval of "
                f"{counter}/{resolution} s, which is no whole number of "
                f"nanoseconds; this importer does not round an activation "
                f"onto an instant the FMU did not ask for"
            )
        return nanoseconds

    def take(self, fmu: CoSimulation) -> bytes:
        """The buffer the activation just made carries, and it is spent."""
        self.due_ns = None
        return self._payload.read(fmu)[0]
