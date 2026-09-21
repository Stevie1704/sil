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
much of it is the payload. On every Message this importer publishes, the bytes
above that length are zero, so one Manifest records the same bytes twice; on
an incoming Message they are ignored, because the length is what says where
the payload ends. Nothing is ever truncated to fit — a payload above the
bound, in either direction, aborts the Run.

A Binary variable that declares a Clock is not a value the Step reads: it is
defined only while its Clock is active, which happens in Event Mode. Such a
Channel carries one Message per Clock activation and a third field,
`<field>_event_time_ns`, holding the FMI event time that activation belongs
to. That time is not the Channel's: a Message is published in the Slot the
importer's activation runs in, becomes visible to a subscriber one Latency
later, and states the FMI event time it carries rather than being timestamped
with it.

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
from collections import deque
from dataclasses import dataclass, field
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


# Every FMI scalar type this importer maps, by the element name
# `modelDescription.xml` gives it: the two the acceptance fixture declares
# beside its Binary variables, and no more. Broad type coverage is outside
# this slice, so every other type — String, Enumeration, Clock, and the
# integer types — is reported rather than skipped when a binding names one.
_SCALARS = {
    "Float64": _ScalarType("f64", ctypes.c_double, float, float),
    # fmi3Boolean is a C `bool`, so a Channel carries it as a `u8` with C's
    # own conversion: zero is false and any other value is true. What the FMU
    # hands back is 0 or 1.
    "Boolean": _ScalarType("u8", ctypes.c_bool, int, _boolean),
}

_BINARY = "Binary"
_FLOAT64 = "Float64"
_CLOCK = "Clock"

# The two Clock kinds this importer drives. A `triggered` Clock is raised by
# whoever owns the thing that happened — the FMU on an output Clock, the
# importer on an input one. A `countdown` Clock is the FMU asking to be
# activated at an instant it computes itself, which only a group can promise,
# because only a group owns communication points between the kernel's Slots. A
# `periodic` Clock asks an importer to own a second time grid and is outside
# the profile either way.
_TRIGGERED = "triggered"
_COUNTDOWN = "countdown"

# fmi3IntervalQualifier. `NotYetKnown` is the FMU saying it has no activation
# to ask for; `Unchanged` leaves the one already asked for standing;
# `Changed` states a new interval, counted from the event it was read in.
_INTERVAL_NOT_YET_KNOWN = 0
_INTERVAL_UNCHANGED = 1
_INTERVAL_CHANGED = 2
_INTERVAL_QUALIFIERS = ("NotYetKnown", "Unchanged", "Changed")

# The capability an FMU has to declare before this importer will use Event
# Mode, and the Clock profile is the only thing it is used for.
_HAS_EVENT_MODE = "hasEventMode"

# The field a clocked Channel carries beside the payload and its length: the
# FMI event time the activation belongs to, in the kernel's own nanoseconds.
_EVENT_TIME_SUFFIX = "_event_time_ns"
_EVENT_TIME_TYPE = "u64"

# How many times one event may ask for another discrete-state update before
# this importer stops asking. FMI 3.0 puts no bound on the iteration, and a
# Run that never leaves an event would hang until the response deadline
# without saying why; the bound is declared here so the failure is the same
# one on every machine.
_MAX_EVENT_ITERATIONS = 100

# How many times one instant may hand an activation from one connected FMU to
# another before this importer stops propagating. Two FMUs that answer each
# other at the same instant would otherwise never let the instant end; the
# bound is the same kind of declaration as the one above, at the group's own
# level rather than one FMU's.
_MAX_PROPAGATIONS = 100

# The FMI-LS-BUS layered standard, as an FMU's own files name it: the archive
# member that declares it, the terminal kind and matching rule a network
# terminal carries, and the four members a transceiver terminal groups.
_BUS_LAYERED_STANDARD = "org.fmi-standard.fmi-ls-bus"
_BUS_MANIFEST_MEMBER = f"extra/{_BUS_LAYERED_STANDARD}/fmi-ls-manifest.xml"
_LAYERED_STANDARD_NAMESPACE = "http://fmi-standard.org/fmi-ls-manifest"
_TERMINALS_MEMBER = "terminalsAndIcons/terminalsAndIcons.xml"
_NETWORK_TERMINAL = "org.fmi-ls-bus.network-terminal"
_TRANSCEIVER = "org.fmi-ls-bus.transceiver"
# The direction is the terminal owner's: `Tx_Data` is what it sends and
# `Rx_Data` is what it is handed, each gated by the Clock beside it.
_TX_DATA, _TX_CLOCK = "Tx_Data", "Tx_Clock"
_RX_DATA, _RX_CLOCK = "Rx_Data", "Rx_Clock"
_TRANSCEIVER_MEMBERS = (_RX_DATA, _RX_CLOCK, _TX_DATA, _TX_CLOCK)

# A structural parameter is the one causality whose value FMI 3.0 has an
# importer change inside Configuration Mode rather than in the instantiated
# state. The CAN node of the acceptance fixture declares one.
_STRUCTURAL = "structuralParameter"

# A bounded payload's length is a count of bytes, so a signed field would
# admit a length no payload can have. The ceiling is what each unsigned field
# can still count to: a length field that cannot reach its own payload
# field's bound describes a Channel whose payload can never fill it.
_LENGTH_CEILINGS = {
    "u8": 0xFF, "u16": 0xFFFF, "u32": 0xFFFF_FFFF,
    "u64": 0xFFFF_FFFF_FFFF_FFFF,
}

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
    # How many values the declared dimensions amount to: one when the
    # variable declares none, and None when a dimension is sized by another
    # variable, which the description does not settle.
    value_count: int | None
    # The value references of the Clocks that gate this variable. A variable
    # that declares one is defined only while that Clock is active.
    clocks: tuple[int, ...] = ()
    # Declared by a Clock only: what decides when it is active.
    interval_variability: str | None = None
    # Declared by a Binary variable only: the media type of what it carries.
    # A layered standard's profile is stated here, parameters included.
    mime_type: str | None = None

    @property
    def media_type(self) -> str | None:
        """The media type alone, without the parameters that qualify it."""
        if self.mime_type is None:
            return None
        return self.mime_type.partition(";")[0].strip()


@dataclass(frozen=True)
class Terminal:
    """One terminal of `terminalsAndIcons.xml`, and the variables it groups.

    A terminal is the unit a layered standard connects: it names a role for
    each variable it holds, so two FMUs of the same matching rule can be wired
    to each other by terminal rather than variable by variable.
    """

    name: str
    kind: str
    matching_rule: str
    # Each member's role, mapped to the variable that plays it.
    members: dict[str, str]


@dataclass(frozen=True)
class BusProfile:
    """What an FMU's FMI-LS-BUS layered-standard manifest declares.

    `bus_simulation` is the one flag that decides a topology: a bus simulation
    FMU is what arbitration and transmission modeling belongs in, and the
    nodes attached to it are ordinary FMUs that only send and receive.
    """

    version: str | None
    bus_simulation: bool


def _by_causality(
    variables: dict[str, Variable], causality: str
) -> dict[str, int]:
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


def _value_count(element) -> int | None:
    """How many values one variable's declared dimensions amount to.

    The acceptance fixture's CAN node declares its Binary input as
    `<Dimension start="1"/>`, which is one value written the long way — the
    same variable a description without any Dimension declares.
    """
    count = 1
    for dimension in element.findall("Dimension"):
        start = dimension.get("start")
        if start is None:
            return None
        count *= int(start)
    return count


def _clock_references(element) -> tuple[int, ...]:
    """The Clocks one variable declares, as FMI 3.0 lists them.

    The attribute is a whitespace-separated list of value references, which is
    how a variable says it is defined only while those Clocks are active.
    """
    return tuple(int(reference) for reference in element.get("clocks", "").split())


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
            value_count=_value_count(element),
            clocks=_clock_references(element),
            interval_variability=element.get("intervalVariability"),
            mime_type=element.get("mimeType"),
        )
    return declared


def _terminals(extracted: Path) -> dict[str, Terminal]:
    """Every terminal the archive declares, by name, in declaration order.

    The file is optional in FMI 3.0, and an FMU without it is not broken — it
    declares no terminal, which is all a mapping by variable name needs. A
    file that is present and unreadable is a different thing and is reported.
    """
    path = extracted / _TERMINALS_MEMBER
    if not path.exists():
        return {}
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError) as error:
        raise ManifestError(
            f"FMU carries an unreadable {_TERMINALS_MEMBER}: {error}"
        ) from error
    declared: dict[str, Terminal] = {}
    for element in root.iter("Terminal"):
        name = element.get("name")
        if name is None:
            continue
        declared[name] = Terminal(
            name=name,
            kind=element.get("terminalKind", ""),
            matching_rule=element.get("matchingRule", ""),
            members={
                member.get("memberName"): member.get("variableName")
                for member in element.findall("TerminalMemberVariable")
                if member.get("memberName") and member.get("variableName")
            },
        )
    return declared


