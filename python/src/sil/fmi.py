"""Importer for FMI 3.0 co-simulation FMUs, run as a process participant.

The kernel learns nothing about FMI. A Manifest declares this module as a
process participant's command with an FMU path, and the step protocol drives
the FMU's co-simulation interface: its input variables are written from the
subscribed Channels, `fmi3DoStep` advances it, and its output variables are
published on the Channels it publishes.

A Float64 mapping needs no configuration beyond the Manifest. The
initialization line carries each Channel's schema and its direction, so a
Channel's schema field names are the FMU variable names and the direction
decides which side of the step the variable is touched on.

Every other variable is *declared* rather than derived, because the FMU's
vocabulary and the Channel's are not the same one and a schema is typically
carried by more than one Channel:

    python -m sil.fmi <model.fmu> --bind <channel>:<field>=<variable> ...
                                  --start <variable>=<value> ...

Declaring one binding declares them all: the bindings are then the whole
mapping, and a Channel field that none of them names is a mapping mistake
rather than a variable left at zero.

A Binary variable is variable-length and a Message is not, so a Channel
carries a Binary value in an explicit bounded representation: a `u8` array
field holding the payload and the `<field>_length` field beside it holding how
much of it is the payload. The bytes above that length are zero, on every
Message. Nothing is ever truncated to fit — a payload above the bound aborts
the Run.

Both the bindings and the start values travel as command arguments, which the
Manifest already hashes, so nothing that affects the Run lives outside the
hashed Manifest.
"""

from __future__ import annotations

import argparse
import ctypes
import platform
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable, Sequence
from xml.etree import ElementTree

from sil.participant import (
    ManifestError,
    ParticipantFailure,
    StepParticipant,
    run,
)

# Virtual time is integer nanoseconds; seconds are derived from those integers
# on every step and never accumulated, so the same integer always produces the
# same double. An integer denominator keeps that true past the 53 bits an
# int-to-float conversion holds exactly.
NS_PER_S = 1_000_000_000

# The one FMI version this importer drives. Anything else is rejected rather
# than half-driven.
_FMI_VERSION = "3.0"

# fmi3Status. Only OK means the call succeeded; every other status aborts the
# Run before the importer can continue with a possibly invalid FMU state.
_FMI_STATUS_NAMES = ("OK", "Warning", "Discard", "Error", "Fatal")
_FMI_SUCCESS_STATUS = 0
_FMI_FATAL_STATUS = 4

# The FMU's `binaries/` subdirectory for the running platform, and the shared
# library suffix that goes with it.
_MACHINES = {"arm64": "aarch64", "AMD64": "x86_64"}
_SYSTEMS = {"Darwin": ("darwin", ".dylib"), "Linux": ("linux", ".so")}


def _integer(text: str) -> int:
    """A start value written in any base Python spells, `0x2a` included."""
    return int(text, 0)


def _boolean(text: str) -> int:
    """A start value in the spelling `modelDescription.xml` uses."""
    if text not in ("true", "false"):
        raise ValueError(f"{text!r} is not 'true' or 'false'")
    return int(text == "true")


@dataclass(frozen=True)
class _ScalarType:
    """One FMI scalar type, as both ends of the mapping see it.

    `field_type` is the one schema field type that carries it — a Channel
    field of any other type is a binding this importer refuses rather than
    reinterprets. `element` is the ctypes element the FMI call takes,
    `to_field` the value as the schema carries it, and `parse` reads a start
    value out of a command argument.
    """

    field_type: str
    element: type
    to_field: Callable
    parse: Callable


