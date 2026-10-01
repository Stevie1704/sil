"""Float32, Int32, UInt32 and UInt64 FMI scalars, end to end (issue #189).

The four types are the numeric profile of the C reference product: Float32 for
sensor and control values, Int32 for signed selected IDs, UInt32 for counts,
modes and sequence numbers, and UInt64 for Sample times. The conformance
target is the Modelica Association's `Feedthrough`, which copies each input
to the output of the same type, so a value that comes back changed was changed
by the importer.

The conversion behavior is specified here, independently of the code under
test:

* a Channel field carries exactly one FMI type: Float32 is `f32`, Int32 is
  `i32`, UInt32 is `u32` and UInt64 is `u64`. Any other width, signedness or
  kind is refused, so no integer is routed through a floating-point field;
* a start value of an integer type is a plain decimal integer — an optional
  `-` and ASCII digits, nothing else — inside the type's range. An unsigned
  type takes no sign at all;
* a Float32 start value is a finite decimal, read as the nearest binary64
  and then rounded to the nearest Float32 (ties to even) — the two steps
  `sil-csv` takes for an `f32` cell. A value that rounds to infinity, or a
  non-zero value that rounds to zero, is refused.
"""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from fractions import Fraction
from pathlib import Path

import pytest
from conftest import ROOT
from sil.fmi import CoSimulation, FmuParticipant
from sil.csv_recording import convert
from sil.fmi.authoring import author
from sil.fmi.coupling import AuthoringError, couple
from sil.fmi.description import Variable
from sil.fmi.inspection import inspect
from sil.fmi.mapping import start_value
from sil.manifest import Manifest, SubscriberRoute
from sil.participant import Input, ManifestError, ParticipantFailure
from sil.recording import read_records
from sil.testing import run_simulation

FEEDTHROUGH = (
    ROOT / "tests" / "fixtures" / "reference-fmus" / "3.0" / "Feedthrough.fmu"
)
STIMULUS = ROOT / "tests" / "participants" / "numeric_stimulus.py"
STEP_PERIOD_NS = 10_000_000

# The binary32 constants, stated from the IEEE 754 format rather than
# computed by any conversion the importer uses.
F32_MAX = (2 - 2**-23) * 2**127
F32_TRUE_MIN = 2**-149
# 0.1 rounded to the nearest binary32: 13421773 / 2^27.
F32_TENTH = float(Fraction(13421773, 2**27))

IN, OUT = "n.In", "n.Out"
FIELDS = [
    {"name": "range_m", "type": "f32"},
    {"name": "object_id", "type": "i32"},
    {"name": "object_count", "type": "u32"},
    {"name": "sample_ns", "type": "u64"},
]
SCHEMAS = {"n.Sensor": {"fields": FIELDS}}
CHANNELS = {IN: ("n.Sensor", "in"), OUT: ("n.Sensor", "out")}
VARIABLES = {
    "range_m": "Float32_discrete",
    "object_id": "Int32",
    "object_count": "UInt32",
    "sample_ns": "UInt64",
}
BINDINGS = [
    f"{channel}:{field}={variable}_{suffix}"
    for channel, suffix in ((IN, "input"), (OUT, "output"))
    for field, variable in VARIABLES.items()
]


def init_line(channels: dict, schemas: dict) -> dict:
    return {
        "op": "init", "name": "fmu", "schemas": schemas,
        "channels": {
            channel: {"schema": schema, "direction": direction}
            for channel, (schema, direction) in channels.items()
        },
    }


@pytest.fixture
def importer():
    """Build an initialized importer over `Feedthrough`, torn down after."""
    built = []

    def _build(*, binds=BINDINGS, starts=(), channels=CHANNELS,
               schemas=SCHEMAS):
        participant = FmuParticipant(
            FEEDTHROUGH, binds=list(binds), starts=list(starts)
        )
        built.append(participant)
        participant.on_init(init_line(channels, schemas))
        return participant

    yield _build
    for participant in built:
        try:
            participant.close()
        except ParticipantFailure:
            pass


