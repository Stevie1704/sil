"""Reading a group's declarations into the instances and terminals it is.

`--instance`, `--connect`, `--bus-profile`, `--bind` and `--start` describe a
composition: which FMUs are in the group, which of their terminals are wired to
each other, which are carried by a Channel instead, and what each instance is
configured with. Resolving that is this module's whole job, and everything it
rejects is a `ManifestError` raised before any FMU is loaded.

It stands to `terminals.py` as `mapping.py` stands to `binding.py`: the
declarations are read and checked here, and what comes out is the objects the
group then drives.
"""

from __future__ import annotations

from pathlib import Path
from collections.abc import Sequence

from sil.participant import ManifestError

from sil.fmi.binding import Binding, BinaryField, causality_of
from sil.fmi.description import (
    BINARY,
    BUS_LAYERED_STANDARD,
    BUS_MANIFEST_MEMBER,
    CLOCK,
    COUNTDOWN,
    HAS_EVENT_MODE,
    NETWORK_TERMINAL,
    RX_CLOCK,
    RX_DATA,
    TRANSCEIVER,
    TRANSCEIVER_MEMBERS,
    TRIGGERED,
    TX_CLOCK,
    TX_DATA,
    Terminal,
    Variable,
    dimensions,
)
from sil.fmi.mapping import (
    binary_field,
    bound_fields,
    event_time_field,
    start_value,
)
from sil.fmi.runtime import ClockedBuffer, CountdownBuffer
from sil.fmi.terminals import Injection, Instance, Observation, Transceiver


def instance_paths(declared: Sequence[Sequence[str]]) -> dict[str, Path]:
    """Resolve `--instance <name> <path>` into the group's declaration order.

    The name and the path are two arguments rather than one joined by a
    separator, because the kernel resolves and digests a command argument that
    names a file: an FMU spelled into a larger argument would name no file, so
    it would neither anchor to the Manifest's directory nor reach the Run's
    provenance. The name is the group's own vocabulary, and every other
    argument spells a variable of it as `<name>.<variable>`.
    """
    paths: dict[str, Path] = {}
    for declaration in declared:
        name, path = declaration
        if not name or not path:
            raise ManifestError(
                f"instance {list(declaration)!r} names "
                f"{'no FMU' if name else 'nothing'}; an instance is a name and "
                f"the path of one FMU"
            )
        if "." in name:
            raise ManifestError(
                f"instance {name!r} carries a '.'; an instance name is what "
                f"qualifies a variable of it"
            )
        if name in paths:
            raise ManifestError(
                f"instance {name!r} is declared twice; an instance name names "
                f"one FMU of the group"
            )
        paths[name] = Path(path)
    return paths


def _instance_of(
    text: str, instances: dict[str, Instance], what: str
) -> tuple[Instance, str]:
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
    where: str, instance: Instance, terminal: Terminal, member: str
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
    where: str, instance: Instance, terminal: Terminal,
    data: str, clock: str, causality: str
) -> tuple[Variable, Variable]:
    """One data member and the Clock gating it, checked as the pair they are."""
    variable = _member_variable(where, instance, terminal, data)
    gate = _member_variable(where, instance, terminal, clock)
    if variable.kind != BINARY:
        raise ManifestError(
            f"{where} member {data!r} is variable {variable.name!r} of type "
            f"{variable.kind}; a transceiver carries bus operations as a "
            f"Binary buffer"
        )
    if variable.value_count != 1:
        raise ManifestError(
            f"{where} member {data!r} is variable {variable.name!r}, which "
            f"declares {dimensions(variable)}; this importer carries "
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
    if gate.kind != CLOCK:
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
    where: str, instance: Instance, terminal: Terminal, profile: str
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
            f"{BUS_MANIFEST_MEMBER}; the layered standard a terminal belongs "
            f"to is declared by the FMU rather than by the connection"
        )
    if terminal.kind != NETWORK_TERMINAL:
        raise ManifestError(
            f"{where} declares terminalKind {terminal.kind!r}; this importer "
            f"connects {NETWORK_TERMINAL!r} terminals"
        )
    if terminal.matching_rule != TRANSCEIVER:
        raise ManifestError(
            f"{where} declares matchingRule {terminal.matching_rule!r}; this "
            f"importer connects {TRANSCEIVER!r} terminals"
        )
    if not instance.description.has_event_mode:
        raise ManifestError(
            f"{where} is a network terminal gated by Clocks, and FMU "
            f"{instance.description.model_identifier!r} declares "
            f"{HAS_EVENT_MODE}=false; a Clock is driven from Event Mode"
        )
    for member in TRANSCEIVER_MEMBERS:
        if member not in terminal.members:
            raise ManifestError(
                f"{where} declares no member {member!r}; a {TRANSCEIVER!r} "
                f"terminal groups {', '.join(TRANSCEIVER_MEMBERS)}"
            )
    for member in (RX_DATA, TX_DATA):
        variable = _member_variable(where, instance, terminal, member)
        if variable.media_type != profile:
            raise ManifestError(
                f"{where} member {member!r} is variable {variable.name!r} of "
                f"mimeType {variable.mime_type!r}; this Run declares the BUS "
                f"profile {profile!r}"
            )