# Every FMI variable type this importer maps, by the element name
# `modelDescription.xml` gives it. String, Enumeration and Clock are outside
# it deliberately: a binding that names one is reported rather than skipped.
_SCALARS = {
    "Float32": _ScalarType("f32", ctypes.c_float, float, float),
    "Float64": _ScalarType("f64", ctypes.c_double, float, float),
    "Int8": _ScalarType("i8", ctypes.c_int8, int, _integer),
    "UInt8": _ScalarType("u8", ctypes.c_uint8, int, _integer),
    "Int16": _ScalarType("i16", ctypes.c_int16, int, _integer),
    "UInt16": _ScalarType("u16", ctypes.c_uint16, int, _integer),
    "Int32": _ScalarType("i32", ctypes.c_int32, int, _integer),
    "UInt32": _ScalarType("u32", ctypes.c_uint32, int, _integer),
    "Int64": _ScalarType("i64", ctypes.c_int64, int, _integer),
    "UInt64": _ScalarType("u64", ctypes.c_uint64, int, _integer),
    # fmi3Boolean is a C `bool`; a Channel carries it as the byte 0 or 1.
    "Boolean": _ScalarType("u8", ctypes.c_bool, int, _boolean),
}

_BINARY = "Binary"
_FLOAT64 = "Float64"

# A bounded payload's length is a count of bytes, so a signed field would
# admit a length no payload can have.
_LENGTH_TYPES = ("u8", "u16", "u32", "u64")

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

def _scalar_signatures() -> dict:
    """Get and Set for every scalar type: one shape, named once per type."""
    signatures = {}
    for kind, scalar in _SCALARS.items():
        accessor = (
            ctypes.c_int,
            [ctypes.c_void_p, _VALUE_REFERENCES, ctypes.c_size_t,
             ctypes.POINTER(scalar.element), ctypes.c_size_t],
        )
        signatures[f"fmi3Get{kind}"] = accessor
        signatures[f"fmi3Set{kind}"] = accessor
    return signatures


_SIGNATURES.update(_scalar_signatures())


def platform_directory() -> str:
    """The `binaries/` subdirectory this platform's shared library lives in."""
    machine = platform.machine()
    machine = _MACHINES.get(machine, machine)
    system, _ = _SYSTEMS[platform.system()]
    return f"{machine}-{system}"


def library_suffix() -> str:
    """The shared-library suffix this platform's FMU binary carries."""
    _, suffix = _SYSTEMS[platform.system()]
    return suffix


@dataclass(frozen=True)
class Variable:
    """One FMU variable, as `modelDescription.xml` declares it."""

    name: str
    reference: int
    # The element name the description uses: 'Float64', 'Binary', 'Clock'…
    kind: str
    causality: str
    # Declared by a Binary variable only, and the most the FMU will accept.
    max_size: int | None
    dimensions: int


def _by_causality(variables: dict[str, Variable], causality: str) -> dict[str, int]:
    """The Float64 variables of one causality, by name.

    These are the variables a mapping is *derived* from. Only `input` and
    `output` variables take part in it; a parameter, a local, or the
    independent variable `time` is the FMU's own business and no Channel
    names it.
    """
    return {
        variable.name: variable.reference
        for variable in variables.values()
        if variable.kind == _FLOAT64 and variable.causality == causality
    }


def _variables(root) -> dict[str, Variable]:
    """Every variable the description declares, of every type."""
    declared: dict[str, Variable] = {}
    for element in root.find("ModelVariables"):
        name, reference = element.get("name"), element.get("valueReference")
        if name is None or reference is None:
            continue
        max_size = element.get("maxSize")
        declared[name] = Variable(
            name=name,
            reference=int(reference),
            kind=element.tag,
            causality=element.get("causality", "local"),
            max_size=None if max_size is None else int(max_size),
            dimensions=len(element.findall("Dimension")),
        )
    return declared