def one_field(field_type: str, variable: str, direction: str = "in"):
    """A Channel of one field of `field_type`, bound to `variable`."""
    return {
        "binds": [f"t.C:value={variable}"],
        "channels": {"t.C": ("t.Value", direction)},
        "schemas": {"t.Value": {"fields": [
            {"name": "value", "type": field_type}
        ]}},
    }


def with_description(tmp_path, name: str, rewrite) -> Path:
    """A copy of `Feedthrough` whose description has been rewritten."""
    path = tmp_path / f"{name}.fmu"
    with zipfile.ZipFile(FEEDTHROUGH) as source, \
            zipfile.ZipFile(path, "w") as target:
        for member in source.infolist():
            data = source.read(member.filename)
            if member.filename == "modelDescription.xml":
                data = rewrite(data.decode()).encode()
            target.writestr(member, data)
    return path


def record_calls(monkeypatch) -> list[str]:
    calls: list[str] = []
    called = CoSimulation._call

    def record(self, name, *arguments):
        calls.append(name)
        return called(self, name, *arguments)

    monkeypatch.setattr(CoSimulation, "_call", record)
    return calls


def failing_call(monkeypatch, failing: str) -> None:
    """Have the FMU answer Error to one entry point, as an FMU may."""
    called = CoSimulation._call

    def answer(self, name, *arguments):
        if name == failing:
            raise ParticipantFailure(f"{name} returned Error")
        return called(self, name, *arguments)

    monkeypatch.setattr(CoSimulation, "_call", answer)


# Start values -------------------------------------------------------------------


def variable(kind: str) -> Variable:
    return Variable(name=f"{kind}_input", reference=1, kind=kind,
                    causality="input", max_size=None, value_count=1)


class TestIntegerStartValues:

    @pytest.mark.parametrize(("kind", "text", "value"), [
        ("Int32", "-2147483648", -(2**31)),
        ("Int32", "2147483647", 2**31 - 1),
        ("Int32", "-0", 0),
        ("Int32", "007", 7),
        ("UInt32", "0", 0),
        ("UInt32", "4294967295", 2**32 - 1),
        ("UInt64", "18446744073709551615", 2**64 - 1),
        # Above 2^53 a double cannot tell these apart.
        ("UInt64", "9007199254740993", 2**53 + 1),
        ("UInt64", "9007199254740992", 2**53),
    ])
    def test_a_value_in_range_is_read_exactly(self, kind, text, value):
        read = start_value(variable(kind), text)
        assert read == value and type(read) is int

    @pytest.mark.parametrize(("kind", "text"), [
        ("Int32", "2147483648"),
        ("Int32", "-2147483649"),
        ("UInt32", "4294967296"),
        ("UInt64", "18446744073709551616"),
    ])
    def test_a_value_out_of_range_is_refused(self, kind, text):
        with pytest.raises(ManifestError, match=f"outside the {kind} range"):
            start_value(variable(kind), text)

    @pytest.mark.parametrize("kind", ["UInt32", "UInt64"])
    @pytest.mark.parametrize("text", ["-1", "-0", "-18446744073709551615"])
    def test_a_negative_value_is_refused_for_an_unsigned_type(self, kind, text):
        with pytest.raises(ManifestError, match="unsigned"):
            start_value(variable(kind), text)

    @pytest.mark.parametrize("kind", ["Int32", "UInt32", "UInt64"])
    @pytest.mark.parametrize("text", [
        "1.0", "1e3", "+1", " 1", "1 ", "1_000", "0x10", "", "true", "false",
        "١",  # an Arabic-Indic digit, which Python's int() accepts
    ])
    def test_anything_but_a_plain_decimal_integer_is_refused(self, kind, text):
        with pytest.raises(ManifestError, match="decimal integer"):
            start_value(variable(kind), text)


