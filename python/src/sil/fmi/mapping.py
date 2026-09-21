"""Reading the Run's declarations into the bindings a step then uses.

The init line says which Channels this participant carries, which way each one
runs and what fields its schema declares; `--bind` and `--start` say what the
FMU calls them. Everything here happens once, before any FMU is loaded, and
everything it rejects is a `ManifestError`: a mapping that cannot hold is a
fact about the Run's configuration rather than something the FMU has to be
asked about.

What comes out is `binding.py`'s objects — the step-time half of the mapping.
"""

from __future__ import annotations

from sil.participant import ManifestError

from sil.fmi.binding import (
    BinaryField,
    BinaryGroup,
    Binding,
    ChannelBinding,
    ClockedPayload,
    ScalarGroup,
    causality_of,
)
from sil.fmi.description import (
    BINARY,
    CLOCK,
    HAS_EVENT_MODE,
    SCALARS,
    TRIGGERED,
    ModelDescription,
    Variable,
    dimensions,
)

# The field a clocked Channel carries beside the payload and its length: the
# FMI event time the activation belongs to, in the kernel's own nanoseconds.
_EVENT_TIME_SUFFIX = "_event_time_ns"
_EVENT_TIME_TYPE = "u64"

# A bounded payload's length is a count of bytes, so a signed field would
# admit a length no payload can have. The ceiling is what each unsigned field
# can still count to: a length field that cannot reach its own payload
# field's bound describes a Channel whose payload can never fill it.
_LENGTH_CEILINGS = {
    "u8": 0xFF, "u16": 0xFFFF, "u32": 0xFFFF_FFFF,
    "u64": 0xFFFF_FFFF_FFFF_FFFF,
}


def channel_fields(init: dict) -> dict[str, dict[str, dict]]:
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


