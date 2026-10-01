"""UInt8 and Int64 FMI scalars, end to end (issue #226).

The ADAS reference FMU declares its validity flags as UInt8 and its sensor
ages as Int64. A Run that replays its recorded input into that FMU binds
both, so the importer maps them as it maps the other numeric types. The
conformance target is again the Modelica Association's `Feedthrough`, which
copies each input to the output of the same type.

The behavior is specified here, independently of the code under test:

* UInt8 is carried by a `u8` field and Int64 by an `i64` field, and by no
  other field type. A `u8` field also carries a Boolean; the variable's
  declared type decides which conversion applies;
* a UInt8 holds every value in [0, 255] and an Int64 every value in
  [-2^63, 2^63 - 1], both ends included, and gives it back unchanged;
* a start value is a plain decimal integer in the type's range.
"""

from __future__ import annotations

import pytest
from conftest import ROOT
from sil.fmi import FmuParticipant
from sil.fmi.description import Variable
from sil.fmi.inspection import inspect
from sil.fmi.mapping import start_value
from sil.participant import Input, ManifestError, ParticipantFailure

FEEDTHROUGH = (
    ROOT / "tests" / "fixtures" / "reference-fmus" / "3.0" / "Feedthrough.fmu"
)
STEP_PERIOD_NS = 10_000_000
IN, OUT = "w.In", "w.Out"
FIELDS = [{"name": "validity", "type": "u8"}, {"name": "age_ns", "type": "i64"}]
VARIABLES = {"validity": "UInt8", "age_ns": "Int64"}
BINDINGS = [
    f"{channel}:{field}={variable}_{suffix}"
    for channel, suffix in ((IN, "input"), (OUT, "output"))
    for field, variable in VARIABLES.items()
]


def init_line(fields: list[dict], directions: dict[str, str]) -> dict:
    return {
        "op": "init", "name": "fmu",
        "schemas": {"w.Values": {"fields": fields}},
        "channels": {channel: {"schema": "w.Values", "direction": direction}
                     for channel, direction in directions.items()},
    }


@pytest.fixture
def importer():
    """Build an initialized importer over `Feedthrough`, torn down after."""
    built = []

    def _build(*, binds=BINDINGS, starts=(), fields=FIELDS,
               directions=None):
        participant = FmuParticipant(FEEDTHROUGH, binds=list(binds),
                                     starts=list(starts))
        built.append(participant)
        participant.on_init(init_line(
            fields, directions or {IN: "in", OUT: "out"}))
        return participant

    yield _build
    for participant in built:
        try:
            participant.close()
        except ParticipantFailure:
            pass


def variable(kind: str) -> Variable:
    return Variable(name=f"{kind}_input", reference=1, kind=kind,
                    causality="input", max_size=None, value_count=1)


class TestRoundTrip:
    @pytest.mark.parametrize("written", [
        {"validity": 0, "age_ns": -1},
        {"validity": 1, "age_ns": 0},
        {"validity": 255, "age_ns": 2**63 - 1},
        {"validity": 128, "age_ns": -(2**63)},
        {"validity": 7, "age_ns": 2**53 + 1},
    ])
    def test_each_boundary_value_survives_the_round_trip(
        self, importer, written
    ):
        participant = importer()
        (_, published), = participant.on_step(
            0, STEP_PERIOD_NS, [Input(IN, 0, written)])
        assert published == written


class TestStartValues:
    @pytest.mark.parametrize(("kind", "text", "value"), [
        ("UInt8", "0", 0),
        ("UInt8", "255", 255),
        ("Int64", "-9223372036854775808", -(2**63)),
        ("Int64", "9223372036854775807", 2**63 - 1),
    ])
    def test_a_value_in_range_is_read_exactly(self, kind, text, value):
        assert start_value(variable(kind), text) == value

    @pytest.mark.parametrize(("kind", "text"), [
        ("UInt8", "256"),
        ("UInt8", "-1"),
        ("Int64", "9223372036854775808"),
        ("Int64", "-9223372036854775809"),
        ("Int64", "1.0"),
    ])
    def test_a_value_out_of_range_or_form_is_refused(self, kind, text):
        with pytest.raises(ManifestError, match=f"{kind}_input"):
            start_value(variable(kind), text)

    def test_a_start_value_is_what_an_unfed_step_sees(self, importer):
        participant = importer(
            binds=BINDINGS[2:], directions={OUT: "out"},
            starts=["UInt8_input=200", "Int64_input=-5"])
        (_, published), = participant.on_step(0, STEP_PERIOD_NS, [])
        assert published == {"validity": 200, "age_ns": -5}


class TestWrongWidth:
    @pytest.mark.parametrize(("variable_name", "field_type", "carried_by"), [
        ("UInt8_input", "u32", "u8"),
        ("UInt8_input", "i8", "u8"),
        ("Int64_input", "u64", "i64"),
        ("Int64_input", "i32", "i64"),
        ("Int64_input", "f64", "i64"),
    ])
    def test_a_field_of_another_type_is_refused(
        self, importer, variable_name, field_type, carried_by
    ):
        with pytest.raises(ManifestError) as raised:
            importer(binds=[f"{IN}:value={variable_name}"],
                     fields=[{"name": "value", "type": field_type}],
                     directions={IN: "in"})
        message = str(raised.value)
        assert f"'{field_type}' scalar" in message
        assert f"is carried by a '{carried_by}' scalar" in message


@pytest.fixture(scope="module")
def declared():
    """Each `Feedthrough` variable, as the inspection reports it."""
    return {v["name"]: v for v in inspect(FEEDTHROUGH)["variables"]}


class TestInspection:
    @pytest.mark.parametrize("name", [
        "UInt8_input", "UInt8_output", "Int64_input", "Int64_output"])
    def test_each_type_is_mappable(self, declared, name):
        assert declared[name]["unmappable"] is None

    def test_the_mapping_is_accepted(self):
        report = inspect(FEEDTHROUGH, {
            "sil_fmi_mapping": 1,
            "schemas": {"w.Values": {"fields": FIELDS}},
            "channels": {IN: {"schema": "w.Values", "direction": "in"},
                         OUT: {"schema": "w.Values", "direction": "out"}},
            "bind": BINDINGS, "start": []})
        assert report["verdict"] == "compatible"