class TestFloat32StartValues:

    @pytest.mark.parametrize(("text", "value"), [
        ("0.1", F32_TENTH),
        ("1.5", 1.5),
        ("-2.25", -2.25),
        ("3.4028234663852886e38", F32_MAX),
        # Below the halfway point to the next binade, so it rounds down.
        ("3.4028235e38", F32_MAX),
        ("1e-45", F32_TRUE_MIN),
        ("0", 0.0),
    ])
    def test_a_finite_value_is_rounded_to_the_nearest_float32(
        self, text, value
    ):
        assert start_value(variable("Float32"), text) == value

    @pytest.mark.parametrize("text", ["3.5e38", "-3.5e38", "1e39"])
    def test_a_value_that_rounds_to_infinity_is_refused(self, text):
        with pytest.raises(ManifestError, match="outside the Float32 range"):
            start_value(variable("Float32"), text)

    def test_a_non_zero_value_that_rounds_to_zero_is_refused(self):
        with pytest.raises(ManifestError, match="underflows to zero"):
            start_value(variable("Float32"), "1e-46")

    @pytest.mark.parametrize("text", ["inf", "-inf", "nan", "infinity"])
    def test_a_non_finite_value_is_refused(self, text):
        with pytest.raises(ManifestError, match="not a finite number"):
            start_value(variable("Float32"), text)

    @pytest.mark.parametrize("text", ["true", "1,5", "", "0x1p3"])
    def test_a_value_that_is_no_decimal_number_is_refused(self, text):
        with pytest.raises(ManifestError, match="Float32"):
            start_value(variable("Float32"), text)


class TestBooleanAmbiguity:
    """A Boolean is not an integer, in either direction."""

    @pytest.mark.parametrize("text", ["1", "0"])
    def test_an_integer_is_no_boolean_start_value(self, text):
        with pytest.raises(ManifestError, match="'true' or 'false'"):
            start_value(variable("Boolean"), text)

    def test_a_boolean_variable_is_not_carried_by_an_integer_field(
        self, importer
    ):
        with pytest.raises(ManifestError, match="'u8' scalar"):
            importer(**one_field("i32", "Boolean_input"))

    def test_an_integer_variable_is_not_carried_by_a_boolean_byte(
        self, importer
    ):
        with pytest.raises(ManifestError, match="'i32' scalar"):
            importer(**one_field("u8", "Int32_input"))


# Bindings ------------------------------------------------------------------------


class TestRoundTrip:
    """What a Step writes into the FMU is what the FMU hands back."""

    @pytest.mark.parametrize("written", [
        {"range_m": 1.5, "object_id": -1, "object_count": 7, "sample_ns": 0},
        {"range_m": F32_MAX, "object_id": -(2**31),
         "object_count": 2**32 - 1, "sample_ns": 2**64 - 1},
        {"range_m": -F32_MAX, "object_id": 2**31 - 1, "object_count": 0,
         "sample_ns": 2**53 + 1},
        {"range_m": F32_TRUE_MIN, "object_id": 0, "object_count": 2**31,
         "sample_ns": 2**63},
        {"range_m": F32_TENTH, "object_id": 5, "object_count": 1,
         "sample_ns": 2**53 - 1},
    ])
    def test_each_boundary_value_survives_the_round_trip(
        self, importer, written
    ):
        participant = importer()
        (_, published), = participant.on_step(
            0, STEP_PERIOD_NS, [Input(IN, 0, written)]
        )
        assert published == written

    def test_the_values_come_back_as_their_own_python_types(self, importer):
        participant = importer()
        (_, published), = participant.on_step(0, STEP_PERIOD_NS, [Input(IN, 0, {
            "range_m": 2.0, "object_id": 3, "object_count": 4,
            "sample_ns": 5,
        })])
        assert {name: type(value) for name, value in published.items()} == {
            "range_m": float, "object_id": int, "object_count": int,
            "sample_ns": int,
        }

    def test_each_type_takes_its_own_fmi_call(self, importer, monkeypatch):
        calls = record_calls(monkeypatch)
        participant = importer()
        participant.on_step(0, STEP_PERIOD_NS, [Input(IN, 0, {
            "range_m": 0.0, "object_id": 0, "object_count": 0, "sample_ns": 0,
        })])
        assert {"fmi3SetFloat32", "fmi3SetInt32", "fmi3SetUInt32",
                "fmi3SetUInt64", "fmi3GetFloat32", "fmi3GetInt32",
                "fmi3GetUInt32", "fmi3GetUInt64"} <= set(calls)
        assert "fmi3SetFloat64" not in calls


