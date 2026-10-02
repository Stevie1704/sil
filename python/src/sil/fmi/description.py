"""What an FMU's archive declares, read once before anything is loaded.

Three files carry it, and this module is the only one that parses any of them:
`modelDescription.xml` for the variables and the co-simulation capabilities,
`terminalsAndIcons.xml` for the terminals a layered standard connects, and the
FMI-LS-BUS manifest for the profile those terminals belong to. Everything above
reads the dataclasses here rather than XML, so an FMU that cannot be driven is
rejected in one place and for a stated reason.
"""

from __future__ import annotations

import math
import platform
import re
import struct
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from collections.abc import Callable
from xml.etree import ElementTree

from sil._schema_types import INT_RANGES, SIZES
from sil.participant import ManifestError

# The two FMI versions this importer drives, each against its own profile.
# Anything else is rejected rather than half-driven.
FMI3 = "3.0"
FMI2 = "2.0"

# The FMU's `binaries/` subdirectory for the running platform, and the shared
# library suffix that goes with it.
_MACHINES = {"arm64": "aarch64", "AMD64": "x86_64"}
_SYSTEMS = {"Darwin": ("darwin", ".dylib"), "Linux": ("linux", ".so")}


def _boolean(text: str) -> int:
    """A start value in the spelling `modelDescription.xml` uses."""
    if text not in ("true", "false"):
        raise ValueError(f"{text!r} is not 'true' or 'false'")
    return int(text == "true")


# A start value of an integer type is a plain decimal integer: an optional
# `-` and ASCII digits. Python's `int()` also reads a `+`, whitespace,
# underscores and non-ASCII digits, so it is not the grammar on its own.
_DECIMAL_INTEGER = re.compile(r"-?[0-9]+")
# A Float32 start value is the decimal grammar `sil-csv` reads an `f32` cell
# in, so one decimal is one Float32 on both paths into a Run.
_FLOAT = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?")
_NON_FINITE = re.compile(r"[+-]?(nan|inf|infinity)", re.IGNORECASE)
_F32 = struct.Struct("<f")


def _integer(kind: str, field_type: str) -> Callable[[str], int]:
    """The start-value reader of one integer type, bounded by its field."""
    low, high = INT_RANGES[field_type]

    def parse(text: str) -> int:
        if not _DECIMAL_INTEGER.fullmatch(text):
            raise ValueError(f"{text!r} is not a decimal integer")
        if low == 0 and text.startswith("-"):
            raise ValueError(f"{text!r} is negative, and {kind} is unsigned")
        value = int(text)
        if not low <= value <= high:
            raise ValueError(
                f"{value} is outside the {kind} range [{low}, {high}]"
            )
        return value

    return parse


def _float32(text: str) -> float:
    """A Float32 start value: a finite decimal, rounded to the nearest Float32.

    The decimal is read as the nearest binary64 first, which is how `sil-csv`
    reads an `f32` cell, then rounded to the nearest Float32 with ties to
    even. Nothing is clamped: a value that rounds to infinity, or a non-zero
    one that rounds to zero, is refused rather than carried as another value.
    """
    if _NON_FINITE.fullmatch(text):
        raise ValueError(f"{text!r} is not a finite number")
    if not _FLOAT.fullmatch(text):
        raise ValueError(f"{text!r} is not a decimal number")
    value = float(text)
    try:
        rounded = _F32.unpack(_F32.pack(value))[0]
    except OverflowError:
        rounded = math.inf
    if math.isinf(rounded):
        raise ValueError(f"{text} is outside the Float32 range")
    if rounded == 0 and Decimal(text) != 0:
        raise ValueError(f"{text} underflows to zero in Float32")
    return rounded


@dataclass(frozen=True)
class _ScalarType:
    """One FMI scalar type, as both ends of the mapping see it.

    `field_type` is the one schema field type that carries it — a Channel
    field of any other type is a binding this importer refuses rather than
    reinterprets. `to_field` is the value as the schema carries it, and
    `parse` reads a start value out of a command argument. What the FMI call
    itself takes is the native element `runtime.py` names for this same key.
    """

    field_type: str
    to_field: Callable
    parse: Callable
    # Whether a variable of this type may declare dimensions. An array is
    # carried by one fixed-count field of `field_type`, flattened in the
    # FMI-defined row-major order.
    array: bool = True


