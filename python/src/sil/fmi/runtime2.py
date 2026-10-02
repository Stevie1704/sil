"""The native side of an FMI 2.0 FMU: the library, the instance, the calls.

The FMI 2.0 profile is co-simulation with scalar Real, Integer and Boolean
variables on a fixed communication step, and this module is every `fmi2*`
entry point of it. `CoSimulation2` answers the calls a single FMU's
participant and its buffers make of `runtime.CoSimulation`, so the mapping,
the buffers and the participant are the FMI 3.0 ones: a buffer holds values
as the FMI 3.0 element of the same width, and only a Boolean, which FMI 2.0
holds as a C `int`, is converted on its way across.

An OSMP binary variable is passed by address in three of those Integers, and
`OsmpBuffer` is the memory behind it: every raw pointer of FMI 2.0 is here.

The lifecycle is instantiate, set the start values, set up the experiment,
enter and exit initialization mode, one `fmi2DoStep` per Step, terminate and
free. FMI 2.0 has no Configuration Mode, no Event Mode and no Clocks, so none
of that is here.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys
from pathlib import Path

from sil.participant import ParticipantFailure

from sil.fmi.description import (
    OSMP_INTEGER,
    ModelDescription,
    OsmpAddress,
    Variable,
)
from sil.fmi.runtime import _Library, _references

# fmi2Status. OK and Warning continue: FMI 2.0 defines Warning as a call that
# succeeded with something worth reporting, so the Run goes on and says so.
# Every other status fails the Run and names the call.
_STATUS_NAMES = ("OK", "Warning", "Discard", "Error", "Fatal", "Pending")
_OK, _WARNING, _FATAL, _PENDING = 0, 1, 4, 5

# fmi2Type: this profile instantiates the co-simulation interface only.
_CO_SIMULATION = 1

# The logger is variadic in C. ctypes cannot declare that, so the message is
# taken as the FMU passes it, before any formatting of its arguments; that is
# what FMPy does too.
_LOGGER = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
    ctypes.c_char_p,
)


class _Callbacks(ctypes.Structure):
    """`fmi2CallbackFunctions`: a logger, an allocator and its free.

    Those three are required. `stepFinished` is for asynchronous stepping,
    which this profile does not support, so it stays NULL.
    """

    _fields_ = [
        ("logger", _LOGGER),
        ("allocateMemory", ctypes.c_void_p),
        ("freeMemory", ctypes.c_void_p),
        ("stepFinished", ctypes.c_void_p),
        ("componentEnvironment", ctypes.c_void_p),
    ]


# The C library's calloc and free are the allocator FMI 2.0 asks for: the
# FMU allocates with one and frees with the other, and neither has to cross
# back into Python.
_LIBC = ctypes.CDLL(ctypes.util.find_library("c"))

_VALUE_REFERENCES = ctypes.POINTER(ctypes.c_uint32)

# The FMI 2.0 accessor and native element of each mapped kind. The kinds are
# the FMI 3.0 names `description.py` gives the FMI 2.0 types.
_ACCESSORS = {
    "Float64": ("Real", ctypes.c_double),
    "Int32": ("Integer", ctypes.c_int),
    "Boolean": ("Boolean", ctypes.c_int),
}


def _signatures() -> dict:
    signatures = {
        "fmi2Instantiate": (
            ctypes.c_void_p,
            [ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p,
             ctypes.POINTER(_Callbacks), ctypes.c_int, ctypes.c_int],
        ),
        "fmi2SetupExperiment": (
            ctypes.c_int,
            [ctypes.c_void_p, ctypes.c_int, ctypes.c_double, ctypes.c_double,
             ctypes.c_int, ctypes.c_double],
        ),
        "fmi2EnterInitializationMode": (ctypes.c_int, [ctypes.c_void_p]),
        "fmi2ExitInitializationMode": (ctypes.c_int, [ctypes.c_void_p]),
        "fmi2DoStep": (
            ctypes.c_int,
            [ctypes.c_void_p, ctypes.c_double, ctypes.c_double, ctypes.c_int],
        ),
        "fmi2Terminate": (ctypes.c_int, [ctypes.c_void_p]),
        "fmi2FreeInstance": (None, [ctypes.c_void_p]),
    }
    for name, element in _ACCESSORS.values():
        accessor = (
            ctypes.c_int,
            [ctypes.c_void_p, _VALUE_REFERENCES, ctypes.c_size_t,
             ctypes.POINTER(element)],
        )
        signatures[f"fmi2Get{name}"] = accessor
        signatures[f"fmi2Set{name}"] = accessor
    return signatures


_SIGNATURES = _signatures()


def _log_to_stderr(environment, instance, status, category, message) -> None:
    """The FMU's logger. stdout is the step protocol, so it cannot go there."""
    text = b"" if message is None else message
    print(f"fmu: {text.decode(errors='replace')}", file=sys.stderr)