class TestWrongWidth:
    """A field of any type but the variable's own is refused, never coerced."""

    @pytest.mark.parametrize(("variable", "field_type", "carried_by"), [
        ("Float32_discrete_input", "f64", "f32"),
        ("Float32_discrete_input", "i32", "f32"),
        ("Int32_input", "i64", "i32"),
        ("Int32_input", "u32", "i32"),
        ("Int32_input", "f32", "i32"),
        ("UInt32_input", "i32", "u32"),
        ("UInt32_input", "u64", "u32"),
        ("UInt32_input", "u16", "u32"),
        ("UInt64_input", "u32", "u64"),
        ("UInt64_input", "i64", "u64"),
        ("UInt64_input", "f64", "u64"),
        ("Float64_continuous_input", "f32", "f64"),
    ])
    def test_a_field_of_another_type_is_refused(
        self, importer, variable, field_type, carried_by
    ):
        with pytest.raises(ManifestError) as raised:
            importer(**one_field(field_type, variable))
        message = str(raised.value)
        assert f"'{field_type}' scalar" in message
        assert f"is carried by a '{carried_by}' scalar" in message

    def test_an_array_field_is_refused(self, importer):
        mapping = one_field("u64", "UInt64_input")
        mapping["schemas"]["t.Value"]["fields"][0]["count"] = 1
        with pytest.raises(ManifestError, match="'u64' array of 1"):
            importer(**mapping)

    @pytest.mark.parametrize(("variable", "kind"), [
        ("Int8_input", "Int8"),
        ("Int16_input", "Int16"),
        ("UInt16_input", "UInt16"),
        ("Enumeration_input", "Enumeration"),
        ("String_input", "String"),
    ])
    def test_an_unselected_type_is_reported_by_its_name(
        self, importer, variable, kind
    ):
        with pytest.raises(ManifestError, match=f"which is a {kind} variable"):
            importer(**one_field("i64", variable))


class TestStartLifecycle:
    """A start value is written before initialization, in the state FMI 3.0
    has the importer write it in."""

    STARTS = [
        "Float32_discrete_input=0.1",
        "Int32_input=-2147483648",
        "UInt32_input=4294967295",
        "UInt64_input=18446744073709551615",
    ]

    def test_input_starts_are_written_before_initialization(
        self, importer, monkeypatch
    ):
        calls = record_calls(monkeypatch)
        importer(binds=BINDINGS[4:], channels={OUT: ("n.Sensor", "out")},
                 starts=self.STARTS)
        initializing = calls.index("fmi3EnterInitializationMode")
        assert calls[:initializing] == [
            "fmi3SetFloat32", "fmi3SetInt32", "fmi3SetUInt32",
            "fmi3SetUInt64",
        ]
        assert "fmi3EnterConfigurationMode" not in calls

    def test_a_start_value_is_what_an_unfed_step_sees(self, importer):
        participant = importer(
            binds=BINDINGS[4:], channels={OUT: ("n.Sensor", "out")},
            starts=self.STARTS,
        )
        (_, published), = participant.on_step(0, STEP_PERIOD_NS, [])
        assert published == {
            "range_m": F32_TENTH, "object_id": -(2**31),
            "object_count": 2**32 - 1, "sample_ns": 2**64 - 1,
        }

    def test_a_structural_parameter_is_written_in_configuration_mode(
        self, importer, monkeypatch, tmp_path
    ):
        """The Reference FMU accepts either sequence, so the call seam is
        where the contract is visible."""
        structural = with_description(
            tmp_path, "structural",
            lambda text: text.replace(
                '<UInt64 name="UInt64_input" valueReference="25" '
                'causality="input"',
                '<UInt64 name="UInt64_input" valueReference="25" '
                'causality="structuralParameter"',
            ),
        )
        calls = record_calls(monkeypatch)
        participant = FmuParticipant(
            structural, binds=BINDINGS[4:],
            starts=["UInt64_input=9007199254740993", "Int32_input=-5"],
        )
        try:
            participant.on_init(init_line({OUT: ("n.Sensor", "out")}, SCHEMAS))
        finally:
            participant.close()
        assert calls[:5] == [
            "fmi3EnterConfigurationMode", "fmi3SetUInt64",
            "fmi3ExitConfigurationMode", "fmi3SetInt32",
            "fmi3EnterInitializationMode",
        ]