def build_transceiver(
    instance: Instance, terminal: Terminal, profile: str
) -> Transceiver:
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
        where, instance, terminal, RX_DATA, RX_CLOCK, "input"
    )
    if (receive_clock.causality, receive_clock.interval_variability) != (
        "input", TRIGGERED
    ):
        raise ManifestError(
            f"{where} member {RX_CLOCK!r} is Clock {receive_clock.name!r} of "
            f"causality {receive_clock.causality!r} and intervalVariability "
            f"{receive_clock.interval_variability!r}; a terminal is handed a "
            f"frame through an input {TRIGGERED!r} Clock"
        )
    send, send_clock = _member_pair(
        where, instance, terminal, TX_DATA, TX_CLOCK, "output"
    )
    built = Transceiver(
        instance, terminal,
        ClockedBuffer(receive, receive_clock, receive.max_size, incoming=True),
        receive.max_size,
    )
    profile_of_clock = (send_clock.causality, send_clock.interval_variability)
    if profile_of_clock == ("output", TRIGGERED):
        instance.triggered.append((
            built,
            ClockedBuffer(send, send_clock, send.max_size, incoming=False),
        ))
    elif profile_of_clock == ("input", COUNTDOWN):
        instance.countdown.append((built, CountdownBuffer(send, send_clock)))
    else:
        raise ManifestError(
            f"{where} member {TX_CLOCK!r} is Clock {send_clock.name!r} of "
            f"causality {send_clock.causality!r} and intervalVariability "
            f"{send_clock.interval_variability!r}; this importer drives an "
            f"output {TRIGGERED!r} Clock the FMU raises, or an input "
            f"{COUNTDOWN!r} Clock it asks the group to raise"
        )
    return built


class Transceivers:
    """Every terminal this Run drives, built once and named two ways.

    A terminal is named by a connection as `<instance>.<terminal>` and by a
    Channel through the variable one of its members is, so both spellings
    resolve to the same object: a terminal connected to a peer and watched by a
    Channel is one transceiver, not two.
    """

    def __init__(self, instances: dict[str, Instance], profile: str):
        self._instances = instances
        self._profile = profile
        self._built: dict[tuple[str, str], Transceiver] = {}
        self._by_variable: dict[tuple[str, str], tuple[Transceiver, str]] = {}

    def named(self, text: str, what: str) -> Transceiver:
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
        built = build_transceiver(instance, terminal, self._profile)
        self._built[key] = built
        for member in (RX_DATA, TX_DATA):
            self._by_variable[(instance.name, terminal.members[member])] = (
                built, member
            )
        return built

    def carrying(self, text: str, what: str) -> tuple[Transceiver, str]:
        """The transceiver member the variable `<instance>.<variable>` is.

        A Channel names a terminal through one of its members rather than by
        name, so the terminal is looked up by the variable and built here if a
        connection has not already built it. Only terminals this Run actually
        uses are built: an FMU may declare a terminal of another profile
        beside the one it is connected through, and that is its business.
        """
        instance, variable_name = _instance_of(text, self._instances, what)
        if variable_name not in instance.description.variables:
            raise ManifestError(
                f"{what} names FMU variable {variable_name!r}, which FMU "
                f"{instance.description.model_identifier!r} of instance "
                f"{instance.name!r} does not declare"
            )
        for terminal in instance.description.terminals.values():
            for member in (RX_DATA, TX_DATA):
                if terminal.members.get(member) == variable_name:
                    self.named(f"{instance.name}.{terminal.name}", what)
                    return self._by_variable[(instance.name, variable_name)]
        raise ManifestError(
            f"{what} names FMU variable {variable_name!r}, which is no "
            f"{TX_DATA!r} or {RX_DATA!r} member of a terminal of instance "
            f"{instance.name!r}; a group's Channels carry a terminal's bus "
            f"operations"
        )

    def require_every_instance_used(self) -> None:
        """Refuse an FMU of the group that nothing in the Run reaches.

        Every instance is stepped on every Step whether it communicates or
        not, so one no connection and no Channel names is an FMU paying for a
        Run it takes no part in — a Manifest mistake rather than a choice.
        """
        used = {instance for instance, _ in self._built}
        for name in self._instances:
            if name not in used:
                raise ManifestError(
                    f"instance {name!r} is named by no --connect and by no "
                    f"Channel; every FMU of a group drives a network terminal, "
                    f"connected to a peer or carried by a Channel"
                )

    def all(self) -> list[Transceiver]:
        return list(self._built.values())