# Every FMI scalar type this importer maps, by the element name
# `modelDescription.xml` gives it: the two the CAN acceptance fixture declares
# beside its Binary variables, and the numeric profile of the C reference
# product — Float32 for sensor and control values, Int32 for signed selected
# IDs, UInt32 for counts, modes and sequence numbers, UInt64 for Sample
# times, UInt8 for validity flags and Int64 for signed ages. Each is carried
# by the one field type of its own width and kind, so no integer crosses a
# floating-point field. Every other type — String,
# Enumeration, Clock, and the other integer widths — is reported rather than
# skipped when a binding names one.
SCALARS = {
    "Float64": _ScalarType("f64", float, float),
    # fmi3Boolean is a C `bool`, so a Channel carries it as a `u8` with C's
    # own conversion: zero is false and any other value is true. What the FMU
    # hands back is 0 or 1.
    "Boolean": _ScalarType("u8", int, _boolean, array=False),
    "Float32": _ScalarType("f32", float, _float32),
    "Int32": _ScalarType("i32", int, _integer("Int32", "i32")),
    "UInt32": _ScalarType("u32", int, _integer("UInt32", "u32")),
    "UInt64": _ScalarType("u64", int, _integer("UInt64", "u64")),
    # A `u8` field carries a Boolean too; the variable's declared type
    # decides the conversion, so a UInt8 keeps every value in [0, 255].
    "UInt8": _ScalarType("u8", int, _integer("UInt8", "u8")),
    "Int64": _ScalarType("i64", int, _integer("Int64", "i64")),
}

BINARY = "Binary"
_FLOAT64 = "Float64"
CLOCK = "Clock"

# The two Clock kinds this importer drives. A `triggered` Clock is raised by

# importer on an input one. A `countdown` Clock is the FMU asking to be
# activated at an instant it computes itself, which only a group can promise,
# because only a group owns communication points between the kernel's Slots. A
# `periodic` Clock asks an importer to own a second time grid and is outside
# the profile either way.
TRIGGERED = "triggered"
COUNTDOWN = "countdown"

# The capability an FMU has to declare before this importer will use Event
# Mode, and the Clock profile is the only thing it is used for.
HAS_EVENT_MODE = "hasEventMode"

# The FMI-LS-BUS layered standard, as an FMU's own files name it: the archive
# member that declares it, the terminal kind and matching rule a network
# terminal carries, and the four members a transceiver terminal groups.
BUS_LAYERED_STANDARD = "org.fmi-standard.fmi-ls-bus"
BUS_MANIFEST_MEMBER = f"extra/{BUS_LAYERED_STANDARD}/fmi-ls-manifest.xml"
_LAYERED_STANDARD_NAMESPACE = "http://fmi-standard.org/fmi-ls-manifest"
_TERMINALS_MEMBER = "terminalsAndIcons/terminalsAndIcons.xml"
NETWORK_TERMINAL = "org.fmi-ls-bus.network-terminal"
TRANSCEIVER = "org.fmi-ls-bus.transceiver"
# The direction is the terminal owner's: `Tx_Data` is what it sends and
# `Rx_Data` is what it is handed, each gated by the Clock beside it.
TX_DATA, TX_CLOCK = "Tx_Data", "Tx_Clock"
RX_DATA, RX_CLOCK = "Rx_Data", "Rx_Clock"
TRANSCEIVER_MEMBERS = (RX_DATA, RX_CLOCK, TX_DATA, TX_CLOCK)

# A structural parameter is the one causality whose value FMI 3.0 has an
# importer change inside Configuration Mode rather than in the instantiated
# state. The CAN node of the acceptance fixture declares one.
STRUCTURAL = "structuralParameter"


def platform_directory() -> str:
    """The `binaries/` subdirectory this platform's shared library lives in."""
    machine = platform.machine()
    machine = _MACHINES.get(machine, machine)
    system, _ = _SYSTEMS[platform.system()]
    return f"{machine}-{system}"


# The FMI 2.0 `binaries/` directory for the running platform. FMI 2.0 names a
# platform by its system and word size only, so `linux64` is Linux x86-64 and
# no other Linux machine has a name. `darwin64` holds whatever the exporter
# built for macOS; it is a development host, not the qualified profile.
_FMI2_PLATFORMS = {
    ("Linux", "x86_64"): "linux64",
    ("Darwin", "x86_64"): "darwin64",
    ("Darwin", "arm64"): "darwin64",
}