def _bus_profile(extracted: Path) -> BusProfile | None:
    """What the FMI-LS-BUS layered-standard manifest declares, if it is there.

    An FMU that ships no such manifest declares no bus profile; the attribute
    that matters is `isBusSimulationFMU`, which the standard puts in no
    namespace while it namespaces its own version.
    """
    path = extracted / _BUS_MANIFEST_MEMBER
    if not path.exists():
        return None
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError) as error:
        raise ManifestError(
            f"FMU carries an unreadable {_BUS_MANIFEST_MEMBER}: {error}"
        ) from error
    return BusProfile(
        version=root.get(f"{{{_LAYERED_STANDARD_NAMESPACE}}}fmi-ls-version"),
        bus_simulation=root.get("isBusSimulationFMU") == "true",
    )


@dataclass(frozen=True)
class ModelDescription:
    """What `modelDescription.xml` says that driving the FMU depends on."""

    model_identifier: str
    instantiation_token: str
    variables: dict[str, Variable]
    inputs: dict[str, int]
    outputs: dict[str, int]
    # The co-simulation capability flags, as the description spells them. A
    # description read by `read` always carries them; the default keeps every
    # other way of naming an FMU's variables working unchanged.
    capabilities: dict[str, str] = field(default_factory=dict)
    # What the archive declares beside `modelDescription.xml`: the terminals a
    # layered standard connects, and the FMI-LS-BUS profile they belong to.
    terminals: dict[str, Terminal] = field(default_factory=dict)
    bus: BusProfile | None = None

    def clock(self, reference: int) -> Variable | None:
        """The Clock one value reference names, if it names a Clock at all."""
        for variable in self.variables.values():
            if variable.reference == reference and variable.kind == _CLOCK:
                return variable
        return None

    @property
    def has_event_mode(self) -> bool:
        """Whether the FMU declares the mode a Clock is driven from."""
        return self.capabilities.get(_HAS_EVENT_MODE) == "true"

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
            capabilities=dict(co_simulation.attrib),
            terminals=_terminals(extracted),
            bus=_bus_profile(extracted),
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


@dataclass(frozen=True)
class _DiscreteStates:
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

    def get_clock(self, references, values) -> None:
        self._call("fmi3GetClock", references, len(references), values)

    def set_clock(self, references, values) -> None:
        self._call("fmi3SetClock", references, len(references), values)

    def interval_fraction(self, references, counters, resolutions,
                          qualifiers) -> None:
        """Read the countdown intervals the FMU is asking to be activated at."""
        self._call(
            "fmi3GetIntervalFraction", references, len(references),
            counters, resolutions, qualifiers,
        )

    def update_discrete_states(self) -> _DiscreteStates:
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
        return _DiscreteStates(
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
            if variable.causality == _STRUCTURAL
        ]
        if structural:
            self._call("fmi3EnterConfigurationMode")
            self._write_start_values(structural)
            self._call("fmi3ExitConfigurationMode")
        self._write_start_values([
            (variable, value) for variable, value in starts
            if variable.causality != _STRUCTURAL
        ])

    def _write_start_values(self, starts) -> None:
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


@dataclass(frozen=True)
class _Binding:
    """One Channel schema field bound to one FMU variable."""

    channel: str
    field: str
    variable: Variable

    def __str__(self) -> str:
        """How every diagnostic about this binding opens."""
        return (
            f"Channel {self.channel!r} field {self.field!r} names FMU "
            f"variable {self.variable.name!r}"
        )


class _ScalarGroup:
    """One Channel's fields bound to FMU variables of one scalar type.

    The value references and the value buffer are built once, at
    initialization, because they are the same on every step, and one group is
    one FMI call however many fields it carries.
    """

    def __init__(self, kind: str, bound: list[_Binding]):
        self._kind = kind
        self._scalar = _SCALARS[kind]
        self._fields = [binding.field for binding in bound]
        self._references = _references(
            *(binding.variable.reference for binding in bound)
        )
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
    # Set when the variable declares a Clock: the Clock that gates it, and the
    # field stating the FMI event time of the activation being carried.
    clock: Variable | None = None
    event_time_field: str | None = None


def _outgoing_length(binary: _BinaryField, fields: dict) -> int:
    """How much of a Message's payload field the FMU is handed."""
    length = fields[binary.length_field]
    if length > binary.capacity:
        raise ParticipantFailure(
            f"field {binary.length_field!r} declares {length} bytes, "
            f"above the {binary.capacity} field {binary.field!r} "
            f"carries for FMU variable {binary.variable.name!r}"
        )
    return length


def _incoming_payload(binary: _BinaryField, payload: bytes) -> dict:
    """One Binary value the FMU produced, as the Channel's bounded fields.

    The payload is padded to the Channel's bound with zeros, so one Manifest
    records the same bytes on every Run.
    """
    if len(payload) > binary.capacity:
        raise ParticipantFailure(
            f"FMU variable {binary.variable.name!r} produced {len(payload)} "
            f"bytes; field {binary.field!r} carries {binary.capacity}"
        )
    return {
        binary.field: payload + bytes(binary.capacity - len(payload)),
        binary.length_field: len(payload),
    }


def _copied_out(length: int, address) -> bytes:
    """One Binary value, out of the pointer the FMU answered with.

    The pointer is the FMU's own and valid only until its next call, so the
    bytes are copied at once rather than held.
    """
    return ctypes.string_at(address, length) if length else b""


class _BinaryGroup:
    """One Channel's Binary variables, as bounded payloads with a length.

    A group writes or reads, never both, because the Channel's direction
    decides which. The buffers a written group hands the FMU are allocated
    once and live as long as the group: FMI 3.0 has the importer own them for
    the duration of the call, and reallocating one per step would put their
    lifetime in the garbage collector's hands. A read group has none — the
    pointers are the FMU's own, valid until its next call, so each payload is
    copied out at once and padded to the Channel's bound.
    """

    def __init__(self, bound: list[_BinaryField], causality: str):
        self._bound = bound
        self._references = _references(*(b.variable.reference for b in bound))
        self._sizes = (ctypes.c_size_t * len(bound))()
        self._values = (ctypes.c_void_p * len(bound))()
        self._buffers = (
            [(ctypes.c_char * b.capacity)() for b in bound]
            if causality == "input" else []
        )
        for index, buffer in enumerate(self._buffers):
            self._values[index] = ctypes.addressof(buffer)

    def write(self, fmu: CoSimulation, fields: dict) -> None:
        for index, binary in enumerate(self._bound):
            length = _outgoing_length(binary, fields)
            self._buffers[index][:length] = fields[binary.field][:length]
            self._sizes[index] = length
        fmu.set_binary(self._references, self._sizes, self._values)

    def read(self, fmu: CoSimulation, into: dict) -> None:
        fmu.get_binary(self._references, self._sizes, self._values)
        for index, binary in enumerate(self._bound):
            into.update(_incoming_payload(
                binary, _copied_out(self._sizes[index], self._values[index])
            ))


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


class _ClockedBuffer:
    """The FMU side of one Binary variable gated by a triggered Clock.

    A clocked Binary variable is defined only while its Clock is active, so
    neither end of it belongs to a Step: the Clock is read, or raised, inside
    an event. Reading and raising are the same rule seen from both ends —
    Clock first, buffer second.

    The buffer an incoming payload is copied into is allocated once and lives
    as long as this object: FMI 3.0 has the importer own it for the duration
    of the call, and reallocating one per activation would put its lifetime in
    the garbage collector's hands.
    """

    def __init__(self, variable: Variable, clock: Variable, capacity: int,
                 *, incoming: bool):
        self.variable = variable
        self._clock_references = _references(clock.reference)
        self._clock_values = (ctypes.c_bool * 1)()
        self._data_references = _references(variable.reference)
        self._sizes = (ctypes.c_size_t * 1)()
        self._values = (ctypes.c_void_p * 1)()
        self._buffer = (ctypes.c_char * capacity)() if incoming else None
        if self._buffer is not None:
            self._values[0] = ctypes.addressof(self._buffer)

    def read(self, fmu: CoSimulation) -> bytes | None:
        """The Clock, and the buffer it gates when it reads active.

        The Clock is read exactly once here, because an FMU clears it on the
        read: reading it twice would lose the activation, and not reading it
        at all would hand on a buffer no activation stands behind.
        """
        fmu.get_clock(self._clock_references, self._clock_values)
        if not self._clock_values[0]:
            return None
        fmu.get_binary(self._data_references, self._sizes, self._values)
        return _copied_out(self._sizes[0], self._values[0])

    def deliver(self, fmu: CoSimulation, payload: bytes) -> None:
        """Raise the Clock, then hand the FMU the payload it gates.

        That order is the contract, not a preference: a clocked variable may
        be accessed only while its Clock is active, and an FMU that checks
        refuses a buffer written before the Clock went up.
        """
        self._clock_values[0] = True
        fmu.set_clock(self._clock_references, self._clock_values)
        self._buffer[:len(payload)] = payload
        self._sizes[0] = len(payload)
        fmu.set_binary(self._data_references, self._sizes, self._values)