def connect(declarations: list[str], transceivers: Transceivers) -> None:
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
    what: str, left: Transceiver, right: Transceiver
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
    # An undeclared version is not a version two ends agree on: comparing two
    # absent ones would let a manifest that says nothing about the layered
    # standard pass the check that exists to make it say something.
    for end in (left, right):
        if not end.instance.description.bus.version:
            raise ManifestError(
                f"{what}: {end} declares no {BUS_LAYERED_STANDARD} version in "
                f"{BUS_MANIFEST_MEMBER}; a connection is compatible at a "
                f"version both ends state"
            )
    if left.instance.description.bus.version != (
        right.instance.description.bus.version
    ):
        raise ManifestError(
            f"{what} connects "
            f"{left} at {BUS_LAYERED_STANDARD} "
            f"{left.instance.description.bus.version!r} to {right} at "
            f"{right.instance.description.bus.version!r}; both ends declare "
            f"one version of the layered standard"
        )
    for sender, receiver in ((left, right), (right, left)):
        sent = _member_variable(
            str(sender), sender.instance, sender.terminal, TX_DATA
        )
        taken = _member_variable(
            str(receiver), receiver.instance, receiver.terminal, RX_DATA
        )
        if sent.mime_type != taken.mime_type:
            raise ManifestError(
                f"{what}: {sender} sends {sent.mime_type!r} and {receiver} "
                f"takes {taken.mime_type!r}; the two ends of one direction "
                f"carry one profile"
            )


def bind_group_channels(
    binds: list[str], init: dict, fields_by_channel: dict[str, dict[str, dict]],
    transceivers: Transceivers
) -> None:
    """Attach every declared Channel to the terminal member it carries.

    A group's Channel carries one terminal's bus operations and nothing else:
    an out-direction Channel observes what a terminal sends, and an
    in-direction Channel is an activation handed to what it receives. The
    observation is what a Recording of this Run holds, and the injection is
    what replaces the FMU that used to produce it.
    """
    for channel, bound in bound_fields(binds, fields_by_channel).items():
        direction = init["channels"][channel]["direction"]
        _bind_group_channel(
            channel, direction, fields_by_channel[channel], bound, transceivers
        )


def _bind_group_channel(
    channel: str, direction: str, fields: dict[str, dict],
    bound: dict[str, str], transceivers: Transceivers
) -> None:
    """Attach one Channel to the one terminal member it carries."""
    if len(bound) != 1:
        raise ManifestError(
            f"Channel {channel!r} binds {len(bound)} FMU variables; a Channel "
            f"of a group carries the activations of one terminal member, so it "
            f"binds one"
        )
    (field, text), = bound.items()
    member = TX_DATA if direction == "out" else RX_DATA
    transceiver, side = transceivers.carrying(
        text, f"Channel {channel!r} field {field!r}"
    )
    if side != member:
        raise ManifestError(
            f"Channel {channel!r} field {field!r} names the {side!r} member of "
            f"{transceiver}, and this Channel's direction is {direction!r}; an "
            f"out-direction Channel observes a terminal's {TX_DATA!r} and an "
            f"in-direction Channel is handed to its {RX_DATA!r}"
        )
    variable = _member_variable(
        str(transceiver), transceiver.instance, transceiver.terminal, member
    )
    causality = causality_of(direction)
    binary = binary_field(
        Binding(channel, field, variable), fields, causality
    )
    event_time = event_time_field(channel, field, fields)
    carried = {binary.field, binary.length_field, event_time}
    for name in fields:
        if name not in carried:
            raise ManifestError(
                f"Channel {channel!r} declares schema field {name!r}, which a "
                f"clocked payload does not carry"
            )
    binary = BinaryField(
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
        transceiver.observation = Observation(channel, binary)
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
    transceiver.injection = Injection(channel, binary)


def group_start_values(
    starts: list[str], instances: dict[str, Instance]
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
        values[instance.name].append((variable, start_value(variable, text)))
    return values