# The FMI 2.0 types this profile maps, by the element name FMI 2.0 gives
# them, and the FMI 3.0 type of the same width each is carried as. A Real is a
# double, an Integer a C `int` and a Boolean a C `int` holding 0 or 1, so each
# is carried by the field and read by the start-value grammar of its FMI 3.0
# counterpart. Every other FMI 2.0 type refuses the FMU.
_FMI2_TYPES = {"Real": "Float64", "Integer": "Int32", "Boolean": "Boolean"}


def fmi2_platform_directory() -> str | None:
    """The FMI 2.0 `binaries/` directory of this platform, or None."""
    return _FMI2_PLATFORMS.get((platform.system(), platform.machine()))


def library_suffix() -> str:
    """The shared-library suffix this platform's FMU binary carries."""
    _, suffix = _SYSTEMS[platform.system()]
    return suffix


@dataclass(frozen=True)
class OsmpAddress:
    """The value references of the three fmi2Integer variables of OSMP.

    OSI Sensor Model Packaging passes one binary value as a memory address in
    two signed 32-bit halves and a byte count beside it.
    """

    lo: int
    hi: int
    size: int


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
    # variable, which the description does not settle, or is not positive.
    value_count: int | None
    # The value references of the Clocks that gate this variable. A variable
    # that declares one is defined only while that Clock is active.
    clocks: tuple[int, ...] = ()
    # Declared by a Clock only: what decides when it is active.
    interval_variability: str | None = None
    # Declared by a Binary variable only: the media type of what it carries.
    # A layered standard's profile is stated here, parameters included.
    mime_type: str | None = None
    # Each `<Dimension>` in declaration order: its literal `start`, or None
    # where another variable's value sizes it. Empty for a scalar.
    shape: tuple[int | None, ...] = ()
    # Set on the Binary variable an FMI 2.0 FMU declares through OSMP
    # annotations: the three Integer variables it is passed in.
    osmp: OsmpAddress | None = None
    # Set on each of those three Integer variables: the name of the OSMP
    # binary variable it is part of.
    osmp_member: str | None = None

    @property
    def is_array(self) -> bool:
        """Whether a Channel carries this variable as an array field.

        A variable whose dimensions hold one value is the scalar it was
        before arrays were mapped: `<Dimension start="1"/>` is one value
        written the long way.
        """
        return bool(self.shape) and self.value_count != 1

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


def _shape(element) -> tuple[int | None, ...]:
    """Each declared dimension: its literal start, or None when unresolved.

    A dimension without a `start` is sized by the variable its
    `valueReference` names, which only the FMU's configuration settles.
    """
    return tuple(
        None if dimension.get("start") is None else int(dimension.get("start"))
        for dimension in element.findall("Dimension")
    )


def _value_count(shape: tuple[int | None, ...]) -> int | None:
    """How many values one variable's declared dimensions amount to.

    The acceptance fixture's CAN node declares its Binary input as
    `<Dimension start="1"/>`, which is one value written the long way — the
    same variable a description without any Dimension declares.
    """
    if any(extent is None or extent < 1 for extent in shape):
        return None
    return math.prod(shape)


def _clock_references(element) -> tuple[int, ...]:
    """The Clocks one variable declares, as FMI 3.0 lists them.

    The attribute is a whitespace-separated list of value references, which is
    how a variable says it is defined only while those Clocks are active.
    """
    return tuple(int(reference) for reference in element.get("clocks", "").split())