@dataclass(frozen=True)
class ModelDescription:
    """What `modelDescription.xml` says that driving the FMU depends on."""

    model_identifier: str
    instantiation_token: str
    variables: dict[str, Variable]
    inputs: dict[str, int]
    outputs: dict[str, int]

    @staticmethod
    def read(extracted: Path) -> ModelDescription:
        """Parse the description, rejecting an FMU this importer cannot drive.

        Both rejections are eager and specific: an FMI 2.0 export otherwise
        loads and fails on a missing symbol, and a Model Exchange FMU
        otherwise fails on an absent element.
        """
        try:
            root = ElementTree.parse(
                extracted / "modelDescription.xml"
            ).getroot()
        except (OSError, ElementTree.ParseError) as error:
            raise ManifestError(
                f"FMU has no readable modelDescription.xml: {error}"
            ) from error
        version = root.get("fmiVersion")
        if version != _FMI_VERSION:
            raise ManifestError(
                f"FMU declares fmiVersion {version!r}; this importer drives "
                f"FMI {_FMI_VERSION} co-simulation only"
            )
        co_simulation = root.find("CoSimulation")
        if co_simulation is None:
            raise ManifestError(
                "FMU declares no co-simulation interface; this importer "
                "drives neither Model Exchange nor Scheduled Execution"
            )
        variables = _variables(root)
        return ModelDescription(
            model_identifier=co_simulation.get("modelIdentifier"),
            instantiation_token=root.get("instantiationToken"),
            variables=variables,
            inputs=_by_causality(variables, "input"),
            outputs=_by_causality(variables, "output"),
        )

    def binary(self, extracted: Path) -> Path:
        """The shared library this platform loads out of the FMU."""
        directory = platform_directory()
        binary = (
            extracted / "binaries" / directory
            / f"{self.model_identifier}{library_suffix()}"
        )
        if not binary.exists():
            raise ManifestError(
                f"FMU {self.model_identifier!r} carries no binary for "
                f"{directory}: binaries/{directory}/{binary.name} is not in "
                f"the archive"
            )
        return binary


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


class CoSimulation:
    """One instantiated FMU, driven through its co-simulation entry points.

    The instance owns its internal state and is given a communication point
    and a step size, which is the contract the kernel already offers a process
    participant — this class is the translation between the two, and nothing
    above it deals in ctypes.
    """

    def __init__(self, binary: Path, description: ModelDescription):
        self._library = _Library(binary)
        # The FMU calls this for the life of the instance, so the ctypes
        # trampoline has to outlive this constructor.
        self._logger = _LOG_CALLBACK(_log_to_stderr)
        self._instance = self._library["fmi3InstantiateCoSimulation"](
            description.model_identifier.encode(),
            description.instantiation_token.encode(),
            None,
            False,  # visible
            True,   # loggingOn
            False,  # eventModeUsed
            False,  # earlyReturnAllowed
            None, 0,
            None, self._logger, None,
        )
        if not self._instance:
            raise ParticipantFailure(
                f"fmi3InstantiateCoSimulation returned no instance for "
                f"{description.model_identifier!r}"
            )

    def initialize(self) -> None:
        """Enter and leave initialization mode, leaving the FMU steppable.

        No stop time is declared: the Manifest's duration is the kernel's, and
        an FMU told a stop time it never reaches would reject the last step.
        """
        self._call("fmi3EnterInitializationMode", False, 0.0, 0.0, False, 0.0)
        self._call("fmi3ExitInitializationMode")

    def apply_start_values(self, starts: list[tuple[Variable, object]]) -> None:
        """Write the declared start values, in the order they were declared.

        This runs in the instantiated state, before initialization mode, which
        is where FMI 3.0 has an importer override the start values a
        description declares.
        """
        for variable, value in starts:
            references = _references(variable.reference)
            if variable.kind == _BINARY:
                buffer = (ctypes.c_char * len(value)).from_buffer_copy(value)
                sizes = (ctypes.c_size_t * 1)(len(value))
                values = (ctypes.c_void_p * 1)(ctypes.addressof(buffer))
                self.set_binary(references, sizes, values)
            else:
                values = (_SCALARS[variable.kind].element * 1)(value)
                self.set_values(variable.kind, references, values)

    def set_values(self, kind: str, references, values) -> None:
        self._call(
            f"fmi3Set{kind}", references, len(references), values, len(values)
        )

    def get_values(self, kind: str, references, values) -> None:
        self._call(
            f"fmi3Get{kind}", references, len(references), values, len(values)
        )

    def set_binary(self, references, sizes, values) -> None:
        self._call(
            "fmi3SetBinary", references, len(references), sizes, values,
            len(references),
        )

    def get_binary(self, references, sizes, values) -> None:
        self._call(
            "fmi3GetBinary", references, len(references), sizes, values,
            len(references),
        )

    def do_step(self, communication_point: float, step_size: float) -> None:
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


