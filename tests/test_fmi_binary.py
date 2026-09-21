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

import pytest
from conftest import ROOT
from sil.fmi import FmuParticipant
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

IN = "can.In"
OUT = "can.Out"
FRAME = "can.Frame"

# The bounded representation: the length actually used, then the payload bytes
# the Channel admits. One schema, carried by both Channels — which is why a
# binding names the Channel as well as the field.
FRAME_SCHEMAS = {
    FRAME: {
        "fields": [
            {"name": "frame_length", "type": "u16"},
            {"name": "frame", "type": "u8", "count": FMU_BINARY_BYTES},
        ]
    }
}

FRAME_BINDINGS = [
    f"{IN}:frame=Binary_input",
    f"{OUT}:frame=Binary_output",
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


FRAME_CHANNELS = {IN: (FRAME, "in"), OUT: (FRAME, "out")}


@pytest.fixture
def importer():
    """Build an initialized importer, torn down after the test."""
    built = []

    def _build(*, binds=FRAME_BINDINGS, starts=(),
               channels=FRAME_CHANNELS, schemas=FRAME_SCHEMAS,
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


def frame(payload_bytes: bytes, capacity: int = FMU_BINARY_BYTES) -> dict:
    """One Message carrying `payload_bytes` on a Channel of that bound."""
    return {
        "frame_length": len(payload_bytes),
        "frame": payload_bytes.ljust(capacity, b"\x00"),
    }


def round_trip(participant, payload_bytes: bytes, capacity=FMU_BINARY_BYTES,
               step: int = 0):
    """Publish one payload into the FMU and read what it hands back.

    `step` is the step index, because an FMU is stepped forward: a second
    round trip on one instance is the next communication point.
    """
    (channel, fields), = participant.on_step(
        step * STEP_PERIOD_NS, STEP_PERIOD_NS,
        [Input(IN, step * STEP_PERIOD_NS, frame(payload_bytes, capacity))],
    )
    assert channel == OUT
    return fields


class TestBoundedPayloads:
    """A variable-length Binary value carried by a fixed-layout Message."""

    def test_arbitrary_bytes_survive_the_round_trip(self, importer):
        """Embedded zeros included: the length says what the payload is."""
        payload_bytes = b"\x00\xff\x00\x10\x00\x00\x7f"
        fields = round_trip(importer(), payload_bytes)
        assert fields["frame_length"] == len(payload_bytes)
        assert fields["frame"][:len(payload_bytes)] == payload_bytes

    def test_an_empty_payload_survives_the_round_trip(self, importer):
        fields = round_trip(importer(), b"")
        assert fields["frame_length"] == 0
        assert fields["frame"] == bytes(FMU_BINARY_BYTES)

    def test_a_payload_filling_the_bound_survives_the_round_trip(self, importer):
        payload_bytes = bytes((index * 7) % 256 for index in range(FMU_BINARY_BYTES))
        fields = round_trip(importer(), payload_bytes)
        assert fields["frame_length"] == FMU_BINARY_BYTES
        assert fields["frame"] == payload_bytes

    def test_the_bytes_beyond_the_payload_are_zero(self, importer):
        """Unused bytes are deterministic, not whatever the buffer held.

        The same payload published after a longer one must produce the same
        Message, or the Recording would carry the previous payload's tail.
        """
        participant = importer()
        round_trip(participant, bytes(FMU_BINARY_BYTES), step=0)
        round_trip(participant, b"\xaa" * 64, step=1)
        fields = round_trip(participant, b"\x01\x02", step=2)
        assert fields["frame"] == b"\x01\x02" + bytes(FMU_BINARY_BYTES - 2)

    def test_the_length_a_message_declares_bounds_what_is_written(self, importer):
        """The payload field is always full-width; the length is the payload."""
        message = frame(b"\x01\x02\x03")
        message["frame"] = b"\x01\x02\x03" + b"\xff" * (FMU_BINARY_BYTES - 3)
        (_, fields), = importer().on_step(
            0, STEP_PERIOD_NS, [Input(IN, 0, message)]
        )
        assert fields["frame_length"] == 3
        assert fields["frame"] == b"\x01\x02\x03" + bytes(FMU_BINARY_BYTES - 3)


class TestPayloadsThatDoNotFit:
    """An oversize payload aborts the Run; nothing is truncated to fit."""

    def test_a_length_above_the_channel_bound_is_refused(self, importer):
        message = frame(b"\x01" * 8)
        message["frame_length"] = FMU_BINARY_BYTES + 1
        with pytest.raises(ParticipantFailure, match="frame_length"):
            importer().on_step(0, STEP_PERIOD_NS, [Input(IN, 0, message)])

    def test_a_payload_above_the_fmus_own_bound_is_refused_by_the_fmu(
        self, importer
    ):
        """A Channel may admit more than the FMU does; the FMU then says so."""
        capacity = FMU_BINARY_BYTES * 2
        schemas = {
            FRAME: {"fields": [
                {"name": "frame_length", "type": "u16"},
                {"name": "frame", "type": "u8", "count": capacity},
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
            FRAME: {"fields": [
                {"name": "frame_length", "type": "u16"},
                {"name": "frame", "type": "u8", "count": FMU_BINARY_BYTES},
            ]},
            "can.Small": {"fields": [
                {"name": "frame_length", "type": "u16"},
                {"name": "frame", "type": "u8", "count": 8},
            ]},
        }
        participant = importer(
            schemas=schemas,
            channels={IN: (FRAME, "in"), OUT: ("can.Small", "out")},
        )
        with pytest.raises(ParticipantFailure, match="produced 16 bytes"):
            participant.on_step(
                0, STEP_PERIOD_NS, [Input(IN, 0, frame(b"\x01" * 16))]
            )


SCALARS = "fmu.Scalars"
SCALAR_SCHEMAS = {
    "fmu.ScalarIn": {"fields": [
        {"name": "f32", "type": "f32"},
        {"name": "i8", "type": "i8"},
        {"name": "u16", "type": "u16"},
        {"name": "i64", "type": "i64"},
        {"name": "u64", "type": "u64"},
        {"name": "flag", "type": "u8"},
    ]},
    "fmu.ScalarOut": {"fields": [
        {"name": "f32", "type": "f32"},
        {"name": "i8", "type": "i8"},
        {"name": "u16", "type": "u16"},
        {"name": "i64", "type": "i64"},
        {"name": "u64", "type": "u64"},
        {"name": "flag", "type": "u8"},
    ]},
}
SCALAR_CHANNELS = {"s.In": ("fmu.ScalarIn", "in"), "s.Out": ("fmu.ScalarOut", "out")}
SCALAR_BINDINGS = [
    "s.In:f32=Float32_continuous_input", "s.Out:f32=Float32_continuous_output",
    "s.In:i8=Int8_input", "s.Out:i8=Int8_output",
    "s.In:u16=UInt16_input", "s.Out:u16=UInt16_output",
    "s.In:i64=Int64_input", "s.Out:i64=Int64_output",
    "s.In:u64=UInt64_input", "s.Out:u64=UInt64_output",
    "s.In:flag=Boolean_input", "s.Out:flag=Boolean_output",
]


class TestScalarTypes:
    """Every FMI scalar type the importer maps, over the same FMU."""

    def test_each_scalar_type_survives_the_round_trip(self, importer):
        participant = importer(
            binds=SCALAR_BINDINGS,
            channels=SCALAR_CHANNELS,
            schemas=SCALAR_SCHEMAS,
        )
        written = {
            "f32": 0.5, "i8": -128, "u16": 65535,
            "i64": -(2 ** 62), "u64": 2 ** 63 + 1, "flag": 1,
        }
        (_, published), = participant.on_step(
            0, STEP_PERIOD_NS, [Input("s.In", 0, written)]
        )
        assert published == written


class TestStartValues:
    """Initialization values, set before the FMU leaves initialization mode."""

    def test_a_start_value_is_what_an_unfed_step_sees(self, importer):
        participant = importer(
            binds=[f"{OUT}:frame=Binary_output"],
            starts=["Binary_input=00ff00"],
            channels={OUT: (FRAME, "out")},
        )
        (_, fields), = participant.on_step(0, STEP_PERIOD_NS, [])
        assert fields["frame_length"] == 3
        assert fields["frame"][:3] == b"\x00\xff\x00"

    def test_a_scalar_start_value_is_what_an_unfed_step_sees(self, importer):
        participant = importer(
            binds=["s.Out:i64=Int64_output", "s.Out:flag=Boolean_output"],
            starts=["Int64_input=-7", "Boolean_input=true"],
            channels={"s.Out": ("fmu.ScalarOut2", "out")},
            schemas={"fmu.ScalarOut2": {"fields": [
                {"name": "i64", "type": "i64"},
                {"name": "flag", "type": "u8"},
            ]}},
        )
        (_, fields), = participant.on_step(0, STEP_PERIOD_NS, [])
        assert fields == {"i64": -7, "flag": 1}

    def test_the_fmus_own_start_value_stands_when_none_is_given(self, importer):
        """`Feedthrough` starts its Binary variables at `foo`."""
        participant = importer(
            binds=[f"{OUT}:frame=Binary_output"], channels={OUT: (FRAME, "out")}
        )
        (_, fields), = participant.on_step(0, STEP_PERIOD_NS, [])
        assert fields["frame"][:fields["frame_length"]] == b"foo"

    @pytest.mark.parametrize(
        ("start", "expected"),
        [
            ("Nope=1", "does not declare"),
            ("Int8_input=300", "out of range"),
            ("Float64_continuous_input=nan-ish", "Float64"),
            ("Boolean_input=yes", "'true'"),
            ("Binary_input=zz", "hexadecimal"),
            ("String_input=x", "String variable"),
            ("Int8_input", "'<variable>=<value>'"),
        ],
    )
    def test_a_start_value_that_cannot_be_honoured_is_rejected(
        self, importer, start, expected
    ):
        with pytest.raises(ManifestError, match=expected):
            importer(binds=[f"{OUT}:frame=Binary_output"], starts=[start],
                     channels={OUT: (FRAME, "out")})


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
INT32_INPUT = (
    '<Int32 name="Int32_input" valueReference="19" causality="input" start="0"/>'
)


class TestBindingsRejectedBeforeStepping:
    """Every binding the importer cannot honour, named before the FMU is stepped.

    Each is a Manifest error: the initialization line and the command's
    bindings describe a mapping that does not exist, which is a fact about the
    Run's configuration rather than about its behavior.
    """

    def test_a_binding_naming_no_fmu_variable_is_rejected(self, importer):
        with pytest.raises(ManifestError, match="Binary_missing"):
            importer(binds=[f"{IN}:frame=Binary_missing", *FRAME_BINDINGS[1:]])

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

    def test_a_binding_naming_a_variable_with_dimensions_is_rejected(
        self, importer, tmp_path
    ):
        """An array variable is not a scalar and is not a bounded Binary."""
        array_fmu = with_description(
            tmp_path, "array",
            lambda text: text.replace(
                INT32_INPUT,
                INT32_INPUT.replace("/>", '><Dimension start="2"/></Int32>'),
            ),
        )
        with pytest.raises(ManifestError, match="dimension"):
            importer(
                fmu=array_fmu,
                binds=["t.In:value=Int32_input"],
                channels={"t.In": ("t.Value", "in")},
                schemas={"t.Value": {"fields": [{"name": "value", "type": "i32"}]}},
            )

    def test_a_binding_whose_field_type_is_not_the_variables_is_rejected(
        self, importer
    ):
        with pytest.raises(ManifestError, match="i32"):
            importer(
                binds=["t.In:value=Int32_input"],
                channels={"t.In": ("t.Value", "in")},
                schemas={"t.Value": {"fields": [{"name": "value", "type": "f64"}]}},
            )

    def test_a_binary_variable_bound_to_a_scalar_field_is_rejected(self, importer):
        with pytest.raises(ManifestError, match="u8"):
            importer(
                binds=[f"{IN}:frame=Binary_input"],
                channels={IN: ("t.Scalar", "in")},
                schemas={"t.Scalar": {"fields": [{"name": "frame", "type": "u8"}]}},
            )

    def test_a_binary_variable_without_a_length_field_is_rejected(self, importer):
        with pytest.raises(ManifestError, match="frame_length"):
            importer(
                binds=[f"{IN}:frame=Binary_input"],
                channels={IN: ("t.NoLength", "in")},
                schemas={"t.NoLength": {"fields": [
                    {"name": "frame", "type": "u8", "count": 8}
                ]}},
            )

    def test_a_signed_length_field_is_rejected(self, importer):
        with pytest.raises(ManifestError, match="unsigned"):
            importer(
                binds=[f"{IN}:frame=Binary_input"],
                channels={IN: ("t.Signed", "in")},
                schemas={"t.Signed": {"fields": [
                    {"name": "frame_length", "type": "i16"},
                    {"name": "frame", "type": "u8", "count": 8},
                ]}},
            )

    def test_a_field_no_binding_names_is_rejected(self, importer):
        """Declaring one binding declares them all: a field left over is a
        variable the FMU would never see written or read."""
        with pytest.raises(ManifestError, match="spare"):
            importer(
                binds=[f"{IN}:frame=Binary_input"],
                channels={IN: ("t.Spare", "in")},
                schemas={"t.Spare": {"fields": [
                    {"name": "frame_length", "type": "u16"},
                    {"name": "frame", "type": "u8", "count": 8},
                    {"name": "spare", "type": "u32"},
                ]}},
            )

    def test_a_binding_against_the_channels_direction_is_rejected(self, importer):
        with pytest.raises(ManifestError, match="output"):
            importer(binds=[f"{IN}:frame=Binary_output", *FRAME_BINDINGS[1:]])

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
        with pytest.raises(ManifestError, match="can.Nope"):
            importer(binds=["can.Nope:frame=Binary_input", *FRAME_BINDINGS])

    def test_a_binding_naming_an_undeclared_field_is_rejected(self, importer):
        with pytest.raises(ManifestError, match="payload"):
            importer(binds=[f"{IN}:payload=Binary_input", *FRAME_BINDINGS])

    def test_a_field_bound_twice_is_rejected(self, importer):
        with pytest.raises(ManifestError, match="twice"):
            importer(binds=[*FRAME_BINDINGS, f"{IN}:frame=Binary_input"])

    def test_a_malformed_binding_is_rejected(self, importer):
        with pytest.raises(ManifestError, match="<channel>:<field>=<variable>"):
            importer(binds=["Binary_input"])


BOUND_BYTES = 32
RUN_SCHEMAS = {
    FRAME: {
        "fields": [
            {"name": "frame_length", "type": "u16"},
            {"name": "frame", "type": "u8", "count": BOUND_BYTES},
        ]
    }
}
RUN_DURATION_NS = 400_000_000


def binary_manifest(
    *,
    fmu: Path = FEEDTHROUGH,
    schemas: dict = RUN_SCHEMAS,
    binds: list[str] = FRAME_BINDINGS,
    capacity: int = BOUND_BYTES,
    declared_extra: int = 0,
    duration_ns: int = RUN_DURATION_NS,
) -> Manifest:
    """A binary stimulus feeding the imported FMU, which publishes it back."""
    m = Manifest(duration_ns=duration_ns)
    m.add_schemas(schemas)
    m.add_channel(IN, schema=FRAME)
    m.add_channel(OUT, schema=FRAME)
    m.add_process(
        "stimulus",
        command=[
            sys.executable, str(STIMULUS),
            IN, "frame", str(capacity), str(declared_extra),
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
            (fields["frame_length"], fields["frame"])
            for _, fields in published[1:]
        ] == [
            (len(payload(step, BOUND_BYTES)),
             payload(step, BOUND_BYTES).ljust(BOUND_BYTES, b"\x00"))
            for step in range(len(published) - 1)
        ]

    def test_the_run_carries_an_empty_and_a_full_payload(self, binary_result):
        """The assertion above is only worth making over the whole range."""
        lengths = {
            fields["frame_length"] for _, fields in binary_result.messages(OUT)
        }
        assert 0 in lengths
        assert BOUND_BYTES in lengths

    def test_every_payload_carries_embedded_zeros(self, binary_result):
        """A zero byte inside the payload is what a C-string mapping loses."""
        # The first Message is the FMU's own start value, which no stimulus
        # produced: the Channel's Latency holds the first payload back a Step.
        assert all(
            b"\x00" in fields["frame"][:fields["frame_length"]]
            for _, fields in binary_result.messages(OUT)[1:]
            if fields["frame_length"] > 0
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


class TestRunBoundaryFailures:
    """The exit-code taxonomy: a rejected mapping is not a failed Run."""

    def test_an_invalid_binding_is_a_manifest_error(self, run_sil, tmp_path):
        proc = run_sil(
            binary_manifest(
                binds=[f"{IN}:frame=Binary_typo", f"{OUT}:frame=Binary_output"]
            ).write(tmp_path / "invalid-binding.json").path
        )
        assert proc.returncode == 2, proc.stderr
        assert "Binary_typo" in proc.stderr

    def test_a_payload_the_fmu_refuses_is_a_run_failure(self, run_sil, tmp_path):
        """The Channel admits more than `Feedthrough` copies, so the FMU is
        what refuses the payload — after the Run has started."""
        capacity = FMU_BINARY_BYTES * 2
        schemas = {
            FRAME: {"fields": [
                {"name": "frame_length", "type": "u16"},
                {"name": "frame", "type": "u8", "count": capacity},
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
        assert "frame_length" in proc.stderr


class TestImporterCommand:
    """The importer's own command line, since a Manifest hashes it verbatim."""

    def test_the_bindings_are_part_of_the_command(self):
        command = binary_manifest().to_doc()["participants"]["importer"]["command"]
        assert command[-4:] == [
            "--bind", FRAME_BINDINGS[0], "--bind", FRAME_BINDINGS[1]
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
                json.dumps(init_line(FRAME_CHANNELS, FRAME_SCHEMAS)) + "\n"
            )
            importer.stdin.flush()
            answer = json.loads(importer.stdout.readline())
        finally:
            importer.stdin.close()
            importer.wait(timeout=10)
        assert answer["op"] == "fail"
        assert "failure" not in answer
        assert "<channel>:<field>=<variable>" in answer["reason"]
