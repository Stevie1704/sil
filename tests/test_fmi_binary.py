"""Bounded Binary payloads and the other FMI variable types, through the importer.

The conformance target is the same vendored Reference FMU the Float64 suite
drives: `Feedthrough` copies each input to the output of the same name, its
`Binary_input` and `Binary_output` among them, so a Run over it proves the
whole path — the Channel's bounded payload is written into the FMU's Binary
variable with its own length, the FMU is stepped, and what it hands back is
published on the Channel it publishes.

A Binary variable is variable-length and a Channel Message is not, so the
Channel carries the bound: a `u8` array field sized to the most the Run admits,
and the `<field>_length` beside it carrying how much of it is the payload.
Nothing is truncated to fit — a payload that does not fit the bound aborts the
Run.

Mapping a variable that is not a Float64 is declared rather than derived:
`--bind <channel>:<field>=<variable>` names both ends, because the FMU's own
names (`CanChannel.Tx_Data`) and the Channel's (`data`) are not the same
vocabulary, and one schema is typically carried by more than one Channel.
"""

import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import quoteattr

import pytest
from conftest import ROOT
from sil.fmi import CoSimulation, FmuParticipant
from sil.manifest import Manifest, SubscriberRoute
from sil.participant import Input, ManifestError, ParticipantFailure
from sil.testing import run_simulation


