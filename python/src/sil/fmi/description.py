"""What an FMU's archive declares, read once before anything is loaded.

Three files carry it, and this module is the only one that parses any of them:
`modelDescription.xml` for the variables and the co-simulation capabilities,
`terminalsAndIcons.xml` for the terminals a layered standard connects, and the
FMI-LS-BUS manifest for the profile those terminals belong to. Everything above
reads the dataclasses here rather than XML, so an FMU that cannot be driven is
rejected in one place and for a stated reason.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable
from xml.etree import ElementTree

from sil.participant import ManifestError

# The one FMI version this importer drives. Anything else is rejected rather
# than half-driven.
_FMI_VERSION = "3.0"

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
    reinterprets. `to_field` is the value as the schema carries it, and
    `parse` reads a start value out of a command argument. What the FMI call
    itself takes is the native element `runtime.py` names for this same key.
    """

    field_type: str
    to_field: Callable
    parse: Callable


# Every FMI scalar type this importer maps, by the element name
# `modelDescription.xml` gives it: the two the acceptance fixture declares
# beside its Binary variables, and no more. Broad type coverage is outside
# this slice, so every other type — String, Enumeration, Clock, and the
# integer types — is reported rather than skipped when a binding names one.
SCALARS = {
    "Float64": _ScalarType("f64", float, float),
    # fmi3Boolean is a C `bool`, so a Channel carries it as a `u8` with C's
    # own conversion: zero is false and any other value is true. What the FMU
    # hands back is 0 or 1.
    "Boolean": _ScalarType("u8", int, _boolean),
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
    elements = root.find("ModelVariables")
    if elements is None:
        raise ValueError("it declares no ModelVariables")
    for element in elements:
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


def dimensions(variable: Variable) -> str:
    """How a variable's declared dimensions read in a diagnostic."""
    if variable.value_count is None:
        return "a dimension sized by another variable"
    return f"dimensions of {variable.value_count} values"