class _ScalarGroup:
    """One Channel's fields bound to FMU variables of one scalar type.

    The value references and the value buffer are built once, at
    initialization, because they are the same on every step, and one group is
    one FMI call however many fields it carries.
    """

    def __init__(self, kind: str, bound: list[tuple[str, Variable]]):
        self._kind = kind
        self._scalar = _SCALARS[kind]
        self._fields = [field for field, _ in bound]
        self._references = _references(*(v.reference for _, v in bound))
        self._values = (self._scalar.element * len(bound))()

    def write(self, fmu: CoSimulation, fields: dict) -> None:
        self._values[:] = [fields[name] for name in self._fields]
        fmu.set_values(self._kind, self._references, self._values)

    def read(self, fmu: CoSimulation, into: dict) -> None:
        fmu.get_values(self._kind, self._references, self._values)
        to_field = self._scalar.to_field
        into.update(
            (name, to_field(value))
            for name, value in zip(self._fields, self._values)
        )


@dataclass(frozen=True)
class _BinaryField:
    """One Binary variable and the bounded Channel fields carrying it."""

    variable: Variable
    field: str
    length_field: str
    capacity: int


class _BinaryGroup:
    """One Channel's Binary variables, as bounded payloads with a length.

    The buffers the FMU is handed on a set are allocated once and live as long
    as this group: FMI 3.0 has the importer own them for the duration of the
    call, and reallocating one per step would put their lifetime in the
    garbage collector's hands. What the FMU hands back on a get points into
    its own memory and is valid until the next call, so it is copied here and
    then padded out to the Channel's bound.
    """

    def __init__(self, bound: list[_BinaryField]):
        self._bound = bound
        self._references = _references(*(b.variable.reference for b in bound))
        self._sizes = (ctypes.c_size_t * len(bound))()
        self._values = (ctypes.c_void_p * len(bound))()
        self._buffers = [(ctypes.c_char * b.capacity)() for b in bound]
        for index, buffer in enumerate(self._buffers):
            self._values[index] = ctypes.addressof(buffer)

    def write(self, fmu: CoSimulation, fields: dict) -> None:
        for index, binary in enumerate(self._bound):
            length = fields[binary.length_field]
            if length > binary.capacity:
                raise ParticipantFailure(
                    f"field {binary.length_field!r} declares {length} bytes, "
                    f"above the {binary.capacity} field {binary.field!r} "
                    f"carries for FMU variable {binary.variable.name!r}"
                )
            self._buffers[index][:length] = fields[binary.field][:length]
            self._sizes[index] = length
        fmu.set_binary(self._references, self._sizes, self._values)

    def read(self, fmu: CoSimulation, into: dict) -> None:
        fmu.get_binary(self._references, self._sizes, self._values)
        for index, binary in enumerate(self._bound):
            length = self._sizes[index]
            if length > binary.capacity:
                raise ParticipantFailure(
                    f"FMU variable {binary.variable.name!r} produced {length} "
                    f"bytes; field {binary.field!r} carries {binary.capacity}"
                )
            payload = (
                ctypes.string_at(self._values[index], length) if length else b""
            )
            into[binary.field] = payload + bytes(binary.capacity - length)
            into[binary.length_field] = length