def _load(name: str, path: Path):
    """Import a module that lives outside the importable packages."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STIMULUS = ROOT / "tests" / "participants" / "binary_stimulus.py"
# The stimulus states what it publishes; restating it here would let the two
# drift and still agree.
payload = _load("binary_stimulus", STIMULUS).payload

FIXTURES = ROOT / "tests" / "fixtures" / "reference-fmus" / "3.0"
FEEDTHROUGH = FIXTURES / "Feedthrough.fmu"

# `Feedthrough` copies at most 128 bytes between its Binary variables, which is
# the most a Channel bound to them can carry without the FMU refusing the
# payload. It declares no `maxSize`, so that ceiling is the FMU's contract
# rather than something the importer can check before stepping.
FMU_BINARY_BYTES = 128

STEP_PERIOD_NS = 10_000_000

IN = "bin.In"
OUT = "bin.Out"
PAYLOAD_SCHEMA = "bin.Payload"

# The bounded representation: the length actually used, then the payload bytes
# the Channel admits. One schema, carried by both Channels — which is why a
# binding names the Channel as well as the field.
PAYLOAD_SCHEMAS = {
    PAYLOAD_SCHEMA: {
        "fields": [
            {"name": "payload_length", "type": "u16"},
            {"name": "payload", "type": "u8", "count": FMU_BINARY_BYTES},
        ]
    }
}

PAYLOAD_BINDINGS = [
    f"{IN}:payload=Binary_input",
    f"{OUT}:payload=Binary_output",
]


def init_line(channels: dict[str, tuple[str, str]], schemas: dict) -> dict:
    """The initialization line the kernel would send for these Channels."""
    return {
        "op": "init",
        "name": "fmu",
        "schemas": schemas,
        "channels": {
            channel: {"schema": schema, "direction": direction}
            for channel, (schema, direction) in channels.items()
        },
    }


PAYLOAD_CHANNELS = {IN: (PAYLOAD_SCHEMA, "in"), OUT: (PAYLOAD_SCHEMA, "out")}


@pytest.fixture
def importer():
    """Build an initialized importer, torn down after the test."""
    built = []

    def _build(*, binds=PAYLOAD_BINDINGS, starts=(),
               channels=PAYLOAD_CHANNELS, schemas=PAYLOAD_SCHEMAS,
               fmu=FEEDTHROUGH):
        participant = FmuParticipant(fmu, binds=list(binds), starts=list(starts))
        built.append(participant)
        participant.on_init(init_line(channels, schemas))
        return participant

    yield _build
    for participant in built:
        # A test that broke the FMU has already said so; terminating an
        # instance that answered Error fails in turn.
        try:
            participant.close()
        except ParticipantFailure:
            pass


def record_calls(monkeypatch) -> list[str]:
    """Every co-simulation entry point the importer calls, in order."""
    calls: list[str] = []
    called = CoSimulation._call

    def record(self, name, *arguments):
        calls.append(name)
        return called(self, name, *arguments)

    monkeypatch.setattr(CoSimulation, "_call", record)
    return calls


def message(payload_bytes: bytes, capacity: int = FMU_BINARY_BYTES) -> dict:
    """One Message carrying `payload_bytes` on a Channel of that bound."""
    return {
        "payload_length": len(payload_bytes),
        "payload": payload_bytes.ljust(capacity, b"\x00"),
    }


def round_trip(participant, payload_bytes: bytes, capacity=FMU_BINARY_BYTES,
               step: int = 0):
    """Publish one payload into the FMU and read what it hands back.

    `step` is the step index, because an FMU is stepped forward: a second
    round trip on one instance is the next communication point.
    """
    (channel, fields), = participant.on_step(
        step * STEP_PERIOD_NS, STEP_PERIOD_NS,
        [Input(IN, step * STEP_PERIOD_NS, message(payload_bytes, capacity))],
    )
    assert channel == OUT
    return fields


class TestBoundedPayloads:
    """A variable-length Binary value carried by a fixed-layout Message."""

    def test_arbitrary_bytes_survive_the_round_trip(self, importer):
        """Embedded zeros included: the length says what the payload is."""
        payload_bytes = b"\x00\xff\x00\x10\x00\x00\x7f"
        fields = round_trip(importer(), payload_bytes)
        assert fields["payload_length"] == len(payload_bytes)
        assert fields["payload"][:len(payload_bytes)] == payload_bytes

    def test_an_empty_payload_survives_the_round_trip(self, importer):
        fields = round_trip(importer(), b"")
        assert fields["payload_length"] == 0
        assert fields["payload"] == bytes(FMU_BINARY_BYTES)

    def test_a_payload_filling_the_bound_survives_the_round_trip(self, importer):
        payload_bytes = bytes((index * 7) % 256 for index in range(FMU_BINARY_BYTES))
        fields = round_trip(importer(), payload_bytes)
        assert fields["payload_length"] == FMU_BINARY_BYTES
        assert fields["payload"] == payload_bytes

    def test_the_bytes_beyond_the_payload_are_zero(self, importer):
        """Unused bytes are deterministic, not whatever the buffer held.

        The same payload published after a longer one must produce the same
        Message, or the Recording would carry the previous payload's tail.
        """
        participant = importer()
        round_trip(participant, bytes(FMU_BINARY_BYTES), step=0)
        round_trip(participant, b"\xaa" * 64, step=1)
        fields = round_trip(participant, b"\x01\x02", step=2)
        assert fields["payload"] == b"\x01\x02" + bytes(FMU_BINARY_BYTES - 2)

    def test_the_length_a_message_declares_bounds_what_is_written(self, importer):
        """The payload field is always full-width; the length is the payload."""
        tailed = message(b"\x01\x02\x03")
        tailed["payload"] = b"\x01\x02\x03" + b"\xff" * (FMU_BINARY_BYTES - 3)
        (_, fields), = importer().on_step(
            0, STEP_PERIOD_NS, [Input(IN, 0, tailed)]
        )
        assert fields["payload_length"] == 3
        assert fields["payload"] == b"\x01\x02\x03" + bytes(FMU_BINARY_BYTES - 3)


def with_description(tmp_path, name: str, rewrite) -> Path:
    """A copy of the Reference FMU whose description has been rewritten."""
    path = tmp_path / f"{name}.fmu"
    with zipfile.ZipFile(FEEDTHROUGH) as source, zipfile.ZipFile(path, "w") as target:
        for member in source.infolist():
            data = source.read(member.filename)
            if member.filename == "modelDescription.xml":
                data = rewrite(data.decode()).encode()
            target.writestr(member, data)
    return path


BINARY_INPUT = '<Binary name="Binary_input" valueReference="31" causality="input">'


def dimensioned(tmp_path, name: str, dimension: str) -> Path:
    """The Reference FMU with one Dimension added to its Binary input."""
    return with_description(
        tmp_path, name,
        lambda text: text.replace(BINARY_INPUT, BINARY_INPUT + dimension),
    )


class TestDeclaredDimensions:
    """`<Dimension start="1"/>` is one value written the long way.

    The acceptance fixture's CAN node declares its Binary input exactly so
    (`proofs/fmi-ls-bus/evidence/profile.json`), so an importer that refused
    every declared dimension could not carry a payload into the one FMU this
    milestone exists for.
    """

    def test_a_dimension_of_one_value_is_carried_like_no_dimension_at_all(
        self, importer, tmp_path
    ):
        one_value = dimensioned(tmp_path, "one-value", '<Dimension start="1"/>')
        payload_bytes = b"\x00\x01\x00\x02"
        fields = round_trip(importer(fmu=one_value), payload_bytes)
        assert fields["payload_length"] == len(payload_bytes)
        assert fields["payload"][:len(payload_bytes)] == payload_bytes


class TestPayloadsThatDoNotFit:
    """An oversize payload aborts the Run; nothing is truncated to fit."""

    def test_a_length_above_the_channel_bound_is_refused(self, importer):
        over_declared = message(b"\x01" * 8)
        over_declared["payload_length"] = FMU_BINARY_BYTES + 1
        with pytest.raises(ParticipantFailure, match="payload_length"):
            importer().on_step(0, STEP_PERIOD_NS, [Input(IN, 0, over_declared)])

    def test_a_payload_above_the_fmus_own_bound_is_refused_by_the_fmu(
        self, importer
    ):
        """A Channel may admit more than the FMU does; the FMU then says so."""
        capacity = FMU_BINARY_BYTES * 2
        schemas = {
            PAYLOAD_SCHEMA: {"fields": [
                {"name": "payload_length", "type": "u16"},
                {"name": "payload", "type": "u8", "count": capacity},
            ]}
        }
        participant = importer(schemas=schemas)
        with pytest.raises(ParticipantFailure, match="fmi3SetBinary returned Error"):
            round_trip(participant, b"\x01" * (FMU_BINARY_BYTES + 1), capacity)

    def test_a_produced_payload_above_the_channel_bound_is_refused(self, importer):
        """The published Channel is bounded too, and its bound is not a ceiling
        the FMU knows about — so what does not fit fails rather than arrives
        cut short."""
        schemas = {
            PAYLOAD_SCHEMA: {"fields": [
                {"name": "payload_length", "type": "u16"},
                {"name": "payload", "type": "u8", "count": FMU_BINARY_BYTES},
            ]},
            "bin.Small": {"fields": [
                {"name": "payload_length", "type": "u16"},
                {"name": "payload", "type": "u8", "count": 8},
            ]},
        }
        participant = importer(
            schemas=schemas,
            channels={IN: (PAYLOAD_SCHEMA, "in"), OUT: ("bin.Small", "out")},
        )
        with pytest.raises(ParticipantFailure, match="produced 16 bytes"):
            participant.on_step(
                0, STEP_PERIOD_NS, [Input(IN, 0, message(b"\x01" * 16))]
            )


SCALAR_SCHEMAS = {
    "fmu.ScalarIn": {"fields": [
        {"name": "value", "type": "f64"},
        {"name": "flag", "type": "u8"},
    ]},
    "fmu.ScalarOut": {"fields": [
        {"name": "value", "type": "f64"},
        {"name": "flag", "type": "u8"},
    ]},
}
SCALAR_CHANNELS = {
    "s.In": ("fmu.ScalarIn", "in"), "s.Out": ("fmu.ScalarOut", "out"),
}
SCALAR_BINDINGS = [
    "s.In:value=Float64_discrete_input", "s.Out:value=Float64_discrete_output",
    "s.In:flag=Boolean_input", "s.Out:flag=Boolean_output",
]


class TestScalarTypes:
    """The scalar types the fixture declares beside its Binary variables.

    Float64 the derived mapping already carried; Boolean is the type the CAN
    node's structural parameter needs. Every other FMI type stays outside
    this slice and is reported rather than mapped.
    """

    def test_each_scalar_type_survives_the_round_trip(self, importer):
        participant = importer(
            binds=SCALAR_BINDINGS,
            channels=SCALAR_CHANNELS,
            schemas=SCALAR_SCHEMAS,
        )
        written = {"value": -12.5, "flag": 1}
        (_, published), = participant.on_step(
            0, STEP_PERIOD_NS, [Input("s.In", 0, written)]
        )
        assert published == written

    def test_a_boolean_is_carried_by_a_byte_with_cs_own_conversion(
        self, importer
    ):
        """`fmi3Boolean` is a C `bool`: any non-zero byte is true, and what
        the FMU hands back is 0 or 1. A `u8` field carries both ends of that,
        so the conversion is stated here rather than left to be discovered."""
        participant = importer(
            binds=["s.In:flag=Boolean_input", "s.Out:flag=Boolean_output"],
            channels={"s.In": ("fmu.Flag", "in"), "s.Out": ("fmu.Flag", "out")},
            schemas={"fmu.Flag": {"fields": [{"name": "flag", "type": "u8"}]}},
        )
        (_, published), = participant.on_step(
            0, STEP_PERIOD_NS, [Input("s.In", 0, {"flag": 2})]
        )
        assert published == {"flag": 1}

    def test_an_integer_variable_is_outside_this_slice(self, importer):
        """Reported as the type it is, rather than silently left unmapped."""
        with pytest.raises(ManifestError, match="Int32 variable"):
            importer(
                binds=["t.In:value=Int32_input"],
                channels={"t.In": ("t.Value", "in")},
                schemas={"t.Value": {"fields": [{"name": "value", "type": "i32"}]}},
            )


class TestStartValues:
    """Initialization values, written before initialization mode is entered."""

    def test_a_structural_parameter_is_written_in_configuration_mode(
        self, importer, monkeypatch, tmp_path
    ):
        """FMI 3.0 has a structural parameter changed in Configuration Mode.

        The Reference FMU accepts the write in the instantiated state too, so
        its own answer cannot tell the two sequences apart; the call seam is
        where the contract is visible.
        """
        calls = record_calls(monkeypatch)
        structural = with_description(
            tmp_path, "structural",
            lambda text: text.replace(
                'name="Float64_fixed_parameter" valueReference="5" '
                'causality="parameter"',
                'name="Float64_fixed_parameter" valueReference="5" '
                'causality="structuralParameter"',
            ),
        )
        importer(
            fmu=structural,
            binds=[f"{OUT}:payload=Binary_output"],
            starts=["Float64_fixed_parameter=2.5"],
            channels={OUT: (PAYLOAD_SCHEMA, "out")},
        )
        assert calls[:4] == [
            "fmi3EnterConfigurationMode", "fmi3SetFloat64",
            "fmi3ExitConfigurationMode", "fmi3EnterInitializationMode",
        ]

    def test_an_ordinary_variable_is_written_without_configuring(
        self, importer, monkeypatch
    ):
        """An FMU with no structural parameter is never asked to configure."""
        calls = record_calls(monkeypatch)
        importer(
            binds=[f"{OUT}:payload=Binary_output"],
            starts=["Binary_input=00ff00"],
            channels={OUT: (PAYLOAD_SCHEMA, "out")},
        )
        assert "fmi3EnterConfigurationMode" not in calls
        assert calls[:2] == ["fmi3SetBinary", "fmi3EnterInitializationMode"]

    def test_a_start_value_is_what_an_unfed_step_sees(self, importer):
        participant = importer(
            binds=[f"{OUT}:payload=Binary_output"],
            starts=["Binary_input=00ff00"],
            channels={OUT: (PAYLOAD_SCHEMA, "out")},
        )
        (_, fields), = participant.on_step(0, STEP_PERIOD_NS, [])
        assert fields["payload_length"] == 3
        assert fields["payload"][:3] == b"\x00\xff\x00"

    def test_a_scalar_start_value_is_what_an_unfed_step_sees(self, importer):
        participant = importer(
            binds=["s.Out:value=Float64_discrete_output",
                   "s.Out:flag=Boolean_output"],
            starts=["Float64_discrete_input=-7.25", "Boolean_input=true"],
            channels={"s.Out": ("fmu.ScalarOut", "out")},
            schemas=SCALAR_SCHEMAS,
        )
        (_, fields), = participant.on_step(0, STEP_PERIOD_NS, [])
        assert fields == {"value": -7.25, "flag": 1}

    def test_the_fmus_own_start_value_stands_when_none_is_given(self, importer):
        """`Feedthrough` starts its Binary variables at `foo`."""
        participant = importer(
            binds=[f"{OUT}:payload=Binary_output"], channels={OUT: (PAYLOAD_SCHEMA, "out")}
        )
        (_, fields), = participant.on_step(0, STEP_PERIOD_NS, [])
        assert fields["payload"][:fields["payload_length"]] == b"foo"

    @pytest.mark.parametrize(
        ("start", "expected"),
        [
            ("Nope=1", "does not declare"),
            ("Float64_continuous_input=nan-ish", "Float64"),
            ("Boolean_input=yes", "'true'"),
            ("Binary_input=zz", "hexadecimal"),
            ("String_input=x", "String variable"),
            ("Int32_input=3", "Int32 variable"),
            ("Binary_input", "'<variable>=<value>'"),
        ],
    )
    def test_a_start_value_that_cannot_be_honoured_is_rejected(
        self, importer, start, expected
    ):
        with pytest.raises(ManifestError, match=expected):
            importer(binds=[f"{OUT}:payload=Binary_output"], starts=[start],
                     channels={OUT: (PAYLOAD_SCHEMA, "out")})


class TestBindingsRejectedBeforeStepping:
    """Every binding the importer cannot honour, named before the FMU is stepped.

    Each is a Manifest error: the initialization line and the command's
    bindings describe a mapping that does not exist, which is a fact about the
    Run's configuration rather than about its behavior.
    """

    def test_a_binding_naming_no_fmu_variable_is_rejected(self, importer):
        with pytest.raises(ManifestError, match="Binary_missing"):
            importer(binds=[f"{IN}:payload=Binary_missing", *PAYLOAD_BINDINGS[1:]])

    def test_a_binding_naming_a_variable_of_an_unsupported_type_is_rejected(
        self, importer
    ):
        with pytest.raises(ManifestError, match="String variable"):
            importer(
                binds=["t.In:text=String_input"],
                channels={"t.In": ("t.Text", "in")},
                schemas={"t.Text": {"fields": [
                    {"name": "text", "type": "u8", "count": 8}
                ]}},
            )

    def test_a_binding_naming_an_enumeration_is_rejected(self, importer):
        with pytest.raises(ManifestError, match="Enumeration variable"):
            importer(
                binds=["t.In:option=Enumeration_input"],
                channels={"t.In": ("t.Option", "in")},
                schemas={"t.Option": {"fields": [{"name": "option", "type": "i64"}]}},
            )

    def test_a_binding_naming_a_variable_of_several_values_is_rejected(
        self, importer, tmp_path
    ):
        """An array variable is neither a scalar nor a bounded Binary."""
        array_fmu = dimensioned(tmp_path, "array", '<Dimension start="2"/>')
        with pytest.raises(ManifestError, match="dimensions of 2 values"):
            importer(fmu=array_fmu)

    def test_a_binding_naming_a_variable_sized_by_another_is_rejected(
        self, importer, tmp_path
    ):
        """A dimension the description does not settle is not a bound."""
        sized = dimensioned(tmp_path, "sized", '<Dimension valueReference="5"/>')
        with pytest.raises(ManifestError, match="sized by another variable"):
            importer(fmu=sized)

    def test_a_binding_whose_field_type_is_not_the_variables_is_rejected(
        self, importer
    ):
        with pytest.raises(ManifestError, match="'f64'"):
            importer(
                binds=["t.In:value=Float64_discrete_input"],
                channels={"t.In": ("t.Value", "in")},
                schemas={"t.Value": {"fields": [{"name": "value", "type": "f32"}]}},
            )

    def test_a_binary_variable_bound_to_a_scalar_field_is_rejected(self, importer):
        with pytest.raises(ManifestError, match="u8"):
            importer(
                binds=[f"{IN}:payload=Binary_input"],
                channels={IN: ("t.Scalar", "in")},
                schemas={"t.Scalar": {"fields": [{"name": "payload", "type": "u8"}]}},
            )

    def test_a_binary_variable_without_a_length_field_is_rejected(self, importer):
        with pytest.raises(ManifestError, match="payload_length"):
            importer(
                binds=[f"{IN}:payload=Binary_input"],
                channels={IN: ("t.NoLength", "in")},
                schemas={"t.NoLength": {"fields": [
                    {"name": "payload", "type": "u8", "count": 8}
                ]}},
            )

    def test_a_signed_length_field_is_rejected(self, importer):
        with pytest.raises(ManifestError, match="unsigned"):
            importer(
                binds=[f"{IN}:payload=Binary_input"],
                channels={IN: ("t.Signed", "in")},
                schemas={"t.Signed": {"fields": [
                    {"name": "payload_length", "type": "i16"},
                    {"name": "payload", "type": "u8", "count": 8},
                ]}},
            )

    def test_a_field_no_binding_names_is_rejected(self, importer):
        """Declaring one binding declares them all: a field left over is a
        variable the FMU would never see written or read."""
        with pytest.raises(ManifestError, match="spare"):
            importer(
                binds=[f"{IN}:payload=Binary_input"],
                channels={IN: ("t.Spare", "in")},
                schemas={"t.Spare": {"fields": [
                    {"name": "payload_length", "type": "u16"},
                    {"name": "payload", "type": "u8", "count": 8},
                    {"name": "spare", "type": "u32"},
                ]}},
            )

    def test_a_binding_against_the_channels_direction_is_rejected(self, importer):
        with pytest.raises(ManifestError, match="output"):
            importer(binds=[f"{IN}:payload=Binary_output", *PAYLOAD_BINDINGS[1:]])

    def test_a_bound_above_the_variables_declared_max_size_is_rejected(
        self, importer, tmp_path
    ):
        """A Channel that admits more than the variable does is a mapping
        mistake: every payload above `maxSize` would be refused by the FMU."""
        bounded = with_description(
            tmp_path, "bounded",
            lambda text: text.replace(
                BINARY_INPUT, BINARY_INPUT.replace(">", ' maxSize="16">')
            ),
        )
        with pytest.raises(ManifestError, match="maxSize 16"):
            importer(fmu=bounded)

    def test_a_binding_naming_an_undeclared_channel_is_rejected(self, importer):
        with pytest.raises(ManifestError, match="bin.Nope"):
            importer(binds=["bin.Nope:payload=Binary_input", *PAYLOAD_BINDINGS])

    def test_a_binding_naming_an_undeclared_field_is_rejected(self, importer):
        with pytest.raises(ManifestError, match="payload"):
            importer(binds=[f"{IN}:payload=Binary_input", *PAYLOAD_BINDINGS])

    def test_a_field_bound_twice_is_rejected(self, importer):
        with pytest.raises(ManifestError, match="twice"):
            importer(binds=[*PAYLOAD_BINDINGS, f"{IN}:payload=Binary_input"])

    def test_a_malformed_binding_is_rejected(self, importer):
        with pytest.raises(ManifestError, match="<channel>:<field>=<variable>"):
            importer(binds=["Binary_input"])


FIXTURE_PROFILE = (
    ROOT / "proofs" / "fmi-ls-bus" / "evidence" / "profile.json"
)
CAN_NODE = "DemoCanNodeTriggeredOutput.fmu"
# The node's Binary variables declare `maxSize` 2048, so a Channel that can
# carry any payload it is allowed to produce carries 2048 bytes and a length
# field that can still count them.
CAN_BUFFER_BYTES = 2048
CAN_SCHEMAS = {
    "can.Buffer": {"fields": [
        {"name": "data_length", "type": "u16"},
        {"name": "data", "type": "u8", "count": CAN_BUFFER_BYTES},
    ]}
}
CAN_CHANNELS = {
    "can.Rx": ("can.Buffer", "in"), "can.Tx": ("can.Buffer", "out"),
}
CAN_BINDINGS = [
    "can.Rx:data=CanChannel.Rx_Data",
    "can.Tx:data=CanChannel.Tx_Data",
]


def _variable_element(variable: dict) -> str:
    """One variable of the fixture's profile, as the description declares it."""
    kind = variable["type"]
    attributes = " ".join(
        f"{name}={quoteattr(value)}"
        for name, value in variable.items()
        if name not in ("type", "dimension_start")
    )
    dimension = (
        f'<Dimension start={quoteattr(variable["dimension_start"])}/>'
        if "dimension_start" in variable else ""
    )
    return f"<{kind} {attributes}>{dimension}</{kind}>"