class TestCallStatusContext:
    """An FMI call that fails says which variables it was carrying."""

    def test_a_failed_start_value_names_the_variable_and_the_state(
        self, importer, monkeypatch
    ):
        failing_call(monkeypatch, "fmi3SetUInt32")
        with pytest.raises(ParticipantFailure) as raised:
            importer(binds=BINDINGS[4:], channels={OUT: ("n.Sensor", "out")},
                     starts=["UInt32_input=3"])
        message = str(raised.value)
        assert "fmi3SetUInt32 returned Error" in message
        assert "'UInt32_input'" in message
        assert "before initialization" in message

    def test_a_failed_write_names_the_channel_and_its_variables(
        self, importer, monkeypatch
    ):
        participant = importer()
        failing_call(monkeypatch, "fmi3SetUInt64")
        with pytest.raises(ParticipantFailure) as raised:
            participant.on_step(0, STEP_PERIOD_NS, [Input(IN, 0, {
                "range_m": 0.0, "object_id": 0, "object_count": 0,
                "sample_ns": 1,
            })])
        message = str(raised.value)
        assert "fmi3SetUInt64 returned Error" in message
        assert f"Channel {IN!r}" in message
        assert "'UInt64_input'" in message

    def test_a_failed_read_names_the_channel_and_its_variables(
        self, importer, monkeypatch
    ):
        participant = importer()
        failing_call(monkeypatch, "fmi3GetInt32")
        with pytest.raises(ParticipantFailure) as raised:
            participant.on_step(0, STEP_PERIOD_NS, [])
        message = str(raised.value)
        assert "fmi3GetInt32 returned Error" in message
        assert f"Channel {OUT!r}" in message
        assert "'Int32_output'" in message


# Inspection ------------------------------------------------------------------------


def mapping_document(binds: list[str], fields: list[dict] = FIELDS,
                     starts: list[str] = ()) -> dict:
    return {
        "sil_fmi_mapping": 1,
        "schemas": {"n.Sensor": {"fields": fields}},
        "channels": {IN: {"schema": "n.Sensor", "direction": "in"},
                     OUT: {"schema": "n.Sensor", "direction": "out"}},
        "bind": binds, "start": list(starts),
    }


@pytest.fixture(scope="module")
def declared():
    """Each `Feedthrough` variable, as the inspection reports it."""
    return {v["name"]: v for v in inspect(FEEDTHROUGH)["variables"]}