class _ChannelBinding:
    """One Channel's fields, written into or read out of the FMU."""

    def __init__(self, groups: list):
        self._groups = groups

    def write(self, fmu: CoSimulation, fields: dict) -> None:
        for group in self._groups:
            group.write(fmu, fields)

    def read(self, fmu: CoSimulation) -> dict:
        fields: dict = {}
        for group in self._groups:
            group.read(fmu, fields)
        return fields


def _channel_fields(init: dict) -> dict[str, dict[str, dict]]:
    """Each Channel's declared schema fields, by name, in declaration order."""
    return {
        channel: {
            field["name"]: field
            for field in init["schemas"][declaration["schema"]]["fields"]
        }
        for channel, declaration in init["channels"].items()
    }


def _declarations(
    init: dict, fields_by_channel: dict[str, dict[str, dict]]
) -> dict[str, dict[str, str]]:
    """Each declared schema field name, mapped to the Channel that declared it.

    Split by the direction the init line gives, because the direction decides
    which side of the step the FMU variable of that name is touched on.
    """
    declared: dict[str, dict[str, str]] = {"in": {}, "out": {}}
    for channel, declaration in init["channels"].items():
        declared[declaration["direction"]].update(
            dict.fromkeys(fields_by_channel[channel], channel)
        )
    return declared


def _require_total_match(
    declared: dict[str, str],
    variables: dict[str, int],
    causality: str,
    model_identifier: str,
) -> None:
    """Require every name on one side of a derived mapping to be on the other.

    `declared` maps each schema field name to the Channel that declared it, so
    both diagnostics can name the Channel and the FMU.
    """
    for name, channel in declared.items():
        if name not in variables:
            raise ManifestError(
                f"Channel {channel!r} declares schema field {name!r}, which is "
                f"no {causality} variable of FMU {model_identifier!r}"
            )
    searched = ", ".join(sorted(repr(c) for c in set(declared.values())))
    for name in variables:
        if name not in declared:
            raise ManifestError(
                f"FMU {model_identifier!r} declares {causality} variable "
                f"{name!r}, which is a schema field of no {causality}-direction "
                f"Channel (searched: {searched or 'none'})"
            )


def _derived_bindings(
    init: dict,
    fields_by_channel: dict[str, dict[str, dict]],
    description: ModelDescription,
) -> dict[str, dict[str, Variable]]:
    """The Float64 mapping a Manifest needs no configuration for.

    The schema field names are the FMU variable names, and the match is total
    in both directions — a name on one side and not the other is a mapping
    mistake, which is worth rejecting rather than carrying as a silently
    zero-valued variable. A declared mapping states which variables take part;
    a derived one has to infer it, and totality is that inference.
    """
    declared = _declarations(init, fields_by_channel)
    _require_total_match(
        declared["in"], description.inputs, "input",
        description.model_identifier,
    )
    _require_total_match(
        declared["out"], description.outputs, "output",
        description.model_identifier,
    )
    return {
        channel: {field: description.variables[field] for field in fields}
        for channel, fields in fields_by_channel.items()
    }


def _declared_bindings(
    binds: list[str],
    fields_by_channel: dict[str, dict[str, dict]],
    description: ModelDescription,
) -> dict[str, dict[str, Variable]]:
    """Resolve `--bind <channel>:<field>=<variable>` against both ends.

    The Channel is named because one schema is typically carried by more than
    one Channel, so a field name alone names no single end of the mapping.
    """
    bound: dict[str, dict[str, Variable]] = {
        channel: {} for channel in fields_by_channel
    }
    for bind in binds:
        target, separator, variable_name = bind.partition("=")
        channel, colon, field = target.rpartition(":")
        if not (separator and colon and channel and field and variable_name):
            raise ManifestError(
                f"binding {bind!r} is not '<channel>:<field>=<variable>'"
            )
        if channel not in fields_by_channel:
            raise ManifestError(
                f"binding {bind!r} names Channel {channel!r}, which the "
                f"initialization line does not declare"
            )
        if field not in fields_by_channel[channel]:
            raise ManifestError(
                f"binding {bind!r} names schema field {field!r}, which Channel "
                f"{channel!r} does not carry"
            )
        if field in bound[channel]:
            raise ManifestError(
                f"binding {bind!r} binds Channel {channel!r} field {field!r} "
                f"twice; a field carries one FMU variable"
            )
        variable = description.variables.get(variable_name)
        if variable is None:
            raise ManifestError(
                f"binding {bind!r} names FMU variable {variable_name!r}, which "
                f"FMU {description.model_identifier!r} does not declare"
            )
        bound[channel][field] = variable
    return bound