def can_node_fmu(tmp_path) -> Path:
    """The fixture's CAN node as an archive carrying its description alone.

    The archive is rebuilt from `proofs/fmi-ls-bus/evidence/profile.json` —
    the fixture's own record of what it inspected — rather than from a
    restatement of it here, so a fixture that is rebuilt and changes takes
    this with it. It carries no binary, because the FMU itself is built by
    the proof rather than vendored.
    """
    described = json.loads(FIXTURE_PROFILE.read_text())["fmus"][CAN_NODE]
    description = described["modelDescription"]
    variables = "".join(
        _variable_element(variable) for variable in description["variables"]
    )
    path = tmp_path / CAN_NODE
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("modelDescription.xml", (
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<fmiModelDescription '
            f'fmiVersion={quoteattr(description["fmiVersion"])} '
            f'instantiationToken='
            f'{quoteattr(description["instantiationToken"])}>'
            f'<CoSimulation '
            f'modelIdentifier={quoteattr(description["modelIdentifier"])}/>'
            f'<ModelVariables>{variables}</ModelVariables>'
            f'</fmiModelDescription>'
        ))
    return path


class TestAcceptanceFixtureMapping:
    """The mapping, against the fixture's own record of the CAN node.

    The node cannot be *driven* here: it requires Event Mode and its Clocks,
    which #139 implements and `proofs/fmi-ls-bus/run-proof.sh` measures. What
    this slice can be held to is that the Binary mapping resolves against the
    node's real declarations — dotted variable names, one bounded schema
    carried by two Channels, `maxSize` 2048, a `<Dimension start="1"/>`
    input and a structural Boolean parameter.

    Binding happens before the FMU is loaded, so a mapping that resolves gets
    as far as the absent binary, and the diagnostic says which of the two
    stopped the Run.
    """

    def reach_the_fmu(self, tmp_path, **arguments) -> str:
        """Initialize against the node, and answer what stopped it."""
        participant = FmuParticipant(can_node_fmu(tmp_path), **arguments)
        try:
            with pytest.raises(ManifestError) as rejected:
                participant.on_init(init_line(CAN_CHANNELS, CAN_SCHEMAS))
        finally:
            participant.close()
        return str(rejected.value)

    def test_the_nodes_binary_variables_bind_through_one_bounded_schema(
        self, tmp_path
    ):
        """One schema, two Channels, dotted names — the mapping resolves, and
        what is left is the binary the fixture builds rather than ships."""
        assert "carries no binary" in self.reach_the_fmu(
            tmp_path, binds=CAN_BINDINGS
        )

    def test_the_nodes_structural_parameter_is_a_start_value(self, tmp_path):
        assert "carries no binary" in self.reach_the_fmu(
            tmp_path,
            binds=CAN_BINDINGS,
            starts=["org.fmi_standard.fmi_ls_bus.Can_BusNotifications=true"],
        )

    def test_the_nodes_clocks_are_reported_rather_than_skipped(self, tmp_path):
        """The Clock is named as the capability it is, which is what #137
        recorded as this importer's other gap."""
        stopped = self.reach_the_fmu(
            tmp_path,
            binds=["can.Rx:data=CanChannel.Rx_Clock", CAN_BINDINGS[1]],
        )
        assert "Clock variable" in stopped
        assert "CanChannel.Rx_Clock" in stopped

    def test_a_length_field_that_cannot_count_the_payload_is_rejected(
        self, tmp_path
    ):
        """A `u8` length beside 2048 payload bytes describes a Channel whose
        payload can never fill it."""
        narrow = {"can.Buffer": {"fields": [
            {"name": "data_length", "type": "u8"},
            {"name": "data", "type": "u8", "count": CAN_BUFFER_BYTES},
        ]}}
        participant = FmuParticipant(can_node_fmu(tmp_path), binds=CAN_BINDINGS)
        try:
            with pytest.raises(ManifestError, match="counts no further than 255"):
                participant.on_init(init_line(CAN_CHANNELS, narrow))
        finally:
            participant.close()