def derived_bindings(
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


def bound_fields(
    binds: list[str], fields_by_channel: dict[str, dict[str, dict]]
) -> dict[str, dict[str, str]]:
    """Every `--bind` argument, as the variable each Channel field names.

    The variable is still text here: what resolves it is the FMU, and one FMU
    on its own and a group of them name their variables differently.
    """
    bound: dict[str, dict[str, str]] = {
        channel: {} for channel in fields_by_channel
    }
    for bind in binds:
        channel, field, variable_name = _parse_binding(bind, fields_by_channel)
        if field in bound[channel]:
            raise ManifestError(
                f"binding {bind!r} binds Channel {channel!r} field {field!r} "
                f"twice; a field carries one FMU variable"
            )
        bound[channel][field] = variable_name
    return bound


def declared_bindings(
    binds: list[str],
    fields_by_channel: dict[str, dict[str, dict]],
    description: ModelDescription,
) -> dict[str, dict[str, Variable]]:
    """Resolve `--bind <channel>:<field>=<variable>` against both ends."""
    return {
        channel: {
            field: _declared_variable(channel, field, name, description)
            for field, name in declared.items()
        }
        for channel, declared in bound_fields(binds, fields_by_channel).items()
    }


def _declared_variable(
    channel: str, field: str, name: str, description: ModelDescription
) -> Variable:
    """The one FMU variable a binding names, or why the FMU has no such name."""
    variable = description.variables.get(name)
    if variable is None:
        raise ManifestError(
            f"Channel {channel!r} field {field!r} names FMU variable {name!r}, "
            f"which FMU {description.model_identifier!r} does not declare"
        )
    return variable


def _shape(field: dict) -> str:
    """How a schema field reads in a diagnostic: its type and its bound."""
    count = field.get("count")
    if count is None:
        return f"a {field['type']!r} scalar"
    return f"a {field['type']!r} array of {count}"


def _require_mappable(binding: Binding) -> None:
    """Reject a variable whose type or shape this importer does not map."""
    variable = binding.variable
    if variable.value_count != 1:
        raise ManifestError(
            f"{binding}, which declares {dimensions(variable)}; this importer "
            f"maps variables of one value"
        )
    if variable.kind == CLOCK:
        raise ManifestError(
            f"{binding}, which is a Clock variable; a Clock is driven through "
            f"the variable it gates rather than bound to a field of its own"
        )
    if variable.kind != BINARY and variable.kind not in SCALARS:
        raise ManifestError(
            f"{binding}, which is a {variable.kind} variable; this importer "
            f"maps Binary and the scalar types {', '.join(SCALARS)}"
        )


def _require_causality(binding: Binding, causality: str) -> None:
    """Reject a variable bound to a Channel of the other direction."""
    if binding.variable.causality != causality:
        raise ManifestError(
            f"{binding}, whose causality is {binding.variable.causality!r}; "
            f"this Channel's direction binds {causality} variables"
        )


def _require_field_type(binding: Binding, spec: dict) -> None:
    """Reject a field whose type is not the one the variable's type maps to."""
    scalar = SCALARS[binding.variable.kind]
    if spec.get("count") is not None or spec["type"] != scalar.field_type:
        raise ManifestError(
            f"Channel {binding.channel!r} declares field {binding.field!r} as "
            f"{_shape(spec)}; {binding.variable.kind} variable "
            f"{binding.variable.name!r} is carried by a "
            f"{scalar.field_type!r} scalar"
        )


def binary_field(
    binding: Binding, fields: dict[str, dict], causality: str
) -> BinaryField:
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
    return BinaryField(
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
    binding: Binding, description: ModelDescription
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
    if clock.interval_variability != TRIGGERED:
        raise ManifestError(
            f"{binding}, which is gated by Clock {clock.name!r} of "
            f"intervalVariability {clock.interval_variability!r}; this "
            f"importer drives {TRIGGERED!r} Clocks"
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
            f"{HAS_EVENT_MODE}=false, and a Clock is driven from Event Mode"
        )
    return clock


def event_time_field(
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


def clocked_payload(
    channel: str,
    direction: str,
    fields: dict[str, dict],
    bound: dict[str, Variable],
    description: ModelDescription,
) -> ClockedPayload | None:
    """The Clock-gated payload this Channel carries, if it carries one.

    A Channel carries one or none: its Messages are activations of a single
    Clock, and a second variable read beside them would be a Step's value
    published on an event's Message.
    """
    clocked = [field for field, variable in bound.items() if variable.clocks]
    if not clocked:
        return None
    causality = causality_of(direction)
    field = clocked[0]
    binding = Binding(channel, field, bound[field])
    if len(bound) > 1:
        others = ", ".join(sorted(repr(name) for name in bound if name != field))
        raise ManifestError(
            f"{binding}, which is gated by a Clock; Channel {channel!r} binds "
            f"{others} as well, and a clocked Channel carries one activation "
            f"and nothing else"
        )
    if binding.variable.kind != BINARY:
        raise ManifestError(
            f"{binding}, which is a clocked {binding.variable.kind} variable; "
            f"this importer carries a clocked variable as a bounded Binary "
            f"payload"
        )
    _require_mappable(binding)
    _require_causality(binding, causality)
    clock = _gating_clock(binding, description)
    binary = binary_field(binding, fields, causality)
    event_time = event_time_field(channel, field, fields)
    carried = {binary.field, binary.length_field, event_time}
    for declared in fields:
        if declared not in carried:
            raise ManifestError(
                f"Channel {channel!r} declares schema field {declared!r}, "
                f"which a clocked payload does not carry"
            )
    return ClockedPayload(
        BinaryField(
            variable=binary.variable, field=binary.field,
            length_field=binary.length_field, capacity=binary.capacity,
            clock=clock, event_time_field=event_time,
        ),
        causality,
    )


def bind_channel(
    channel: str,
    direction: str,
    fields: dict[str, dict],
    bound: dict[str, Variable],
) -> ChannelBinding:
    """One Channel's fields, checked against the variables they name."""
    causality = causality_of(direction)
    scalars: dict[str, list[Binding]] = {}
    binaries: list[BinaryField] = []
    lengths: dict[str, str] = {}
    for field in fields:
        if field not in bound:
            continue
        binding = Binding(channel, field, bound[field])
        _require_mappable(binding)
        _require_causality(binding, causality)
        if binding.variable.kind == BINARY:
            binary = binary_field(binding, fields, causality)
            lengths[binary.length_field] = field
            binaries.append(binary)
        else:
            _require_field_type(binding, fields[field])
            scalars.setdefault(binding.variable.kind, []).append(binding)
    _require_every_field_carried(channel, fields, bound, lengths)
    groups: list = [
        ScalarGroup(kind, bindings) for kind, bindings in scalars.items()
    ]
    if binaries:
        groups.append(BinaryGroup(binaries, causality))
    return ChannelBinding(groups)


def start_value(variable: Variable, text: str):
    """One start value, read out of a command argument by its own type."""
    if variable.kind == BINARY:
        try:
            return bytes.fromhex(text)
        except ValueError as error:
            raise ManifestError(
                f"start value for FMU variable {variable.name!r}: {text!r} is "
                f"not hexadecimal"
            ) from error
    if variable.kind not in SCALARS:
        raise ManifestError(
            f"start value for FMU variable {variable.name!r}: it is a "
            f"{variable.kind} variable, which this importer does not set"
        )
    scalar = SCALARS[variable.kind]
    try:
        value = scalar.parse(text)
    except ValueError as error:
        raise ManifestError(
            f"start value for FMU variable {variable.name!r}: cannot read "
            f"{text!r} as {variable.kind} ({error})"
        ) from error
    return value


def start_values(
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
        values.append((variable, start_value(variable, text)))
    return values