def _shape(field: dict) -> str:
    """How a schema field reads in a diagnostic: its type and its bound."""
    count = field.get("count")
    if count is None:
        return f"a {field['type']!r} scalar"
    return f"a {field['type']!r} array of {count}"


def _require_mappable(channel: str, field: str, variable: Variable) -> None:
    """Reject a variable whose type or shape this importer does not map."""
    if variable.dimensions:
        raise ManifestError(
            f"Channel {channel!r} field {field!r} names FMU variable "
            f"{variable.name!r}, which declares dimensions; this importer maps "
            f"scalar and Binary variables only"
        )
    if variable.kind != _BINARY and variable.kind not in _SCALARS:
        raise ManifestError(
            f"Channel {channel!r} field {field!r} names FMU variable "
            f"{variable.name!r}, which is a {variable.kind} variable; this "
            f"importer maps Binary and the scalar types "
            f"{', '.join(_SCALARS)}"
        )


def _require_causality(
    channel: str, field: str, variable: Variable, causality: str
) -> None:
    """Reject a variable bound to a Channel of the other direction."""
    if variable.causality != causality:
        raise ManifestError(
            f"Channel {channel!r} field {field!r} names FMU variable "
            f"{variable.name!r}, whose causality is {variable.causality!r}; "
            f"this Channel's direction binds {causality} variables"
        )


def _require_field_type(
    channel: str, field: str, spec: dict, variable: Variable
) -> None:
    """Reject a field whose type is not the one the variable's type maps to."""
    scalar = _SCALARS[variable.kind]
    if spec.get("count") is not None or spec["type"] != scalar.field_type:
        raise ManifestError(
            f"Channel {channel!r} declares field {field!r} as {_shape(spec)}; "
            f"{variable.kind} variable {variable.name!r} is carried by a "
            f"{scalar.field_type!r} scalar"
        )


def _binary_field(
    channel: str,
    field: str,
    fields: dict[str, dict],
    variable: Variable,
    causality: str,
) -> _BinaryField:
    """The bounded representation a Channel carries one Binary variable in."""
    spec = fields[field]
    capacity = spec.get("count")
    if capacity is None or spec["type"] != "u8":
        raise ManifestError(
            f"Channel {channel!r} declares field {field!r} as {_shape(spec)}; "
            f"Binary variable {variable.name!r} is carried by a bounded 'u8' "
            f"array"
        )
    length_field = f"{field}_length"
    length = fields.get(length_field)
    if length is None:
        raise ManifestError(
            f"Channel {channel!r} carries no field {length_field!r}; a bounded "
            f"Binary payload carries the length it uses beside it"
        )
    if length.get("count") is not None or length["type"] not in _LENGTH_TYPES:
        raise ManifestError(
            f"Channel {channel!r} declares field {length_field!r} as "
            f"{_shape(length)}; a Binary payload's length is an unsigned "
            f"scalar ({', '.join(repr(t) for t in _LENGTH_TYPES)})"
        )
    if (
        causality == "input"
        and variable.max_size is not None
        and capacity > variable.max_size
    ):
        raise ManifestError(
            f"Channel {channel!r} field {field!r} carries {capacity} bytes, "
            f"but input variable {variable.name!r} declares maxSize "
            f"{variable.max_size}; the FMU would refuse every payload above it"
        )
    return _BinaryField(
        variable=variable, field=field, length_field=length_field,
        capacity=capacity,
    )