BOUND_BYTES = 32
RUN_SCHEMAS = {
    PAYLOAD_SCHEMA: {
        "fields": [
            {"name": "payload_length", "type": "u16"},
            {"name": "payload", "type": "u8", "count": BOUND_BYTES},
        ]
    }
}
RUN_DURATION_NS = 400_000_000


def binary_manifest(
    *,
    fmu: Path = FEEDTHROUGH,
    schemas: dict = RUN_SCHEMAS,
    binds: list[str] = PAYLOAD_BINDINGS,
    capacity: int = BOUND_BYTES,
    declared_extra: int = 0,
    duration_ns: int = RUN_DURATION_NS,
) -> Manifest:
    """A binary stimulus feeding the imported FMU, which publishes it back."""
    m = Manifest(duration_ns=duration_ns)
    m.add_schemas(schemas)
    m.add_channel(IN, schema=PAYLOAD_SCHEMA)
    m.add_channel(OUT, schema=PAYLOAD_SCHEMA)
    m.add_process(
        "stimulus",
        command=[
            sys.executable, str(STIMULUS),
            IN, "payload", str(capacity), str(declared_extra),
        ],
        step_period_ns=STEP_PERIOD_NS,
        publishes=[IN],
    )
    # The bindings travel as command arguments, which the Manifest already
    # hashes — so the whole mapping is inside the hashed Manifest.
    m.add_process(
        "importer",
        command=[sys.executable, "-m", "sil.fmi", str(fmu), *_bind_arguments(binds)],
        step_period_ns=STEP_PERIOD_NS,
        subscribes=[SubscriberRoute(IN, capacity=2)],
        publishes=[OUT],
        priority=1,
    )
    return m