def _status_name(status: int) -> str:
    if 0 <= status < len(_STATUS_NAMES):
        return _STATUS_NAMES[status]
    return f"unknown status {status}"


class CoSimulation2:
    """One instantiated FMI 2.0 FMU, driven through its co-simulation calls."""

    def __init__(self, binary: Path, description: ModelDescription,
                 *, resources: Path):
        self._library = _Library(binary, _SIGNATURES)
        # The FMU may call these for the life of the instance, so the
        # trampoline and the structure have to outlive this constructor.
        self._callbacks = _Callbacks(
            logger=_LOGGER(_log_to_stderr),
            allocateMemory=ctypes.cast(_LIBC.calloc, ctypes.c_void_p),
            freeMemory=ctypes.cast(_LIBC.free, ctypes.c_void_p),
        )
        # Set when a call fails: the instance is then freed without being
        # terminated.
        self._failed = False
        self._instance = self._library["fmi2Instantiate"](
            description.model_identifier.encode(),
            _CO_SIMULATION,
            description.instantiation_token.encode(),
            # FMI 2.0 names the resources directory by URI, whether or not
            # the archive carries one.
            resources.resolve().as_uri().encode(),
            ctypes.byref(self._callbacks),
            False,  # visible
            True,   # loggingOn
        )
        if not self._instance:
            raise ParticipantFailure(
                f"fmi2Instantiate returned no instance for "
                f"{description.model_identifier!r}"
            )

    def apply_start_values(self, starts: list[tuple[Variable, object]]) -> None:
        """Write the declared start values, before the experiment is set up.

        FMI 2.0 has an importer set a parameter's value in the instantiated
        state, so every value is in place before initialization mode is left.
        """
        for variable, value in starts:
            try:
                self._set_values(
                    variable.kind, _references(variable.reference),
                    (_ACCESSORS[variable.kind][1] * 1)(value),
                )
            except ParticipantFailure as error:
                raise ParticipantFailure(
                    f"start value for FMU variable {variable.name!r}, written "
                    f"in the instantiated state, before initialization: "
                    f"{error}"
                ) from error

    def initialize(self) -> None:
        """Set up the experiment at virtual time zero and initialize.

        No tolerance and no stop time are declared, for the reason the FMI 3.0
        instance gives: the Manifest's duration is the kernel's.
        """
        self._call("fmi2SetupExperiment", False, 0.0, 0.0, False, 0.0)
        self._call("fmi2EnterInitializationMode")
        self._call("fmi2ExitInitializationMode")

    def do_step(self, communication_point: float, step_size: float) -> bool:
        """Step the FMU over one interval. FMI 2.0 has no event to report."""
        self._call("fmi2DoStep", communication_point, step_size, True)
        return False

    def _set_values(self, kind: str, references, values) -> None:
        name, element = _ACCESSORS[kind]
        self._call(
            f"fmi2Set{name}", references, len(references),
            _as(element, values),
        )

    def _get_values(self, kind: str, references, values) -> None:
        name, element = _ACCESSORS[kind]
        native = _as(element, values)
        self._call(f"fmi2Get{name}", references, len(references), native)
        if native is not values:
            values[:] = list(native)

    def close(self) -> None:
        """Terminate and free the instance, even if terminating failed.

        After a failing status the instance is freed without being
        terminated, and after Fatal it is not called again at all.
        """
        if self._instance is None:
            return
        try:
            if not self._failed:
                self._call("fmi2Terminate")
        finally:
            if self._instance is not None:
                self._library["fmi2FreeInstance"](self._instance)
                self._instance = None

    def _call(self, name: str, *arguments) -> None:
        """Invoke one entry point on this instance and apply the status rules."""
        status = self._library[name](self._instance, *arguments)
        if status == _OK:
            return
        if status == _WARNING:
            print(
                f"sil.fmi: {name} returned Warning; the FMI 2.0 profile "
                f"continues the Run",
                file=sys.stderr,
            )
            return
        if status == _FATAL:
            # Like FMI 3.0, FMI 2.0 allows no further call after Fatal.
            self._instance = None
        else:
            self._failed = True
        reason = (
            "; asynchronous fmi2DoStep is not supported"
            if status == _PENDING else ""
        )
        raise ParticipantFailure(f"{name} returned {_status_name(status)}{reason}")