def _bind_channel(
    channel: str,
    direction: str,
    fields: dict[str, dict],
    bound: dict[str, Variable],
) -> _ChannelBinding:
    """One Channel's fields, checked against the variables they name."""
    causality = "input" if direction == "in" else "output"
    scalars: dict[str, list[tuple[str, Variable]]] = {}
    binaries: list[_BinaryField] = []
    lengths: dict[str, str] = {}
    for field in fields:
        variable = bound.get(field)
        if variable is None:
            continue
        _require_mappable(channel, field, variable)
        _require_causality(channel, field, variable, causality)
        if variable.kind == _BINARY:
            binary = _binary_field(channel, field, fields, variable, causality)
            lengths[binary.length_field] = field
            binaries.append(binary)
        else:
            _require_field_type(channel, field, fields[field], variable)
            scalars.setdefault(variable.kind, []).append((field, variable))
    for field in fields:
        if field in bound and field in lengths:
            raise ManifestError(
                f"Channel {channel!r} field {field!r} carries the length of "
                f"Binary variable {bound[lengths[field]].name!r} and is bound "
                f"to FMU variable {bound[field].name!r} as well"
            )
        if field not in bound and field not in lengths:
            raise ManifestError(
                f"Channel {channel!r} declares schema field {field!r}, which "
                f"no binding names an FMU variable for"
            )
    groups: list = [
        _ScalarGroup(kind, items) for kind, items in scalars.items()
    ]
    if binaries:
        groups.append(_BinaryGroup(binaries))
    return _ChannelBinding(groups)


def _start_value(variable: Variable, text: str):
    """One start value, read out of a command argument by its own type."""
    if variable.kind == _BINARY:
        try:
            return bytes.fromhex(text)
        except ValueError as error:
            raise ManifestError(
                f"start value for FMU variable {variable.name!r}: {text!r} is "
                f"not hexadecimal"
            ) from error
    if variable.kind not in _SCALARS:
        raise ManifestError(
            f"start value for FMU variable {variable.name!r}: it is a "
            f"{variable.kind} variable, which this importer does not set"
        )
    scalar = _SCALARS[variable.kind]
    try:
        value = scalar.parse(text)
    except ValueError as error:
        raise ManifestError(
            f"start value for FMU variable {variable.name!r}: cannot read "
            f"{text!r} as {variable.kind} ({error})"
        ) from error
    # ctypes narrows an out-of-range integer silently, which would start the
    # FMU at a value the Manifest does not state.
    held = (scalar.element * 1)()
    held[0] = value
    if isinstance(value, int) and held[0] != value:
        raise ManifestError(
            f"start value for FMU variable {variable.name!r}: {value} is out "
            f"of range for {variable.kind}"
        )
    return value


def _start_values(
    starts: list[str], description: ModelDescription
) -> list[tuple[Variable, object]]:
    """Resolve `--start <variable>=<value>` against the description."""
    values = []
    for start in starts:
        name, separator, text = start.partition("=")
        if not separator or not name:
            raise ManifestError(
                f"start value {start!r} is not '<variable>=<value>'"
            )
        variable = description.variables.get(name)
        if variable is None:
            raise ManifestError(
                f"start value {start!r} names FMU variable {name!r}, which FMU "
                f"{description.model_identifier!r} does not declare"
            )
        values.append((variable, _start_value(variable, text)))
    return values