class _CountdownBuffer:
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
        self._data_references = _references(variable.reference)
        self._sizes = (ctypes.c_size_t * 1)()
        self._values = (ctypes.c_void_p * 1)()
        # The interval last stated, and the instant it ends at. They are kept
        # apart because an interval outlives the activation it caused: a
        # Clock the FMU leaves `Unchanged` after an activation is asking for
        # the same interval again, and one it never stated asks for nothing.
        self._interval_ns: int | None = None
        self.due_ns: int | None = None

    def refresh(self, fmu: CoSimulation, now_ns: int) -> None:
        """Read what the FMU asks for next, at the end of one of its events."""
        fmu.interval_fraction(
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
        fmu.get_binary(self._data_references, self._sizes, self._values)
        return _copied_out(self._sizes[0], self._values[0])


class _ClockedPayload:
    """One Channel whose Messages are activations of one Clock.

    One activation is one Message, and the Message states the FMI event time
    it belongs to rather than being timestamped with it.
    """

    def __init__(self, binary: _BinaryField, causality: str):
        self._binary = binary
        self._buffer = _ClockedBuffer(
            binary.variable, binary.clock, binary.capacity,
            incoming=causality == "input",
        )

    def activation(self, fmu: CoSimulation, event_time_ns: int) -> dict | None:
        """The Message this Clock's activation carries, if it is active."""
        payload = self._buffer.read(fmu)
        if payload is None:
            return None
        return {
            **_incoming_payload(self._binary, payload),
            self._binary.event_time_field: event_time_ns,
        }

    def activate(self, fmu: CoSimulation, fields: dict) -> None:
        """Hand one Message to the FMU as an activation of its input Clock.

        The Message's own event time is not used: this importer activates the
        Clock at the communication point it is standing on, and quietly
        dating the activation as its sender did would claim a time the FMU
        was never driven to.
        """
        length = _outgoing_length(self._binary, fields)
        self._buffer.deliver(fmu, fields[self._binary.field][:length])


def _run_event(fmu: CoSimulation, event_time_ns: int,
               collect: Callable[[], list]) -> tuple[list, _DiscreteStates]:
    """Iterate one event to quiescence, collecting what each update produced.

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


class _Events:
    """The FMU's event side: the Clocks an event activates, in both directions.

    Every method here is called with the FMU already in Event Mode, because
    entering and leaving it is the participant's business — an event that
    followed a Step and one that ends initialization are the same event, and
    only the caller knows which it is holding.

    `next_event_time` is what the last discrete-state update declared, in
    seconds, and it is kept because the *next* Step is where an importer that
    cannot honour it has to say so.
    """

    def __init__(self, outputs: dict[str, _ClockedPayload],
                 inputs: dict[str, _ClockedPayload]):
        self._outputs = list(outputs.items())
        self._inputs = inputs
        self.next_event_time: float | None = None

    def handle(self, fmu: CoSimulation, event_time_ns: int) -> list:
        """One event: every Clock activation it carries, in Publish order."""
        published, states = _run_event(
            fmu, event_time_ns,
            lambda: self._activations(fmu, event_time_ns),
        )
        self.next_event_time = states.next_event_time
        return published

    def _activations(self, fmu: CoSimulation, event_time_ns: int) -> list:
        """Every output Clock that reads active, and the buffer it gates."""
        published = []
        for channel, payload in self._outputs:
            activation = payload.activation(fmu, event_time_ns)
            if activation is not None:
                published.append((channel, activation))
        return published

    def deliver(self, fmu: CoSimulation, messages: list,
                event_time_ns: int) -> list:
        """Activate one input Clock per Message, in the order they arrived.

        Each Message is its own activation, so they are handed over one at a
        time: two frames delivered in one Slot are two activations of the same
        Clock, and merging them would lose one.
        """
        published = []
        for message in messages:
            self._inputs[message.channel].activate(fmu, message.data)
            published.extend(self.handle(fmu, event_time_ns))
        return published

    def receives(self, channel: str) -> bool:
        """Whether Messages on this Channel are Clock activations."""
        return channel in self._inputs

    def require_next_event_reachable(self, t: int, dt: int) -> None:
        """Refuse to step past an event the FMU asked to be stopped at.

        An FMU that declares a next event time is asking its importer to
        choose the next communication point. This one cannot: the kernel owns
        the Slot grid, and the Manifest's step period is what decides it. An
        FMU that declares such a time inside the interval about to be stepped
        is told so, rather than stepped past it and reported as if the
        interval had been clean.

        An event declared *on* the end of this interval is reachable: the
        Step lands on it, and `due` is what takes it.
        """
        if self.next_event_time is None:
            return
        declared_ns = self._declared_ns()
        if declared_ns < t + dt:
            raise ParticipantFailure(
                f"the FMU declared its next event at {self.next_event_time} s "
                f"({declared_ns} ns), inside the interval [{t}, {t + dt}] ns "
                f"this Step covers; this importer does not choose "
                f"communication points, so it cannot stop there"
            )

    def due(self, communication_point_ns: int) -> bool:
        """Whether the FMU declared an event at the point just reached.

        An FMU that declares a next event time is asking to be in Event Mode
        when its own clock reaches that instant. `fmi3DoStep` reporting
        `eventHandlingNeeded` is the FMU saying so a second time, and an
        importer that waited for the second one would skip the event of an
        FMU that only said it once.
        """
        return (
            self.next_event_time is not None
            and self._declared_ns() == communication_point_ns
        )

    def _declared_ns(self) -> int:
        """The declared next event time, in the kernel's own nanoseconds.

        The FMU declares a double of seconds and the Slot grid is integer
        nanoseconds, so the two are compared in nanoseconds: an event the FMU
        means to fall on a communication point must not be missed, or
        refused, because the two ways of writing that instant differ in the
        last bit.
        """
        return round(self.next_event_time * NS_PER_S)


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


def _parse_binding(
    bind: str, fields_by_channel: dict[str, dict[str, dict]]
) -> tuple[str, str, str]:
    """One `--bind` argument, with its Channel end checked.

    The Channel is named because one schema is typically carried by more than
    one Channel, so a field name alone names no single end of the mapping.
    """
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
    return channel, field, variable_name


def _declared_bindings(
    binds: list[str],
    fields_by_channel: dict[str, dict[str, dict]],
    description: ModelDescription,
) -> dict[str, dict[str, Variable]]:
    """Resolve `--bind <channel>:<field>=<variable>` against both ends."""
    bound: dict[str, dict[str, Variable]] = {
        channel: {} for channel in fields_by_channel
    }
    for bind in binds:
        channel, field, variable_name = _parse_binding(bind, fields_by_channel)
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


def _dimensions(variable: Variable) -> str:
    """How a variable's declared dimensions read in a diagnostic."""
    if variable.value_count is None:
        return "a dimension sized by another variable"
    return f"dimensions of {variable.value_count} values"


def _require_mappable(binding: _Binding) -> None:
    """Reject a variable whose type or shape this importer does not map."""
    variable = binding.variable
    if variable.value_count != 1:
        raise ManifestError(
            f"{binding}, which declares {_dimensions(variable)}; this importer "
            f"maps variables of one value"
        )
    if variable.kind == _CLOCK:
        raise ManifestError(
            f"{binding}, which is a Clock variable; a Clock is driven through "
            f"the variable it gates rather than bound to a field of its own"
        )
    if variable.kind != _BINARY and variable.kind not in _SCALARS:
        raise ManifestError(
            f"{binding}, which is a {variable.kind} variable; this importer "
            f"maps Binary and the scalar types {', '.join(_SCALARS)}"
        )


def _require_causality(binding: _Binding, causality: str) -> None:
    """Reject a variable bound to a Channel of the other direction."""
    if binding.variable.causality != causality:
        raise ManifestError(
            f"{binding}, whose causality is {binding.variable.causality!r}; "
            f"this Channel's direction binds {causality} variables"
        )


def _require_field_type(binding: _Binding, spec: dict) -> None:
    """Reject a field whose type is not the one the variable's type maps to."""
    scalar = _SCALARS[binding.variable.kind]
    if spec.get("count") is not None or spec["type"] != scalar.field_type:
        raise ManifestError(
            f"Channel {binding.channel!r} declares field {binding.field!r} as "
            f"{_shape(spec)}; {binding.variable.kind} variable "
            f"{binding.variable.name!r} is carried by a "
            f"{scalar.field_type!r} scalar"
        )


def _binary_field(
    binding: _Binding, fields: dict[str, dict], causality: str
) -> _BinaryField:
    """The bounded representation a Channel carries one Binary variable in."""
    channel, field, variable = binding.channel, binding.field, binding.variable
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
    ceiling = _LENGTH_CEILINGS.get(length["type"])
    if length.get("count") is not None or ceiling is None:
        raise ManifestError(
            f"Channel {channel!r} declares field {length_field!r} as "
            f"{_shape(length)}; a Binary payload's length is an unsigned "
            f"scalar ({', '.join(repr(t) for t in _LENGTH_CEILINGS)})"
        )
    if capacity > ceiling:
        raise ManifestError(
            f"Channel {channel!r} declares field {length_field!r} as "
            f"{length['type']!r}, which counts no further than {ceiling}; "
            f"field {field!r} carries {capacity} bytes, so a full payload "
            f"could not state its own length"
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


def _require_every_field_carried(
    channel: str,
    fields: dict[str, dict],
    bound: dict[str, Variable],
    lengths: dict[str, str],
) -> None:
    """Require each field of one Channel to carry exactly one thing.

    A field carries a variable it is bound to, or the length of a Binary
    payload beside it. A field that carries neither is a mapping mistake, and
    one that carries both is two.
    """
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


def _gating_clock(
    binding: _Binding, description: ModelDescription
) -> Variable:
    """The Clock one clocked variable declares, checked against the profile."""
    variable = binding.variable
    if len(variable.clocks) != 1:
        raise ManifestError(
            f"{binding}, which declares {len(variable.clocks)} Clocks; this "
            f"importer carries a variable gated by one Clock"
        )
    clock = description.clock(variable.clocks[0])
    if clock is None:
        raise ManifestError(
            f"{binding}, whose clocks attribute names value reference "
            f"{variable.clocks[0]}, which FMU "
            f"{description.model_identifier!r} declares no Clock for"
        )
    if clock.interval_variability != _TRIGGERED:
        raise ManifestError(
            f"{binding}, which is gated by Clock {clock.name!r} of "
            f"intervalVariability {clock.interval_variability!r}; this "
            f"importer drives {_TRIGGERED!r} Clocks"
        )
    if clock.causality != variable.causality:
        raise ManifestError(
            f"{binding}, which is gated by Clock {clock.name!r} of causality "
            f"{clock.causality!r}; a Clock gates a variable of its own "
            f"causality"
        )
    if not description.has_event_mode:
        raise ManifestError(
            f"{binding}, which is gated by Clock {clock.name!r}; FMU "
            f"{description.model_identifier!r} declares "
            f"{_HAS_EVENT_MODE}=false, and a Clock is driven from Event Mode"
        )
    return clock


def _event_time_field(
    channel: str, field: str, fields: dict[str, dict]
) -> str:
    """The field a clocked Channel states each activation's event time in."""
    name = f"{field}{_EVENT_TIME_SUFFIX}"
    declared = fields.get(name)
    if declared is None:
        raise ManifestError(
            f"Channel {channel!r} carries no field {name!r}; a clocked "
            f"payload carries the FMI event time of its activation beside "
            f"it, which is not the time the Message is published at"
        )
    if declared.get("count") is not None or declared["type"] != _EVENT_TIME_TYPE:
        raise ManifestError(
            f"Channel {channel!r} declares field {name!r} as "
            f"{_shape(declared)}; an FMI event time is a "
            f"{_EVENT_TIME_TYPE!r} scalar of nanoseconds"
        )
    return name


def _clocked_payload(
    channel: str,
    direction: str,
    fields: dict[str, dict],
    bound: dict[str, Variable],
    description: ModelDescription,
) -> _ClockedPayload | None:
    """The Clock-gated payload this Channel carries, if it carries one.

    A Channel carries one or none: its Messages are activations of a single
    Clock, and a second variable read beside them would be a Step's value
    published on an event's Message.
    """
    clocked = [field for field, variable in bound.items() if variable.clocks]
    if not clocked:
        return None
    causality = "input" if direction == "in" else "output"
    field = clocked[0]
    binding = _Binding(channel, field, bound[field])
    if len(bound) > 1:
        others = ", ".join(sorted(repr(name) for name in bound if name != field))
        raise ManifestError(
            f"{binding}, which is gated by a Clock; Channel {channel!r} binds "
            f"{others} as well, and a clocked Channel carries one activation "
            f"and nothing else"
        )
    if binding.variable.kind != _BINARY:
        raise ManifestError(
            f"{binding}, which is a clocked {binding.variable.kind} variable; "
            f"this importer carries a clocked variable as a bounded Binary "
            f"payload"
        )
    _require_mappable(binding)
    _require_causality(binding, causality)
    clock = _gating_clock(binding, description)
    binary = _binary_field(binding, fields, causality)
    event_time = _event_time_field(channel, field, fields)
    carried = {binary.field, binary.length_field, event_time}
    for declared in fields:
        if declared not in carried:
            raise ManifestError(
                f"Channel {channel!r} declares schema field {declared!r}, "
                f"which a clocked payload does not carry"
            )
    return _ClockedPayload(
        _BinaryField(
            variable=binary.variable, field=binary.field,
            length_field=binary.length_field, capacity=binary.capacity,
            clock=clock, event_time_field=event_time,
        ),
        causality,
    )


def _bind_channel(
    channel: str,
    direction: str,
    fields: dict[str, dict],
    bound: dict[str, Variable],
) -> _ChannelBinding:
    """One Channel's fields, checked against the variables they name."""
    causality = "input" if direction == "in" else "output"
    scalars: dict[str, list[_Binding]] = {}
    binaries: list[_BinaryField] = []
    lengths: dict[str, str] = {}
    for field in fields:
        if field not in bound:
            continue
        binding = _Binding(channel, field, bound[field])
        _require_mappable(binding)
        _require_causality(binding, causality)
        if binding.variable.kind == _BINARY:
            binary = _binary_field(binding, fields, causality)
            lengths[binary.length_field] = field
            binaries.append(binary)
        else:
            _require_field_type(binding, fields[field])
            scalars.setdefault(binding.variable.kind, []).append(binding)
    _require_every_field_carried(channel, fields, bound, lengths)
    groups: list = [
        _ScalarGroup(kind, bindings) for kind, bindings in scalars.items()
    ]
    if binaries:
        groups.append(_BinaryGroup(binaries, causality))
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


def _require_contiguous(standing_ns: int, t: int) -> None:
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
        # Set when a Channel carries a Clock-gated payload; None is the
        # Step-only lifecycle, where the FMU never leaves Step Mode.
        self._events: _Events | None = None
        # What the event that ended initialization produced, held until the
        # first activation: the kernel has no Slot before it.
        self._pending_activations: list = []
        # Where the FMU stands, in kernel nanoseconds.
        self._communication_point = 0

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
        self._fmu = CoSimulation(
            description.binary(extracted), description,
            event_mode=self._events is not None,
        )
        self._fmu.apply_start_values(starts)
        self._fmu.initialize()
        if self._events is not None:
            # With Event Mode in use, initialization ends in Event Mode, and
            # a bus node has its configuration waiting in that first event.
            # Its activations belong to the initial time, and they are
            # published in the first Slot the kernel activates this
            # participant in, which is that same instant.
            self._pending_activations = self._events.handle(self._fmu, 0)
            self._fmu.enter_step_mode()

    def _bind_channels(self, init: dict, description: ModelDescription) -> None:
        """Bind the declared Channels to FMU variables, in both directions.

        An input-direction Channel is written into the FMU before its step; an
        output-direction Channel is published from it after. Declaring one
        binding declares them all — the bindings are the whole mapping, and a
        Channel with none derives its own from the Float64 variable names.

        A Channel whose variable declares a Clock is neither: its Messages are
        activations of that Clock, handled in Event Mode.
        """
        fields_by_channel = _channel_fields(init)
        bound = (
            _declared_bindings(self._binds, fields_by_channel, description)
            if self._binds
            else _derived_bindings(init, fields_by_channel, description)
        )
        clocked: dict[str, dict[str, _ClockedPayload]] = {"in": {}, "out": {}}
        for channel, declaration in init["channels"].items():
            direction = declaration["direction"]
            fields = fields_by_channel[channel]
            payload = _clocked_payload(
                channel, direction, fields, bound[channel], description
            )
            if payload is not None:
                clocked[direction][channel] = payload
                continue
            bindings = self._inputs if direction == "in" else self._outputs
            bindings[channel] = _bind_channel(
                channel, direction, fields, bound[channel]
            )
        if clocked["in"] or clocked["out"]:
            self._events = _Events(clocked["out"], clocked["in"])

    def on_step(self, t: int, dt: int, inputs: list):
        """One Step of the FMU, and every Message the interval produced.

        The kernel's Slot is `t` and the FMU is advanced over `[t, t+dt]`, so
        what this publishes at `t` is what the FMU reached at `t+dt` — the
        Step-mapped outputs as the values it holds there, and each Clock
        activation as its own Message stating the FMI event time it belongs
        to. The two are different quantities on purpose: an operation
        observed at a communication point is published in the Slot the
        importer was activated in, and reaches a subscriber one Latency after
        that.
        """
        if self._events is None:
            return self._step(t, dt, inputs)
        self._require_communication_point(t)
        published, self._pending_activations = self._pending_activations, []
        events, plain = self._split_activations(inputs)
        published.extend(self._deliver(events, t))
        published.extend(self._step(t, dt, plain))
        self._communication_point = t + dt
        return published

    def _step(self, t: int, dt: int, inputs: list) -> list:
        """The Step-mapped half: write, advance, read, and take any event."""
        # Inputs arrive in publish order, so writing each in turn leaves the
        # newest Message on a Channel as the value the step sees.
        for message in inputs:
            self._inputs[message.channel].write(self._fmu, message.data)
        if self._events is not None:
            self._events.require_next_event_reachable(t, dt)
        event_needed = self._fmu.do_step(t / NS_PER_S, dt / NS_PER_S)
        published = []
        if event_needed and self._events is None:
            raise ParticipantFailure(
                f"fmi3DoStep reported eventHandlingNeeded at "
                f"{(t + dt) / NS_PER_S} s; no Channel of this Run carries "
                f"a Clock, so the FMU was instantiated with eventModeUsed "
                f"false and the event cannot be handled"
            )
        # The FMU asks for an event either by reporting one at the end of the
        # Step or by having declared its time beforehand. A Step that lands
        # on a declared event time is that event's communication point, and
        # waiting for the flag as well would skip it.
        if self._events is not None and (event_needed or self._events.due(t + dt)):
            self._fmu.enter_event_mode()
            published = self._events.handle(self._fmu, t + dt)
            self._fmu.enter_step_mode()
        return published + [
            (channel, binding.read(self._fmu))
            for channel, binding in self._outputs.items()
        ]

    def _split_activations(self, inputs: list) -> tuple[list, list]:
        """Messages that are Clock activations, and Messages that are values."""
        events: list = []
        plain: list = []
        for message in inputs:
            target = events if self._events.receives(message.channel) else plain
            target.append(message)
        return events, plain

    def _deliver(self, messages: list, t: int) -> list:
        """Hand the FMU the activations that arrived, at the time it stands on.

        They are delivered before the Step rather than inside it: the FMU is
        standing on this communication point, and an event happens at the
        point the FMU stands on.
        """
        if not messages:
            return []
        self._fmu.enter_event_mode()
        published = self._events.deliver(self._fmu, messages, t)
        self._fmu.enter_step_mode()
        return published

    def _require_communication_point(self, t: int) -> None:
        _require_contiguous(self._communication_point, t)


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


@dataclass(frozen=True)
class _Observation:
    """An out-direction Channel carrying one clocked variable's activations.

    It is observation and nothing else: the group reads the variable once,
    because the Clock that gates it clears on the read, and the Message is a
    statement of what crossed the terminal rather than a second read of it.
    """

    channel: str
    binary: _BinaryField

    def message(self, payload: bytes, event_time_ns: int) -> tuple[str, dict]:
        return self.channel, {
            **_incoming_payload(self.binary, payload),
            self.binary.event_time_field: event_time_ns,
        }


@dataclass(frozen=True)
class _Injection:
    """An in-direction Channel whose Messages are activations of one Clock.

    This is the replay-input half of the boundary: a Message on it is what a
    connected peer would otherwise have handed the same terminal, so a
    Recording of the observation Channel can stand in for the FMU that
    produced it.
    """

    channel: str
    binary: _BinaryField

    def payload(self, fields: dict) -> bytes:
        length = _outgoing_length(self.binary, fields)
        return fields[self.binary.field][:length]


class _Transceiver:
    """One network terminal of one instance, as the group drives it.

    The direction is the terminal owner's: `Tx_Data` is what this FMU sends
    and `Rx_Data` is what it is handed. A connection pairs one transceiver's
    send side with its peer's receive side, in both directions, and a Channel
    may watch the one or feed the other.
    """

    def __init__(self, instance: _Instance, terminal: Terminal,
                 receive: _ClockedBuffer, capacity: int):
        self.instance = instance
        self.terminal = terminal
        self.receive = receive
        self.capacity = capacity
        self.peer: _Transceiver | None = None
        self.observation: _Observation | None = None
        self.injection: _Injection | None = None

    def __str__(self) -> str:
        """How every diagnostic names one end of a connection."""
        return f"{self.instance.name}.{self.terminal.name}"

    def accept(self, payload: bytes) -> None:
        """Refuse a payload above what the receiving variable declared.

        `maxSize` is the FMU's own statement of the most it will take, so a
        longer buffer is refused here rather than handed over and rejected
        with a status that names no length.
        """
        if len(payload) > self.capacity:
            raise ParticipantFailure(
                f"{self} was handed {len(payload)} bytes, above the "
                f"{self.capacity} its {_RX_DATA!r} variable "
                f"{self.receive.variable.name!r} declares as maxSize"
            )


@dataclass(frozen=True)
class _Pending:
    """One event a group owes an instance at the instant it is settling.

    An activation handed over by a connected peer, or by a Channel, comes with
    it: the payload is written into the receiving FMU immediately before its
    event, because that write is what the event is about.
    """

    instance: _Instance
    transceiver: _Transceiver | None = None
    payload: bytes | None = None


class _Instance:
    """One FMU of a group: the instance, and the Clocks the group drives.

    It owns where the FMU stands in its own lifecycle — Step Mode or Event
    Mode — because a group enters an event on one instance and not on another,
    and an FMU asked to enter the mode it is already in refuses.
    """

    def __init__(self, name: str, description: ModelDescription):
        self.name = name
        self.description = description
        self.fmu: CoSimulation | None = None
        # The send side of every terminal, split by who decides when it
        # activates: the FMU raises a triggered Clock, and the group raises a
        # countdown one at the instant the FMU asked for.
        self.triggered: list[tuple[_Transceiver, _ClockedBuffer]] = []
        self.countdown: list[tuple[_Transceiver, _CountdownBuffer]] = []
        self._in_event = False
        self._event_pending = False
        self._next_event_time: float | None = None

    def instantiate(self, extracted: Path,
                    starts: list[tuple[Variable, object]]) -> None:
        """Load and initialize this FMU, in the group's declaration order.

        Event Mode is always in use: every instance of a group drives a
        network terminal, and a terminal is Clocks. Initialization therefore
        ends in Event Mode, and the caller handles that first event.
        """
        self.fmu = CoSimulation(
            self.description.binary(extracted), self.description,
            event_mode=True,
        )
        self.fmu.apply_start_values(starts)
        self.fmu.initialize()
        self._in_event = True

    def enter_event(self) -> None:
        if not self._in_event:
            self.fmu.enter_event_mode()
            self._in_event = True

    def leave_event(self) -> None:
        if self._in_event:
            self.fmu.enter_step_mode()
            self._in_event = False

    def step(self, t: int, dt: int) -> None:
        """Advance over one of the group's own intervals."""
        self._event_pending = self.fmu.do_step(t / NS_PER_S, dt / NS_PER_S)

    def due(self, instant_ns: int) -> bool:
        """Whether this FMU asked for an event at the instant just reached."""
        return (
            self._event_pending
            or self._declared_ns() == instant_ns
            or any(buffer.due_ns == instant_ns for _, buffer in self.countdown)
        )

    def asked_for(self, after_ns: int) -> list[int]:
        """Every instant this FMU asked to be stopped at, after `after_ns`.

        Both kinds of request are the same thing to a group: an instant the
        FMU means to be in Event Mode at. The group stops at the earliest of
        them across every instance, so no FMU is stepped past its own event
        and no peer of it is past that instant either.
        """
        instants = [
            buffer.due_ns for _, buffer in self.countdown
            if buffer.due_ns is not None
        ]
        declared = self._declared_ns()
        if declared is not None:
            instants.append(declared)
        return [instant for instant in instants if instant > after_ns]

    def require_nothing_passed(self, instant_ns: int) -> None:
        """Refuse an FMU that asks to be activated at an instant already gone.

        The group can stop anywhere between two Slots, so an instant it cannot
        reach is one behind it. Stepping on regardless would take the FMU past
        an event it asked for, and no FMU of this profile offers the rollback
        that would take it back.
        """
        for transceiver, buffer in self.countdown:
            if buffer.due_ns is not None and buffer.due_ns <= instant_ns:
                raise ParticipantFailure(
                    f"{transceiver} asks for Clock {buffer.clock.name!r} to be "
                    f"activated at {buffer.due_ns} ns, and the group stands at "
                    f"{instant_ns} ns; a countdown interval is counted from the "
                    f"event it was stated in"
                )
        declared = self._declared_ns()
        if declared is not None and declared <= instant_ns:
            raise ParticipantFailure(
                f"FMU {self.name!r} declared its next event at "
                f"{self._next_event_time} s ({declared} ns), and the group "
                f"stands at {instant_ns} ns; an event is asked for ahead of the "
                f"instant it is asked in"
            )

    def handle(self, event_time_ns: int) -> list[tuple[_Transceiver, bytes]]:
        """One event of this FMU: every activation it hands the group.

        The countdown Clocks due at this instant go up first, together in one
        call, and the buffers they gate are read before any discrete-state
        update — an FMU clears them in the update that ends the activation. The
        triggered Clocks are then read around every update, as they are for one
        FMU on its own.
        """
        self.enter_event()
        self._event_pending = False
        due = [
            (transceiver, buffer) for transceiver, buffer in self.countdown
            if buffer.due_ns == event_time_ns
        ]
        if due:
            self.fmu.set_clock(
                _references(*(buffer.clock.reference for _, buffer in due)),
                (ctypes.c_bool * len(due))(*(True,) * len(due)),
            )
        produced = [
            (transceiver, buffer.take(self.fmu)) for transceiver, buffer in due
        ]
        collected, states = _run_event(
            self.fmu, event_time_ns, self._activations
        )
        produced.extend(collected)
        self._next_event_time = states.next_event_time
        for _, buffer in self.countdown:
            buffer.refresh(self.fmu, event_time_ns)
        return produced

    def deliver(self, transceiver: _Transceiver, payload: bytes) -> None:
        """Raise one input Clock and hand it the payload, before its event."""
        transceiver.accept(payload)
        self.enter_event()
        transceiver.receive.deliver(self.fmu, payload)

    def _activations(self) -> list[tuple[_Transceiver, bytes]]:
        """Every triggered output Clock that reads active, and its buffer."""
        produced = []
        for transceiver, buffer in self.triggered:
            payload = buffer.read(self.fmu)
            if payload is not None:
                produced.append((transceiver, payload))
        return produced

    def _declared_ns(self) -> int | None:
        """The declared next event time, in the kernel's own nanoseconds."""
        if self._next_event_time is None:
            return None
        return round(self._next_event_time * NS_PER_S)


class _Group:
    """Connected FMUs, coordinated at their own communication points.

    Every instance stands on the same internal communication point at all
    times. That is what makes rollback unnecessary — and no FMU of this
    profile offers it, because they declare `canGetAndSetFMUState` false: an
    event reported at the end of an interval is reported at an instant no peer
    has passed, so no peer has to be taken back to it.

    The group's own grid is finer than the kernel's Slot grid, and it has to
    be: a bus simulation FMU asks to be activated at the end of a frame's
    transmission time, which is an instant the Manifest's Step period has no
    reason to contain. The kernel still owns the Slots the Messages are
    published in; what the group owns is where the FMUs meet between them.

    Propagation at one instant is bounded, like the iteration of one event:
    two FMUs answering each other at the same instant would otherwise never
    let the instant end.
    """

    def __init__(self, instances: list[_Instance],
                 transceivers: list[_Transceiver]):
        self._instances = instances
        self._injections = {
            transceiver.injection.channel: transceiver
            for transceiver in transceivers
            if transceiver.injection is not None
        }

    def initialize(self) -> list:
        """The event initialization ended in, across the whole group.

        A bus node's own configuration is already waiting in it, and the bus
        simulation FMU is what that configuration is for, so the first instant
        is propagated exactly like every later one.
        """
        return self._settle(0, [_Pending(i) for i in self._instances])

    def advance(self, t: int, dt: int, inputs: list) -> list:
        """Advance the group over one kernel Step, event by event.

        The interval is covered in sub-intervals ending at every instant any
        instance asked for, and at the Step's own end. Nothing is published
        with an internal instant as its timestamp: a Message is published in
        the Slot this activation runs in and states the FMI event time it
        belongs to, which is how the group's finer grid reaches a Recording.
        """
        published = self._settle(t, self._injected(inputs))
        now, end = t, t + dt
        while now < end:
            boundary = self._boundary(now, end)
            for instance in self._instances:
                instance.step(now, boundary - now)
            now = boundary
            published.extend(self._settle(now, [
                _Pending(instance) for instance in self._instances
                if instance.due(now)
            ]))
        return published

    def close(self) -> None:
        """Terminate and free every instance, whatever any one of them does.

        An FMU that fails to terminate must not leave its peers instantiated:
        the first failure is the one reported, and every instance is closed
        either way.
        """
        failure = None
        for instance in self._instances:
            if instance.fmu is None:
                continue
            try:
                instance.fmu.close()
            except ParticipantFailure as error:
                failure = failure or ParticipantFailure(
                    f"FMU of instance {instance.name!r}: {error}"
                )
        if failure is not None:
            raise failure

    def _injected(self, inputs: list) -> list:
        """The Messages that arrived, as activations at the point stood on.

        They are delivered before the interval rather than inside it: the
        group is standing on this communication point, and an event happens at
        the point the FMUs stand on.
        """
        pending = []
        for message in inputs:
            transceiver = self._injections[message.channel]
            pending.append(_Pending(
                transceiver.instance, transceiver,
                transceiver.injection.payload(message.data),
            ))
        return pending

    def _boundary(self, now: int, end: int) -> int:
        """The next instant the whole group stops at, at the latest `end`."""
        asked = [
            instant for instance in self._instances
            for instant in instance.asked_for(now) if instant <= end
        ]
        return min(asked) if asked else end

    def _settle(self, instant_ns: int, pending: list) -> list:
        """One instant, propagated between the FMUs until it stops producing.

        Each activation is handed over on its own and its event handled before
        the next one is written: two frames arriving at one terminal in the
        same instant are two activations of the same Clock, and writing the
        second before the first was processed would lose it.
        """
        if not pending:
            return []
        published: list = []
        work = deque(pending)
        for _ in range(_MAX_PROPAGATIONS):
            if not work:
                self._quiesce(instant_ns)
                return published
            owed = work.popleft()
            if owed.transceiver is not None:
                owed.instance.deliver(owed.transceiver, owed.payload)
            for source, payload in owed.instance.handle(instant_ns):
                if source.observation is not None:
                    published.append(
                        source.observation.message(payload, instant_ns)
                    )
                if source.peer is not None:
                    work.append(
                        _Pending(source.peer.instance, source.peer, payload)
                    )
        raise ParticipantFailure(
            f"the connected FMUs handed each other {_MAX_PROPAGATIONS} "
            f"activations at {instant_ns} ns without the instant ending; this "
            f"importer bounds the propagation of one instant"
        )

    def _quiesce(self, instant_ns: int) -> None:
        """End the instant: check what was asked for, return to Step Mode."""
        for instance in self._instances:
            instance.require_nothing_passed(instant_ns)
        for instance in self._instances:
            instance.leave_event()


def _instance_paths(declared: list[str]) -> dict[str, Path]:
    """Resolve `--instance <name>=<path>` into the group's declaration order.

    The name is the group's own vocabulary and every other argument spells a
    variable of it as `<name>.<variable>`, so a name carrying that separator
    would make the spelling ambiguous.
    """
    paths: dict[str, Path] = {}
    for declaration in declared:
        name, separator, path = declaration.partition("=")
        if not separator or not name or not path:
            raise ManifestError(
                f"instance {declaration!r} is not '<name>=<path>'"
            )
        if "." in name:
            raise ManifestError(
                f"instance {declaration!r} names {name!r}, which carries a "
                f"'.'; an instance name is what qualifies a variable of it"
            )
        if name in paths:
            raise ManifestError(
                f"instance {declaration!r} declares {name!r} twice; an "
                f"instance name names one FMU of the group"
            )
        paths[name] = Path(path)
    return paths


def _instance_of(
    text: str, instances: dict[str, _Instance], what: str
) -> tuple[_Instance, str]:
    """Split `<instance>.<rest>` and resolve the instance it names."""
    name, separator, rest = text.partition(".")
    if not separator or not rest:
        raise ManifestError(
            f"{what} names {text!r}, which is not '<instance>.<name>'; a "
            f"group's terminals and variables are qualified by their instance"
        )
    instance = instances.get(name)
    if instance is None:
        raise ManifestError(
            f"{what} names instance {name!r}, which --instance does not "
            f"declare (declared: "
            f"{', '.join(repr(i) for i in instances) or 'none'})"
        )
    return instance, rest


def _member_variable(
    where: str, instance: _Instance, terminal: Terminal, member: str
) -> Variable:
    """One terminal member's variable, as the description declares it."""
    name = terminal.members[member]
    variable = instance.description.variables.get(name)
    if variable is None:
        raise ManifestError(
            f"{where} member {member!r} names variable {name!r}, which FMU "
            f"{instance.description.model_identifier!r} does not declare"
        )
    return variable


def _member_pair(
    where: str, instance: _Instance, terminal: Terminal,
    data: str, clock: str, causality: str
) -> tuple[Variable, Variable]:
    """One data member and the Clock gating it, checked as the pair they are."""
    variable = _member_variable(where, instance, terminal, data)
    gate = _member_variable(where, instance, terminal, clock)
    if variable.kind != _BINARY:
        raise ManifestError(
            f"{where} member {data!r} is variable {variable.name!r} of type "
            f"{variable.kind}; a transceiver carries bus operations as a "
            f"Binary buffer"
        )
    if variable.value_count != 1:
        raise ManifestError(
            f"{where} member {data!r} is variable {variable.name!r}, which "
            f"declares {_dimensions(variable)}; this importer carries "
            f"variables of one value"
        )
    if variable.max_size is None:
        raise ManifestError(
            f"{where} member {data!r} is variable {variable.name!r}, which "
            f"declares no maxSize; a bus buffer states the most it holds"
        )
    if variable.causality != causality:
        raise ManifestError(
            f"{where} member {data!r} is variable {variable.name!r} of "
            f"causality {variable.causality!r}; a transceiver's {data!r} is "
            f"an {causality} variable"
        )
    if gate.kind != _CLOCK:
        raise ManifestError(
            f"{where} member {clock!r} is variable {gate.name!r} of type "
            f"{gate.kind}; a transceiver's {clock!r} is a Clock"
        )
    if variable.clocks != (gate.reference,):
        raise ManifestError(
            f"{where} member {data!r} is variable {variable.name!r}, whose "
            f"clocks attribute is {list(variable.clocks)}; it is gated by "
            f"member {clock!r}, value reference {gate.reference}"
        )
    return variable, gate


def _require_transceiver_terminal(
    where: str, instance: _Instance, terminal: Terminal, profile: str
) -> None:
    """Check one terminal against the BUS profile the Run declares.

    Everything this importer needs from a terminal is checked here, once per
    terminal: that the FMU declares the layered standard at all, that the
    terminal is a transceiver of it, that it groups the four members, and that
    what its buffers carry is the profile the Manifest asked for.
    """
    if instance.description.bus is None:
        raise ManifestError(
            f"{where} is a network terminal, and FMU "
            f"{instance.description.model_identifier!r} carries no "
            f"{_BUS_MANIFEST_MEMBER}; the layered standard a terminal belongs "
            f"to is declared by the FMU rather than by the connection"
        )
    if terminal.kind != _NETWORK_TERMINAL:
        raise ManifestError(
            f"{where} declares terminalKind {terminal.kind!r}; this importer "
            f"connects {_NETWORK_TERMINAL!r} terminals"
        )
    if terminal.matching_rule != _TRANSCEIVER:
        raise ManifestError(
            f"{where} declares matchingRule {terminal.matching_rule!r}; this "
            f"importer connects {_TRANSCEIVER!r} terminals"
        )
    if not instance.description.has_event_mode:
        raise ManifestError(
            f"{where} is a network terminal gated by Clocks, and FMU "
            f"{instance.description.model_identifier!r} declares "
            f"{_HAS_EVENT_MODE}=false; a Clock is driven from Event Mode"
        )
    for member in _TRANSCEIVER_MEMBERS:
        if member not in terminal.members:
            raise ManifestError(
                f"{where} declares no member {member!r}; a {_TRANSCEIVER!r} "
                f"terminal groups {', '.join(_TRANSCEIVER_MEMBERS)}"
            )
    for member in (_RX_DATA, _TX_DATA):
        variable = _member_variable(where, instance, terminal, member)
        if variable.media_type != profile:
            raise ManifestError(
                f"{where} member {member!r} is variable {variable.name!r} of "
                f"mimeType {variable.mime_type!r}; this Run declares the BUS "
                f"profile {profile!r}"
            )


def _build_transceiver(
    instance: _Instance, terminal: Terminal, profile: str
) -> _Transceiver:
    """Build one terminal's two sides, and say who raises each Clock.

    The receive side is always a triggered input Clock: an arriving frame is
    something that happened, and whoever hands it over is what raises it. The
    send side is either the FMU's own triggered output Clock — a node
    announcing a frame — or a countdown input Clock the FMU asks the group to
    raise, which is how a bus simulation FMU states a transmission time.
    """
    where = f"{instance.name}.{terminal.name}"
    _require_transceiver_terminal(where, instance, terminal, profile)
    receive, receive_clock = _member_pair(
        where, instance, terminal, _RX_DATA, _RX_CLOCK, "input"
    )
    if (receive_clock.causality, receive_clock.interval_variability) != (
        "input", _TRIGGERED
    ):
        raise ManifestError(
            f"{where} member {_RX_CLOCK!r} is Clock {receive_clock.name!r} of "
            f"causality {receive_clock.causality!r} and intervalVariability "
            f"{receive_clock.interval_variability!r}; a terminal is handed a "
            f"frame through an input {_TRIGGERED!r} Clock"
        )
    send, send_clock = _member_pair(
        where, instance, terminal, _TX_DATA, _TX_CLOCK, "output"
    )
    built = _Transceiver(
        instance, terminal,
        _ClockedBuffer(receive, receive_clock, receive.max_size, incoming=True),
        receive.max_size,
    )
    profile_of_clock = (send_clock.causality, send_clock.interval_variability)
    if profile_of_clock == ("output", _TRIGGERED):
        instance.triggered.append((
            built,
            _ClockedBuffer(send, send_clock, send.max_size, incoming=False),
        ))
    elif profile_of_clock == ("input", _COUNTDOWN):
        instance.countdown.append((built, _CountdownBuffer(send, send_clock)))
    else:
        raise ManifestError(
            f"{where} member {_TX_CLOCK!r} is Clock {send_clock.name!r} of "
            f"causality {send_clock.causality!r} and intervalVariability "
            f"{send_clock.interval_variability!r}; this importer drives an "
            f"output {_TRIGGERED!r} Clock the FMU raises, or an input "
            f"{_COUNTDOWN!r} Clock it asks the group to raise"
        )
    return built


class _Transceivers:
    """Every terminal this Run drives, built once and named two ways.

    A terminal is named by a connection as `<instance>.<terminal>` and by a
    Channel through the variable one of its members is, so both spellings
    resolve to the same object: a terminal connected to a peer and watched by a
    Channel is one transceiver, not two.
    """

    def __init__(self, instances: dict[str, _Instance], profile: str):
        self._instances = instances
        self._profile = profile
        self._built: dict[tuple[str, str], _Transceiver] = {}
        self._by_variable: dict[tuple[str, str], tuple[_Transceiver, str]] = {}

    def named(self, text: str, what: str) -> _Transceiver:
        """The transceiver `<instance>.<terminal>` names, built on first use."""
        instance, terminal_name = _instance_of(text, self._instances, what)
        key = (instance.name, terminal_name)
        if key in self._built:
            return self._built[key]
        terminal = instance.description.terminals.get(terminal_name)
        if terminal is None:
            declared = ", ".join(
                repr(t) for t in instance.description.terminals
            )
            raise ManifestError(
                f"{what} names terminal {terminal_name!r}, which FMU "
                f"{instance.description.model_identifier!r} of instance "
                f"{instance.name!r} does not declare (declared: "
                f"{declared or 'none'})"
            )
        built = _build_transceiver(instance, terminal, self._profile)
        self._built[key] = built
        for member in (_RX_DATA, _TX_DATA):
            self._by_variable[(instance.name, terminal.members[member])] = (
                built, member
            )
        return built

    def carrying(self, text: str, what: str) -> tuple[_Transceiver, str]:
        """The transceiver member the variable `<instance>.<variable>` is.

        Every terminal of every instance is built before this is asked, so a
        variable that is no member of one is a Channel bound to something a
        group does not carry.
        """
        instance, variable_name = _instance_of(text, self._instances, what)
        side = self._by_variable.get((instance.name, variable_name))
        if side is None:
            if variable_name not in instance.description.variables:
                raise ManifestError(
                    f"{what} names FMU variable {variable_name!r}, which FMU "
                    f"{instance.description.model_identifier!r} of instance "
                    f"{instance.name!r} does not declare"
                )
            raise ManifestError(
                f"{what} names FMU variable {variable_name!r}, which is no "
                f"{_TX_DATA!r} or {_RX_DATA!r} member of a network terminal of "
                f"instance {instance.name!r}; a group's Channels carry a "
                f"terminal's bus operations"
            )
        return side

    def build_every_terminal(self) -> None:
        """Build every network terminal every instance declares.

        A terminal is checked against the profile whether a connection or a
        Channel names it or not, because an FMU that declares a terminal this
        importer cannot drive is an FMU it cannot drive.
        """
        for instance in self._instances.values():
            if not instance.description.terminals:
                raise ManifestError(
                    f"instance {instance.name!r} is FMU "
                    f"{instance.description.model_identifier!r}, which declares "
                    f"no terminal; every FMU of a group drives a network "
                    f"terminal, connected to a peer or carried by a Channel"
                )
            for terminal in instance.description.terminals:
                self.named(f"{instance.name}.{terminal}", "the group")

    def all(self) -> list[_Transceiver]:
        return list(self._built.values())


def _connect(declarations: list[str], transceivers: _Transceivers) -> None:
    """Resolve `--connect <a>.<terminal>=<b>.<terminal>` into peer links."""
    for declaration in declarations:
        what = f"connection {declaration!r}"
        left, separator, right = declaration.partition("=")
        if not separator or not left or not right:
            raise ManifestError(
                f"{what} is not '<instance>.<terminal>=<instance>.<terminal>'"
            )
        ends = (transceivers.named(left, what), transceivers.named(right, what))
        if ends[0] is ends[1]:
            raise ManifestError(
                f"{what} connects {ends[0]} to itself; a terminal is connected "
                f"to a terminal of another instance"
            )
        for end in ends:
            if end.peer is not None:
                raise ManifestError(
                    f"{what} connects {end}, which is already connected to "
                    f"{end.peer}; a terminal carries one connection"
                )
        _require_compatible(what, *ends)
        ends[0].peer, ends[1].peer = ends[1], ends[0]


def _require_compatible(
    what: str, left: _Transceiver, right: _Transceiver
) -> None:
    """Check the two ends of one connection against each other.

    Three statements have to agree before a frame may cross: the layered
    standard's version, the media type of what each buffer carries, and which
    of the two FMUs models the bus. The last one is the supported topology —
    arbitration and transmission timing belong in the dedicated bus simulation
    FMU, and a connection between two nodes, or between two buses, has nobody
    to model them.
    """
    buses = [
        end for end in (left, right)
        if end.instance.description.bus.bus_simulation
    ]
    if len(buses) != 1:
        stated = ", ".join(
            f"{end} declares isBusSimulationFMU="
            f"{str(end.instance.description.bus.bus_simulation).lower()}"
            for end in (left, right)
        )
        raise ManifestError(
            f"{what}: {stated}; one end of a connection is the bus simulation "
            f"FMU and the other is a node attached to it"
        )
    versions = {
        end.instance.description.bus.version for end in (left, right)
    }
    if len(versions) != 1:
        raise ManifestError(
            f"{what} connects "
            f"{left} at {_BUS_LAYERED_STANDARD} "
            f"{left.instance.description.bus.version!r} to {right} at "
            f"{right.instance.description.bus.version!r}; both ends declare "
            f"one version of the layered standard"
        )
    for sender, receiver in ((left, right), (right, left)):
        sent = _member_variable(
            str(sender), sender.instance, sender.terminal, _TX_DATA
        )
        taken = _member_variable(
            str(receiver), receiver.instance, receiver.terminal, _RX_DATA
        )
        if sent.mime_type != taken.mime_type:
            raise ManifestError(
                f"{what}: {sender} sends {sent.mime_type!r} and {receiver} "
                f"takes {taken.mime_type!r}; the two ends of one direction "
                f"carry one profile"
            )


def _bind_group_channels(
    binds: list[str], init: dict, fields_by_channel: dict[str, dict[str, dict]],
    transceivers: _Transceivers
) -> None:
    """Attach every declared Channel to the terminal member it carries.

    A group's Channel carries one terminal's bus operations and nothing else:
    an out-direction Channel observes what a terminal sends, and an
    in-direction Channel is an activation handed to what it receives. The
    observation is what a Recording of this Run holds, and the injection is
    what replaces the FMU that used to produce it.
    """
    declared: dict[str, dict[str, str]] = {
        channel: {} for channel in fields_by_channel
    }
    for bind in binds:
        channel, field, text = _parse_binding(bind, fields_by_channel)
        if field in declared[channel]:
            raise ManifestError(
                f"binding {bind!r} binds Channel {channel!r} field {field!r} "
                f"twice; a field carries one FMU variable"
            )
        declared[channel][field] = text
    for channel, bound in declared.items():
        direction = init["channels"][channel]["direction"]
        _bind_group_channel(
            channel, direction, fields_by_channel[channel], bound, transceivers
        )


def _bind_group_channel(
    channel: str, direction: str, fields: dict[str, dict],
    bound: dict[str, str], transceivers: _Transceivers
) -> None:
    """Attach one Channel to the one terminal member it carries."""
    if len(bound) != 1:
        raise ManifestError(
            f"Channel {channel!r} binds {len(bound)} FMU variables; a Channel "
            f"of a group carries the activations of one terminal member, so it "
            f"binds one"
        )
    (field, text), = bound.items()
    member = _TX_DATA if direction == "out" else _RX_DATA
    transceiver, side = transceivers.carrying(
        text, f"Channel {channel!r} field {field!r}"
    )
    if side != member:
        raise ManifestError(
            f"Channel {channel!r} field {field!r} names the {side!r} member of "
            f"{transceiver}, and this Channel's direction is {direction!r}; an "
            f"out-direction Channel observes a terminal's {_TX_DATA!r} and an "
            f"in-direction Channel is handed to its {_RX_DATA!r}"
        )
    variable = _member_variable(
        str(transceiver), transceiver.instance, transceiver.terminal, member
    )
    causality = "input" if direction == "in" else "output"
    binary = _binary_field(
        _Binding(channel, field, variable), fields, causality
    )
    event_time = _event_time_field(channel, field, fields)
    carried = {binary.field, binary.length_field, event_time}
    for name in fields:
        if name not in carried:
            raise ManifestError(
                f"Channel {channel!r} declares schema field {name!r}, which a "
                f"clocked payload does not carry"
            )
    binary = _BinaryField(
        variable=variable, field=binary.field,
        length_field=binary.length_field, capacity=binary.capacity,
        event_time_field=event_time,
    )
    if direction == "out":
        if transceiver.observation is not None:
            raise ManifestError(
                f"Channel {channel!r} observes {transceiver}, which Channel "
                f"{transceiver.observation.channel!r} already observes; one "
                f"Channel carries a terminal's activations"
            )
        transceiver.observation = _Observation(channel, binary)
        return
    if transceiver.injection is not None:
        raise ManifestError(
            f"Channel {channel!r} is handed to {transceiver}, which Channel "
            f"{transceiver.injection.channel!r} is already handed to; one "
            f"Channel is a terminal's source"
        )
    if transceiver.peer is not None:
        raise ManifestError(
            f"Channel {channel!r} is handed to {transceiver}, which is "
            f"connected to {transceiver.peer}; a terminal takes its frames "
            f"from a connected peer or from a Channel, not from both"
        )
    transceiver.injection = _Injection(channel, binary)


def _group_start_values(
    starts: list[str], instances: dict[str, _Instance]
) -> dict[str, list[tuple[Variable, object]]]:
    """Resolve `--start <instance>.<variable>=<value>` per instance."""
    values: dict[str, list] = {name: [] for name in instances}
    for start in starts:
        target, separator, text = start.partition("=")
        if not separator or not target:
            raise ManifestError(
                f"start value {start!r} is not "
                f"'<instance>.<variable>=<value>'"
            )
        what = f"start value {start!r}"
        instance, variable_name = _instance_of(target, instances, what)
        variable = instance.description.variables.get(variable_name)
        if variable is None:
            raise ManifestError(
                f"{what} names FMU variable {variable_name!r}, which FMU "
                f"{instance.description.model_identifier!r} of instance "
                f"{instance.name!r} does not declare"
            )
        values[instance.name].append((variable, _start_value(variable, text)))
    return values


class FmuGroupParticipant(StepParticipant):
    """A process participant whose behavior is a group of connected FMUs'.

    The group is one participant because the coordination it does cannot be
    expressed between participants. A Channel's Latency is by default the
    subscriber's next activation, which is what makes a Run independent of
    execution order inside a Slot; a frame crossing a bus reaches its
    destination at an instant the bus computes, in the same instant its
    neighbours are standing on. Two separately stepped participants would
    deliver it a Slot later and lose the instant it happened at, and no
    Manifest Latency could restore it, because the delay is the bus model's
    output rather than a declaration.

    So FMI-specific event processing stays where it already was — at the
    Importer edge — and what the kernel sees is one process participant
    publishing bounded Messages on Channels it declares. The kernel learns
    nothing about Clocks, events, or transmission times.
    """

    def __init__(self, instances: Sequence[str], *, connects: Sequence[str] = (),
                 binds: Sequence[str] = (), starts: Sequence[str] = (),
                 profile: str = ""):
        self.name = ""  # the init line's, for the one diagnostic the kernel misses
        self._declared = list(instances)
        self._connects = list(connects)
        self._binds = list(binds)
        self._starts = list(starts)
        self._profile = profile
        self._extraction = None
        self._extracted: dict[str, Path] = {}
        self._group: _Group | None = None
        # What the event that ended initialization produced, held until the
        # first activation: the kernel has no Slot before it.
        self._pending_activations: list = []
        # Where the FMUs stand, in kernel nanoseconds. They stand together.
        self._communication_point = 0

    def on_init(self, init: dict) -> None:
        self.name = init["name"]
        instances = self._read(init)
        # Everything the Manifest got wrong is rejected before any FMU is
        # instantiated: a group that cannot hold is a fact about the Run's
        # configuration, and no FMU has to be loaded to see it.
        transceivers = _Transceivers(instances, self._profile)
        transceivers.build_every_terminal()
        _connect(self._connects, transceivers)
        _bind_group_channels(
            self._binds, init, _channel_fields(init), transceivers
        )
        starts = _group_start_values(self._starts, instances)
        # The group is held before any FMU is loaded, so an instance that
        # fails to instantiate or to initialize is still one this participant
        # closes: the instances already loaded are reachable from `close`.
        self._group = _Group(list(instances.values()), transceivers.all())
        for name, instance in instances.items():
            instance.instantiate(self._extracted[name], starts[name])
        self._pending_activations = self._group.initialize()

    def _read(self, init: dict) -> dict[str, _Instance]:
        """Extract every declared FMU and read what driving it depends on.

        Each archive is extracted into its own directory beneath this
        participant's own, because two instances of one FMU are two extractions
        of it: they are told apart by where they were extracted, and nothing
        one of them writes belongs to the other.
        """
        paths = _instance_paths(self._declared)
        if not paths:
            raise ManifestError(
                "a group declares at least one --instance <name>=<path>"
            )
        if not self._profile:
            raise ManifestError(
                "a group declares the BUS profile its terminals carry: "
                "--bus-profile <media-type>"
            )
        self._extraction = tempfile.TemporaryDirectory(
            prefix="sil-fmu-", dir=Path.cwd()
        )
        root = Path(self._extraction.name)
        self._extracted: dict[str, Path] = {}
        instances: dict[str, _Instance] = {}
        for name, path in paths.items():
            extracted = root / name
            extracted.mkdir()
            try:
                with zipfile.ZipFile(path) as archive:
                    archive.extractall(extracted)
            except (OSError, zipfile.BadZipFile) as error:
                raise ManifestError(
                    f"cannot read FMU {str(path)!r} of instance {name!r}: "
                    f"{error}"
                ) from error
            self._extracted[name] = extracted
            instances[name] = _Instance(name, ModelDescription.read(extracted))
        return instances

    def on_step(self, t: int, dt: int, inputs: list):
        """One Step of the group, and every Message the interval produced."""
        _require_contiguous(self._communication_point, t)
        published, self._pending_activations = self._pending_activations, []
        published.extend(self._group.advance(t, dt, inputs))
        self._communication_point = t + dt
        return published

    def close(self) -> None:
        """Terminate and free every instance, and drop the extracted FMUs."""
        group, extraction = self._group, self._extraction
        self._group = None
        self._extraction = None
        try:
            if group is not None:
                group.close()
        finally:
            if extraction is not None:
                extraction.cleanup()


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
        "--instance", action="append", default=[], metavar="NAME=PATH",
        help="declare one FMU of a connected group under a name of this "
             "Run's own; every other argument spells a variable of it as "
             "'<name>.<variable>'",
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


if __name__ == "__main__":
    main()