# One half of an OSMP address: the low or high 32 bits, held in an
# fmi2Integer, a signed C `int`.
_HALF = 0xFFFF_FFFF


def _signed(half: int) -> int:
    """32 bits as the signed C `int` an fmi2Integer holds."""
    return half - (1 << 32) if half & 0x8000_0000 else half


class OsmpBuffer:
    """The memory one OSMP binary variable is passed through, by address.

    A buffer writes or reads, never both, because the Channel's direction
    decides which. A written one owns a buffer of the Channel's bound for as
    long as this object lives, and hands the FMU its address. The bytes
    there stay as they were written until the next write, so they are valid
    for the whole `fmi2DoStep` and after it, as OSMP requires.

    A read one hands over nothing: the address and size are the FMU's own,
    valid only until its next `fmi2DoStep`, so they are read once after the
    step and the bytes are copied at once. The size is checked against the
    Channel's bound before anything is copied, so an oversized output fails
    the Run rather than being truncated or read past.
    """

    def __init__(self, name: str, address: OsmpAddress, capacity: int,
                 *, incoming: bool):
        self._name = name
        self._capacity = capacity
        self._references = _references(address.lo, address.hi, address.size)
        self._integers = (ctypes.c_int * 3)()
        self._buffer = (ctypes.c_char * max(capacity, 1))() if incoming else None

    def write(self, fmu: CoSimulation2, payload: bytes) -> None:
        ctypes.memmove(self._buffer, payload, len(payload))
        address = ctypes.addressof(self._buffer)
        self._integers[:] = [
            _signed(address & _HALF), _signed(address >> 32 & _HALF),
            len(payload),
        ]
        fmu._set_values(OSMP_INTEGER, self._references, self._integers)

    def read(self, fmu: CoSimulation2) -> bytes:
        fmu._get_values(OSMP_INTEGER, self._references, self._integers)
        lo, hi, size = self._integers
        address = (hi & _HALF) << 32 | lo & _HALF
        if size < 0:
            raise ParticipantFailure(
                f"OSMP binary variable {self._name!r} reports size {size}; "
                f"a size counts bytes"
            )
        if size > self._capacity:
            raise ParticipantFailure(
                f"OSMP binary variable {self._name!r} reports {size} bytes; "
                f"the Channel carries {self._capacity}, and nothing is "
                f"truncated"
            )
        if size and not address:
            raise ParticipantFailure(
                f"OSMP binary variable {self._name!r} reports {size} bytes "
                f"at address 0"
            )
        return ctypes.string_at(address, size) if size else b""


def _as(element, values):
    """The values as an array of `element`, converted only when they differ.

    A Boolean buffer holds C `bool`s and FMI 2.0 takes `int`s; every other
    mapped kind is already the element its call takes.
    """
    if values._type_ is element:
        return values
    return (element * len(values))(*values)