class TestInspection:
    """The inspection reaches the verdict the importer reaches."""

    @pytest.mark.parametrize("name", [
        "Float32_continuous_input", "Float32_discrete_output", "Int32_input",
        "Int32_output", "UInt32_input", "UInt32_output", "UInt64_input",
        "UInt64_output",
    ])
    def test_each_selected_type_is_mappable(self, declared, name):
        assert declared[name]["unmappable"] is None

    @pytest.mark.parametrize("name", [
        "Int8_input", "Int16_input", "UInt16_output", "String_input",
        "Enumeration_input",
    ])
    def test_each_unselected_type_is_reported(self, declared, name):
        assert declared[name]["unmappable"] == (
            f"which is a {declared[name]['type']} variable; this importer "
            f"maps Binary and the scalar types Float64, Boolean, Float32, "
            f"Int32, UInt32, UInt64, UInt8, Int64"
        )

    def test_unused_unsupported_variables_leave_the_archive_usable(self):
        report = inspect(FEEDTHROUGH, mapping_document(BINDINGS))
        assert report["verdict"] == "compatible"
        assert report["mapping"]["accepted"] is True

    @pytest.mark.parametrize(("binds", "fields", "starts"), [
        (BINDINGS, [*FIELDS[:3], {"name": "sample_ns", "type": "u32"}], []),
        (BINDINGS, [{"name": "range_m", "type": "f64"}, *FIELDS[1:]], []),
        (BINDINGS, FIELDS, ["UInt32_input=-1"]),
        (BINDINGS, FIELDS, ["Float32_continuous_input=1e39"]),
    ])
    def test_a_rejection_agrees_with_the_runtime(
        self, importer, binds, fields, starts
    ):
        report = inspect(FEEDTHROUGH, mapping_document(binds, fields, starts))
        assert report["verdict"] == "mapping-rejected"
        with pytest.raises(ManifestError) as raised:
            importer(binds=binds, starts=starts,
                     schemas={"n.Sensor": {"fields": fields}})
        assert report["mapping"]["rejection"] == str(raised.value)


# The Run boundary --------------------------------------------------------------------


RUN_DURATION_NS = 120_000_000


def numeric_manifest(*, transport: str = "shm",
                     binds: list[str] = BINDINGS,
                     schemas: dict = SCHEMAS) -> Manifest:
    """A stimulus feeding the four types into `Feedthrough`, and the importer
    publishing them back, both over shared memory or both inline.
    """
    m = Manifest(duration_ns=RUN_DURATION_NS)
    m.add_schemas(schemas)
    for channel in (IN, OUT):
        if transport == "shm":
            m.add_channel(channel, schema="n.Sensor", transport="shm", slots=2)
        else:
            m.add_channel(channel, schema="n.Sensor")
    m.add_process(
        "stimulus",
        command=[sys.executable, str(STIMULUS), IN],
        step_period_ns=STEP_PERIOD_NS,
        publishes=[IN],
    )
    m.add_process(
        "importer",
        command=[sys.executable, "-m", "sil.fmi", str(FEEDTHROUGH),
                 *(argument for bind in binds for argument in ("--bind", bind))],
        step_period_ns=STEP_PERIOD_NS,
        subscribes=[SubscriberRoute(IN, capacity=2)],
        publishes=[OUT],
        priority=1,
    )
    return m


@pytest.fixture(scope="module", params=["shm", "inline"])
def numeric_result(request, sil_run, tmp_path_factory):
    return request.param, run_simulation(
        numeric_manifest(transport=request.param),
        runner=sil_run,
        workdir=tmp_path_factory.mktemp(f"fmi-numeric-{request.param}"),
    )