def _bind_arguments(binds: list[str]) -> list[str]:
    return [argument for bind in binds for argument in ("--bind", bind)]


@pytest.fixture(scope="module")
def binary_result(sil_run, tmp_path_factory):
    """One Run of the binary round-trip, shared by every assertion on it."""
    return run_simulation(
        binary_manifest(),
        runner=sil_run,
        workdir=tmp_path_factory.mktemp("fmi-binary"),
    )


class TestRunBoundary:
    """The importer as a process participant in a complete Run."""

    def test_the_stimulus_reaches_the_recording_through_the_fmu(
        self, binary_result
    ):
        """The Channels take the default Latency, so a Message published at
        `t` is visible to the importer at its next activation: what it
        publishes is the payload the stimulus published one Step earlier."""
        published = binary_result.messages(OUT)
        assert len(published) == RUN_DURATION_NS // STEP_PERIOD_NS
        assert [
            (fields["payload_length"], fields["payload"])
            for _, fields in published[1:]
        ] == [
            (len(payload(step, BOUND_BYTES)),
             payload(step, BOUND_BYTES).ljust(BOUND_BYTES, b"\x00"))
            for step in range(len(published) - 1)
        ]

    def test_the_run_carries_an_empty_and_a_full_payload(self, binary_result):
        """The assertion above is only worth making over the whole range."""
        lengths = {
            fields["payload_length"] for _, fields in binary_result.messages(OUT)
        }
        assert 0 in lengths
        assert BOUND_BYTES in lengths

    def test_every_payload_carries_embedded_zeros(self, binary_result):
        """A zero byte inside the payload is what a C-string mapping loses."""
        # The first Message is the FMU's own start value, which no stimulus
        # produced: the Channel's Latency holds the first payload back a Step.
        assert all(
            b"\x00" in fields["payload"][:fields["payload_length"]]
            for _, fields in binary_result.messages(OUT)[1:]
            if fields["payload_length"] > 0
        )

    def test_the_determinism_check_passes_for_a_binary_run(self, sil_run, tmp_path):
        """Two Runs of one Manifest, bit-compared."""
        ref = binary_manifest().write(tmp_path / "binary.json")
        proc = subprocess.run(
            [sys.executable, "-m", "sil.check", str(ref.path),
             "--runner", str(sil_run)],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.startswith("deterministic: ")


SCALAR_IN = "s.In"
SCALAR_OUT = "s.Out"
SCALAR_STIMULUS = ROOT / "tests" / "participants" / "scalar_stimulus.py"

# The start value the Run declares for a variable no Channel writes, so the
# Recording is where it becomes visible.
HELD_START = 3.5


def scalar_manifest() -> Manifest:
    """Declared scalar bindings and one start value, as a complete Run."""
    m = Manifest(duration_ns=RUN_DURATION_NS)
    m.add_schemas(SCALAR_SCHEMAS | {"fmu.Held": {
        "fields": [{"name": "held", "type": "f64"}]
    }})
    m.add_channel(SCALAR_IN, schema="fmu.ScalarIn")
    m.add_channel(SCALAR_OUT, schema="fmu.ScalarOut")
    m.add_channel("s.Held", schema="fmu.Held")
    m.add_process(
        "stimulus",
        command=[sys.executable, str(SCALAR_STIMULUS), SCALAR_IN],
        step_period_ns=STEP_PERIOD_NS,
        publishes=[SCALAR_IN],
    )
    m.add_process(
        "importer",
        command=[
            sys.executable, "-m", "sil.fmi", str(FEEDTHROUGH),
            *_bind_arguments(SCALAR_BINDINGS),
            *_bind_arguments(["s.Held:held=Float64_continuous_output"]),
            "--start", f"Float64_continuous_input={HELD_START}",
        ],
        step_period_ns=STEP_PERIOD_NS,
        subscribes=[SubscriberRoute(SCALAR_IN, capacity=2)],
        publishes=[SCALAR_OUT, "s.Held"],
        priority=1,
    )
    return m


@pytest.fixture(scope="module")
def scalar_result(sil_run, tmp_path_factory):
    """One Run of the scalar bindings, shared by every assertion on it."""
    return run_simulation(
        scalar_manifest(),
        runner=sil_run,
        workdir=tmp_path_factory.mktemp("fmi-scalars"),
    )


class TestScalarRunBoundary:
    """Declared scalar bindings and start values, in a complete Run.

    The in-process assertions above reach the importer directly; a behavior
    change needs the Recording too (CONTRIBUTING.md).
    """

    def test_the_stimulus_reaches_the_recording_through_the_fmu(
        self, scalar_result
    ):
        published = scalar_result.messages(SCALAR_OUT)
        stimulus = scalar_result.messages(SCALAR_IN)
        assert len(published) == RUN_DURATION_NS // STEP_PERIOD_NS
        assert [
            (fields["value"], fields["flag"]) for _, fields in published[1:]
        ] == [
            (fields["value"], fields["flag"]) for _, fields in stimulus[:-1]
        ]

    def test_the_boolean_alternates_rather_than_holding_one_value(
        self, scalar_result
    ):
        """A mapping that wrote nothing would publish one value throughout."""
        flags = [fields["flag"] for _, fields in scalar_result.messages(SCALAR_OUT)]
        assert set(flags) == {0, 1}

    def test_the_start_value_holds_for_a_variable_no_channel_writes(
        self, scalar_result
    ):
        """`Float64_continuous_input` is bound by no Channel, so what the
        Recording shows for its output is the `--start` and nothing else."""
        held = {fields["held"] for _, fields in scalar_result.messages("s.Held")}
        assert held == {HELD_START}

    def test_the_determinism_check_passes_for_a_scalar_run(self, sil_run, tmp_path):
        ref = scalar_manifest().write(tmp_path / "scalars.json")
        proc = subprocess.run(
            [sys.executable, "-m", "sil.check", str(ref.path),
             "--runner", str(sil_run)],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.startswith("deterministic: ")


class TestRunBoundaryFailures:
    """The exit-code taxonomy: a rejected mapping is not a failed Run."""

    def test_an_invalid_binding_is_a_manifest_error(self, run_sil, tmp_path):
        proc = run_sil(
            binary_manifest(
                binds=[f"{IN}:payload=Binary_typo", f"{OUT}:payload=Binary_output"]
            ).write(tmp_path / "invalid-binding.json").path
        )
        assert proc.returncode == 2, proc.stderr
        assert "Binary_typo" in proc.stderr

    def test_a_payload_the_fmu_refuses_is_a_run_failure(self, run_sil, tmp_path):
        """The Channel admits more than `Feedthrough` copies, so the FMU is
        what refuses the payload — after the Run has started."""
        capacity = FMU_BINARY_BYTES * 2
        schemas = {
            PAYLOAD_SCHEMA: {"fields": [
                {"name": "payload_length", "type": "u16"},
                {"name": "payload", "type": "u8", "count": capacity},
            ]}
        }
        # The stimulus grows its payload by one byte per Step, so the Run has
        # to be long enough to reach the length the FMU refuses.
        proc = run_sil(
            binary_manifest(
                schemas=schemas, capacity=capacity,
                duration_ns=STEP_PERIOD_NS * (FMU_BINARY_BYTES + 4),
            ).write(tmp_path / "overflow.json").path
        )
        assert proc.returncode == 1, proc.stderr
        assert "fmi3SetBinary returned Error" in proc.stderr

    def test_a_length_above_the_bound_is_a_run_failure(self, run_sil, tmp_path):
        """A publisher that declares more than its Channel carries is refused
        by the importer itself, before the FMU is asked for anything."""
        manifest = binary_manifest(declared_extra=1)
        proc = run_sil(manifest.write(tmp_path / "too-long.json").path)
        assert proc.returncode == 1, proc.stderr
        assert "payload_length" in proc.stderr


class TestImporterCommand:
    """The importer's own command line, since a Manifest hashes it verbatim."""

    def test_the_bindings_are_part_of_the_command(self):
        command = binary_manifest().to_doc()["participants"]["importer"]["command"]
        assert command[-4:] == [
            "--bind", PAYLOAD_BINDINGS[0], "--bind", PAYLOAD_BINDINGS[1]
        ]

    def test_an_unreadable_binding_is_reported_as_a_manifest_error(
        self, tmp_path
    ):
        """Driven directly: a `fail` line before `ready` is what makes the
        kernel call this a Manifest error rather than a Run failure."""
        importer = subprocess.Popen(
            [sys.executable, "-m", "sil.fmi", str(FEEDTHROUGH),
             "--bind", "nonsense"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=tmp_path,
        )
        try:
            importer.stdin.write(
                json.dumps(init_line(PAYLOAD_CHANNELS, PAYLOAD_SCHEMAS)) + "\n"
            )
            importer.stdin.flush()
            answer = json.loads(importer.stdout.readline())
        finally:
            importer.stdin.close()
            importer.wait(timeout=10)
        assert answer["op"] == "fail"
        assert "failure" not in answer
        assert "<channel>:<field>=<variable>" in answer["reason"]