class FmuParticipant(StepParticipant):
    """A process participant whose behavior is an imported FMU's."""

    def __init__(self, fmu_path: Path, *, binds: Sequence[str] = (),
                 starts: Sequence[str] = ()):
        self.name = ""  # the init line's, for the one diagnostic the kernel misses
        self._fmu_path = fmu_path
        self._binds = list(binds)
        self._starts = list(starts)
        self._extraction = None
        self._fmu = None
        self._inputs: dict[str, _ChannelBinding] = {}
        self._outputs: dict[str, _ChannelBinding] = {}

    def on_init(self, init: dict) -> None:
        self.name = init["name"]
        # The kernel starts this process in its kernel-owned Run working
        # directory, so the extracted archive is inside the tree the kernel
        # removes after reaping us. TemporaryDirectory's own cleanup is an
        # eager optimization for a cooperative shutdown, not the guarantee.
        self._extraction = tempfile.TemporaryDirectory(
            prefix="sil-fmu-", dir=Path.cwd()
        )
        extracted = Path(self._extraction.name)
        try:
            with zipfile.ZipFile(self._fmu_path) as archive:
                archive.extractall(extracted)
        except (OSError, zipfile.BadZipFile) as error:
            raise ManifestError(
                f"cannot read FMU {str(self._fmu_path)!r}: {error}"
            ) from error
        description = ModelDescription.read(extracted)
        # Everything the Manifest got wrong is rejected before the FMU is
        # instantiated: a mapping that cannot hold is a fact about the Run's
        # configuration, and no FMU has to be loaded to see it.
        self._bind_channels(init, description)
        starts = _start_values(self._starts, description)
        self._fmu = CoSimulation(description.binary(extracted), description)
        self._fmu.apply_start_values(starts)
        self._fmu.initialize()

    def _bind_channels(self, init: dict, description: ModelDescription) -> None:
        """Bind the declared Channels to FMU variables, in both directions.

        An input-direction Channel is written into the FMU before its step; an
        output-direction Channel is published from it after. Declaring one
        binding declares them all — the bindings are the whole mapping, and a
        Channel with none derives its own from the Float64 variable names.
        """
        fields_by_channel = _channel_fields(init)
        bound = (
            _declared_bindings(self._binds, fields_by_channel, description)
            if self._binds
            else _derived_bindings(init, fields_by_channel, description)
        )
        for channel, declaration in init["channels"].items():
            direction = declaration["direction"]
            bindings = self._inputs if direction == "in" else self._outputs
            bindings[channel] = _bind_channel(
                channel, direction, fields_by_channel[channel], bound[channel]
            )

    def on_step(self, t: int, dt: int, inputs: list):
        # Inputs arrive in publish order, so writing each in turn leaves the
        # newest Message on a Channel as the value the step sees.
        for message in inputs:
            self._inputs[message.channel].write(self._fmu, message.data)
        self._fmu.do_step(t / NS_PER_S, dt / NS_PER_S)
        return [
            (channel, binding.read(self._fmu))
            for channel, binding in self._outputs.items()
        ]

    def close(self) -> None:
        """Terminate and free the instance, and drop the extracted FMU.

        The diagnostic stays unframed: on every path the kernel can see, the
        kernel is what names the participant.
        """
        fmu, extraction = self._fmu, self._extraction
        self._fmu = None
        self._extraction = None
        try:
            if fmu is not None:
                fmu.close()
        finally:
            if extraction is not None:
                extraction.cleanup()


def _close_after_failure(participant: FmuParticipant) -> None:
    """Drop the FMU on the way out of a failure that is already reported.

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
    parser.add_argument("fmu", help="path to the FMU archive")
    parser.add_argument(
        "--bind", action="append", default=[], metavar="CHANNEL:FIELD=VARIABLE",
        help="bind one Channel schema field to one FMU variable; declaring "
             "any binding makes the bindings the whole mapping",
    )
    parser.add_argument(
        "--start", action="append", default=[], metavar="VARIABLE=VALUE",
        help="set one FMU variable before initialization mode is left; a "
             "Binary value is hexadecimal",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _arguments(sys.argv[1:] if argv is None else argv)
    participant = FmuParticipant(
        Path(args.fmu), binds=args.bind, starts=args.start
    )
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


if __name__ == "__main__":
    main()