class TestRunBoundary:

    def test_the_fmu_publishes_each_value_one_step_later(self, numeric_result):
        _, result = numeric_result
        published = [fields for _, fields in result.messages(OUT)]
        stimulus = [fields for _, fields in result.messages(IN)]
        assert len(published) == RUN_DURATION_NS // STEP_PERIOD_NS
        assert published[1:] == stimulus[:-1]

    def test_the_recorded_values_are_the_boundaries_themselves(
        self, numeric_result
    ):
        """Compared with constants rather than with the stimulus, so a
        conversion both ends share cannot hide."""
        _, result = numeric_result
        published = [fields for _, fields in result.messages(OUT)]
        assert published[0] == {"range_m": 0.0, "object_id": 0,
                                "object_count": 0, "sample_ns": 0}
        assert published[2] == {
            "range_m": F32_MAX, "object_id": -(2**31),
            "object_count": 2**32 - 1, "sample_ns": 2**64 - 1,
        }
        assert published[3]["sample_ns"] == 2**53 + 1
        assert published[4]["range_m"] == F32_TRUE_MIN

    @pytest.mark.parametrize("transport", ["shm", "inline"])
    def test_two_runs_record_identical_bytes(self, sil_run, tmp_path, transport):
        ref = numeric_manifest(transport=transport).write(
            tmp_path / "numeric.json")
        proc = subprocess.run(
            [sys.executable, "-m", "sil.check", str(ref.path),
             "--runner", str(sil_run)],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.startswith("deterministic: ")

    def test_a_wrong_width_field_is_a_manifest_error(self, run_sil, tmp_path):
        """A Sample time bound to a `u32` field would drop its high bits; the
        Run refuses it before the FMU is stepped."""
        narrow = {"n.Sensor": {"fields": [
            *FIELDS[:3], {"name": "sample_ns", "type": "u32"},
        ]}}
        proc = run_sil(numeric_manifest(schemas=narrow).write(
            tmp_path / "narrow.json").path)
        assert proc.returncode == 2, proc.stderr
        assert "'UInt64_input'" in proc.stderr
        assert "'u64' scalar" in proc.stderr


# Signal coupling ----------------------------------------------------------------------


class TestCoupling:
    """A connection between two FMUs carries the four types like any other."""

    INPUTS = [
        "Float32_continuous_input", "Float32_discrete_input",
        "Float64_continuous_input", "Float64_discrete_input", "Int8_input",
        "UInt8_input", "Int16_input", "UInt16_input", "Int32_input",
        "UInt32_input", "Int64_input", "UInt64_input", "Boolean_input",
        "String_input", "Binary_input", "Enumeration_input",
    ]

    def document(self, field_type: str = "u64") -> dict:
        connected = "UInt64_input"
        return {
            "sil_fmu_coupling": 1, "duration_ns": 30_000_000,
            "fmus": {
                "left": {"step_period_ns": STEP_PERIOD_NS, "priority": 0,
                         "start": [{"variable": connected,
                                    "value": "18446744073709551615",
                                    "unit": None}],
                         "hold": [v for v in self.INPUTS if v != connected]},
                "right": {"step_period_ns": STEP_PERIOD_NS, "priority": 1,
                          "start": [{"variable": connected, "value": "7",
                                     "unit": None}],
                          "hold": [v for v in self.INPUTS if v != connected]},
            },
            "channels": {
                "left.sample": {
                    "publisher": "left", "latency_ns": STEP_PERIOD_NS,
                    "fields": [{"name": "sample_ns", "type": field_type,
                                "variable": "UInt64_output", "unit": None}],
                    "subscribers": {"right": {
                        "capacity": 2, "overflow": "fail",
                        "bind": {"sample_ns": connected}}},
                },
            },
        }

    def test_a_uint64_connection_runs_exactly(self, tmp_path, sil_run):
        document = tmp_path / "coupling.json"
        document.write_text(json.dumps(self.document()))
        manifest = tmp_path / "manifest.json"
        couple(document, {"left": FEEDTHROUGH, "right": FEEDTHROUGH}, manifest)
        out = tmp_path / "run.mcap"
        proc = subprocess.run([str(sil_run), str(manifest), "-o", str(out)],
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        values = [int.from_bytes(data, "little")
                  for topic, _, data in read_records(out)
                  if topic == "left.sample"]
        assert values == [2**64 - 1] * 3

    def test_a_narrower_field_is_refused_before_anything_runs(self, tmp_path):
        document = tmp_path / "coupling.json"
        document.write_text(json.dumps(self.document("u32")))
        with pytest.raises(AuthoringError, match="'u64' scalar"):
            couple(document, {"left": FEEDTHROUGH, "right": FEEDTHROUGH},
                   tmp_path / "manifest.json")