def _variables(root) -> dict[str, Variable]:
    """Every variable the description declares, of every type."""
    declared: dict[str, Variable] = {}
    elements = root.find("ModelVariables")
    if elements is None:
        raise ValueError("it declares no ModelVariables")
    for element in elements:
        name, reference = element.get("name"), element.get("valueReference")
        if name is None or reference is None:
            continue
        max_size = element.get("maxSize")
        shape = _shape(element)
        declared[name] = Variable(
            name=name,
            reference=int(reference),
            kind=element.tag,
            causality=element.get("causality", "local"),
            max_size=None if max_size is None else int(max_size),
            value_count=_value_count(shape),
            clocks=_clock_references(element),
            interval_variability=element.get("intervalVariability"),
            mime_type=element.get("mimeType"),
            shape=shape,
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
    path = extracted / BUS_MANIFEST_MEMBER
    if not path.exists():
        return None
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError) as error:
        raise ManifestError(
            f"FMU carries an unreadable {BUS_MANIFEST_MEMBER}: {error}"
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
    # The FMI version the description declares, which decides the profile it
    # is checked against and the native interface it is driven through.
    fmi_version: str = FMI3

    def clock(self, reference: int) -> Variable | None:
        """The Clock one value reference names, if it names a Clock at all."""
        for variable in self.variables.values():
            if variable.reference == reference and variable.kind == CLOCK:
                return variable
        return None

    @property
    def has_event_mode(self) -> bool:
        """Whether the FMU declares the mode a Clock is driven from."""
        return self.capabilities.get(HAS_EVENT_MODE) == "true"

    @staticmethod
    def read(extracted: Path) -> ModelDescription:
        """Parse the description, rejecting an FMU this importer cannot drive.

        Each rejection is eager and specific: an FMU of another FMI version
        otherwise loads and fails on a missing symbol, and a Model Exchange
        FMU otherwise fails on an absent element.
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
        if version == FMI2:
            return _read_fmi2(root)
        if version != FMI3:
            raise ManifestError(
                f"FMU declares fmiVersion {version!r}; this importer drives "
                f"FMI {FMI3} and FMI {FMI2} co-simulation only"
            )
        co_simulation = root.find("CoSimulation")
        if co_simulation is None:
            raise ManifestError(
                "FMU declares no co-simulation interface; this importer "
                "drives neither Model Exchange nor Scheduled Execution"
            )
        try:
            variables = _variables(root)
        except ValueError as error:
            raise ManifestError(
                f"FMU declares a malformed modelDescription.xml: {error}"
            ) from error
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

    def platform_directory(self) -> str | None:
        """The `binaries/` directory this platform loads from, for this FMU.

        FMI 3.0 and FMI 2.0 name a platform differently, and FMI 2.0 has no
        name for some platforms at all.
        """
        if self.fmi_version == FMI2:
            return fmi2_platform_directory()
        return platform_directory()

    def binary(self, extracted: Path) -> Path:
        """The shared library this platform loads out of the FMU."""
        directory = self.platform_directory()
        if directory is None:
            raise ManifestError(
                f"FMU {self.model_identifier!r} is FMI {FMI2}, whose profile "
                f"loads binaries/linux64 on Linux x86-64; this host "
                f"({platform.system()} {platform.machine()}) has no FMI "
                f"{FMI2} platform directory"
            )
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


def _read_fmi2(root) -> ModelDescription:
    """An FMI 2.0 description, checked against the FMI 2.0 profile.

    The profile is co-simulation with scalar Real, Integer and Boolean
    variables. A variable of any other type refuses the FMU as a whole, and
    every such variable is named: an FMI 2.0 FMU has no other way to say
    which of its variables a Run may leave alone.
    """
    co_simulation = root.find("CoSimulation")
    if co_simulation is None:
        raise ManifestError(
            f"FMU declares fmiVersion {FMI2!r} and no co-simulation interface; "
            f"the FMI {FMI2} profile drives co-simulation only, not Model "
            f"Exchange"
        )
    guid = root.get("guid")
    if guid is None:
        raise ManifestError(
            f"FMU declares fmiVersion {FMI2!r} and no guid; an FMI {FMI2} "
            f"description names its instantiation token there"
        )
    try:
        variables = _fmi2_variables(root)
    except ValueError as error:
        raise ManifestError(
            f"FMU declares a malformed FMI {FMI2} modelDescription.xml: {error}"
        ) from error
    outside = [
        f"{variable.kind} variable {variable.name!r}"
        for variable in variables.values() if variable.kind not in SCALARS
    ]
    if outside:
        raise ManifestError(
            f"FMU declares {', '.join(outside)}; the FMI {FMI2} profile maps "
            f"{', '.join(_FMI2_TYPES)} variables only"
        )
    variables = _with_osmp(root, variables)
    return ModelDescription(
        model_identifier=co_simulation.get("modelIdentifier"),
        instantiation_token=guid,
        variables=variables,
        inputs=_by_causality(variables, "input"),
        outputs=_by_causality(variables, "output"),
        capabilities=dict(co_simulation.attrib),
        fmi_version=FMI2,
    )


def fmi2_type(element) -> ElementTree.Element | None:
    """The type element an FMI 2.0 `<ScalarVariable>` declares, if any."""
    return next(iter(element), None)


def _fmi2_variables(root) -> dict[str, Variable]:
    """Every FMI 2.0 scalar variable, in the importer's FMI 3.0 terms.

    A mapped type takes its FMI 3.0 counterpart's kind; any other type keeps
    its FMI 2.0 element name, so the profile check can name it.
    """
    elements = root.find("ModelVariables")
    if elements is None:
        raise ValueError("it declares no ModelVariables")
    declared: dict[str, Variable] = {}
    for element in elements.iter("ScalarVariable"):
        name, reference = element.get("name"), element.get("valueReference")
        declared_type = fmi2_type(element)
        if name is None or reference is None or declared_type is None:
            raise ValueError(
                f"ScalarVariable {name!r} declares no name, valueReference "
                f"or type"
            )
        declared[name] = Variable(
            name=name,
            reference=int(reference),
            kind=_FMI2_TYPES.get(declared_type.tag, declared_type.tag),
            causality=element.get("causality", "local"),
            max_size=None,
            value_count=1,
        )
    return declared


# OSI Sensor Model Packaging: the tool name of its annotations, the namespace
# of their elements, and the three roles of one binary variable's Integers.
OSMP_TOOL = "net.pmsf.osmp"
_OSMP_NAMESPACE = "{http://xsd.pmsf.net/OSISensorModelPackaging}"
OSMP_ROLES = ("base.lo", "base.hi", "size")
OSMP_INTEGER = _FMI2_TYPES["Integer"]


def _osmp_annotation(element) -> ElementTree.Element | None:
    """The OSMP binary-variable annotation of one `<ScalarVariable>`, if any."""
    for tool in element.iterfind("Annotations/Tool"):
        if tool.get("name") != OSMP_TOOL:
            continue
        annotation = tool.find(f"{_OSMP_NAMESPACE}osmp-binary-variable")
        if annotation is None:
            raise ManifestError(
                f"FMU variable {element.get('name')!r} carries an "
                f"{OSMP_TOOL} annotation without an osmp-binary-variable"
            )
        return annotation
    return None


def _declares_osmp(root) -> bool:
    """Whether the model declares OSMP in its vendor annotations."""
    return any(
        tool.find(f"{_OSMP_NAMESPACE}osmp") is not None
        for tool in root.iterfind("VendorAnnotations/Tool")
        if tool.get("name") == OSMP_TOOL
    )


def _with_osmp(root, variables: dict[str, Variable]) -> dict[str, Variable]:
    """The variables, with each OSMP binary variable added as one Binary.

    Each Integer of a triple keeps its own entry and names the binary
    variable it is part of, so a Run that binds it alone can be told what
    to bind instead. Every annotation is checked here: an address that is
    half declared would be passed on as a number.
    """
    triples: dict[str, dict[str, tuple[Variable, str | None]]] = {}
    for element in root.find("ModelVariables").iter("ScalarVariable"):
        annotation = _osmp_annotation(element)
        if annotation is None:
            continue
        member = variables[element.get("name")]
        binary, role = annotation.get("name"), annotation.get("role")
        if not binary:
            raise ManifestError(
                f"FMU variable {member.name!r} carries an OSMP annotation "
                f"that names no binary variable"
            )
        if role not in OSMP_ROLES:
            raise ManifestError(
                f"FMU variable {member.name!r} declares OSMP role {role!r} of "
                f"binary variable {binary!r}; OSMP declares the roles "
                f"{', '.join(repr(r) for r in OSMP_ROLES)}"
            )
        roles = triples.setdefault(binary, {})
        if role in roles:
            raise ManifestError(
                f"FMU declares OSMP role {role!r} twice for binary variable "
                f"{binary!r}: {roles[role][0].name!r} and {member.name!r}"
            )
        roles[role] = (member, annotation.get("mime-type"))
    if triples and not _declares_osmp(root):
        raise ManifestError(
            f"FMU annotates OSMP binary variables "
            f"{', '.join(repr(b) for b in triples)} but declares no OSMP "
            f"annotation under VendorAnnotations"
        )
    declared = dict(variables)
    for binary, roles in triples.items():
        declared[binary] = _osmp_binary(binary, roles, variables)
        for member, _ in roles.values():
            declared[member.name] = replace(member, osmp_member=binary)
    return declared


def _osmp_binary(
    binary: str,
    roles: dict[str, tuple[Variable, str | None]],
    variables: dict[str, Variable],
) -> Variable:
    """One OSMP binary variable, checked against its three Integers."""
    missing = [role for role in OSMP_ROLES if role not in roles]
    if missing:
        raise ManifestError(
            f"FMU declares OSMP binary variable {binary!r} without the "
            f"{', '.join(repr(r) for r in missing)} role; one binary "
            f"variable is the three Integers "
            f"{', '.join(repr(r) for r in OSMP_ROLES)}"
        )
    if binary in variables:
        raise ManifestError(
            f"FMU declares OSMP binary variable {binary!r}, a name that "
            f"already names an FMU variable"
        )
    for role, (member, _) in roles.items():
        if member.kind != OSMP_INTEGER:
            raise ManifestError(
                f"FMU declares {member.name!r}, the {role!r} of OSMP binary "
                f"variable {binary!r}, as {member.kind}; OSMP passes it in "
                f"an Integer"
            )
        if member.name != f"{binary}.{role}":
            raise ManifestError(
                f"FMU declares {member.name!r} as the {role!r} of OSMP binary "
                f"variable {binary!r}; OSMP names it {f'{binary}.{role}'!r}"
            )
    members = [member for member, _ in roles.values()]
    causalities = {member.causality for member in members}
    if len(causalities) != 1:
        raise ManifestError(
            f"FMU declares the Integers of OSMP binary variable {binary!r} "
            f"with causality {', '.join(sorted(map(repr, causalities)))}; "
            f"the three are one variable of one causality"
        )
    mime_types = {mime_type for _, mime_type in roles.values()}
    if len(mime_types) != 1:
        raise ManifestError(
            f"FMU annotates the Integers of OSMP binary variable {binary!r} "
            f"with mime-type "
            f"{', '.join(sorted(repr(m) for m in mime_types))}; the three "
            f"carry one value of one type"
        )
    address = OsmpAddress(
        *(roles[role][0].reference for role in OSMP_ROLES)
    )
    return Variable(
        name=binary,
        reference=address.lo,
        kind=BINARY,
        causality=causalities.pop(),
        max_size=None,
        value_count=1,
        mime_type=mime_types.pop(),
        osmp=address,
    )


def dimensions(variable: Variable) -> str:
    """How a variable's declared dimensions read in a diagnostic."""
    if None in variable.shape:
        return "a dimension sized by another variable"
    shape = "x".join(str(extent) for extent in variable.shape)
    if variable.value_count is None:
        return f"dimensions [{shape}], which are not all positive"
    return f"dimensions [{shape}] of {variable.value_count} values"


# The most bytes one value buffer may span: what `size_t` counts on the
# 64-bit platforms this importer supports. An array past it could neither be
# allocated nor named by the `nValues` of an FMI call.
_SIZE_MAX = (1 << 64) - 1


def array_problem(variable: Variable) -> str | None:
    """Why this importer carries no Channel field for an array, or None.

    The reason completes a sentence that opens by naming the variable. A
    variable of one value is a scalar and has none.
    """
    if not variable.is_array:
        return None
    scalar = SCALARS.get(variable.kind)
    if scalar is None or not scalar.array:
        return (
            f"which declares {dimensions(variable)}; this importer maps "
            f"arrays of {', '.join(k for k, s in SCALARS.items() if s.array)}"
            f" only"
        )
    if None in variable.shape:
        return (
            f"which declares {dimensions(variable)}; this importer maps "
            f"dimensions with a literal start only"
        )
    if variable.value_count is None:
        return (
            f"which declares {dimensions(variable)}; a dimension is a "
            f"positive integer"
        )
    if variable.value_count > _SIZE_MAX // SIZES[scalar.field_type]:
        return (
            f"which declares {dimensions(variable)}; that many "
            f"{scalar.field_type!r} values overflow a size_t buffer"
        )
    return None
