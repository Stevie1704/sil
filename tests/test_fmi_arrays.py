"""Fixed-size numeric FMI arrays through bounded Channel fields (issue #190).

The FMU under test is `tests/fixtures/fmi_array.c`, described by
`tests/array_fixture.py`: [8] arrays of Float32, Int32, UInt32 and UInt64,
a Float32 [3] beside a Float32 scalar, a UInt32 scalar beside a UInt32 [8],
and a Float64 [2,3] matrix whose output depends on each element's indices.
Its accessors refuse a call whose `nValues` is not what its references hold
together.

The behavior is specified here, independently of the code under test:

* an array whose every dimension is a literal positive `start` is carried by
  one Schema field of its element type, whose `count` is the product of the
  dimensions. The values are flattened in FMI's row-major order: the last
  dimension varies fastest. A scalar keeps its scalar field;
* Float32, Float64, Int32, UInt32 and UInt64 arrays are mapped. Boolean and
  Binary arrays, a dimension sized by another variable, a dimension that is
  not positive and a count whose buffer overflows `size_t` are refused
  before anything is loaded;
* an array's start value lists every value, separated by single spaces, in
  row-major order. A start with any other count is refused: nothing is
  broadcast and nothing is filled in.
"""

from __future__ import annotations

import copy
import ctypes
import csv
import json
from fractions import Fraction
from pathlib import Path

import pytest
from array_fixture import (
    MODEL_IDENTIFIER,
    VARIABLES,
    array_fmu,
    column_major,
    expected_matrix,
    field,
    nested,
    row_major,
)
from conftest import ROOT, run_manifest

from sil import schema
from sil.compare import compare, read_contract
from sil.csv_recording import convert
from sil.fmi import CoSimulation, FmuParticipant, library_suffix, platform_directory
from sil.fmi.authoring import author
from sil.fmi.coupling import AuthoringError, couple
from sil.fmi.description import ModelDescription, Variable
from sil.fmi.inspection import inspect
from sil.fmi.mapping import start_value, unmappable
from sil.fmi.runtime import ScalarBuffer
from sil.participant import Input, ManifestError, ParticipantFailure
from sil.recording import read_records

EXAMPLE = ROOT / "examples" / "fmu-array"
STEP_PERIOD_NS = 10_000_000

F32_MAX = (2 - 2**-23) * 2**127
F32_TRUE_MIN = 2**-149
F32_TENTH = float(Fraction(13421773, 2**27))

IN, OUT = "a.In", "a.Out"
INPUTS = ["matrix_in", "range_in", "trim_in", "gain_in", "id_in", "count_in",
          "class_in", "sample_in"]
FIELDS = [field(name) for name in INPUTS]
SCHEMAS = {"a.Objects": {"fields": FIELDS}}
CHANNELS = {IN: ("a.Objects", "in"), OUT: ("a.Objects", "out")}
BINDINGS = [
    f"{channel}:{f['name']}={f['name']}_{suffix}"
    for channel, suffix in ((IN, "in"), (OUT, "out"))
    for f in FIELDS
]

MATRIX = [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
ZERO = [[0.0] * 3, [0.0] * 3]
WRITTEN = {
    "matrix": row_major(MATRIX),
    "range": [F32_TENTH, F32_MAX, -F32_MAX, F32_TRUE_MIN, 2.0**24, -0.25,
              12.5, 0.0],
    "trim": [1.5, -2.5, 0.125],
    "gain": 2.0,
    "id": [-(2**31), 2**31 - 1, -1, 0, 1, 42, -42, 65536],
    "count": 2**32 - 1,
    "class": [0, 2**32 - 1, 1, 2**31, 3, 4, 5, 6],
    "sample": [2**64 - 1, 2**53 + 1, 2**63, 0, 1, 2**53, 2**64 - 2,
               40_000_000],
}


def echoed(written: dict, bias=ZERO) -> dict:
    """What the fixture hands back for `written`, by its stated rule."""
    return {**written, "matrix": row_major(
        expected_matrix(nested(written["matrix"]), bias))}


@pytest.fixture(scope="session")
def binary(build_dir) -> Path:
    built = build_dir / f"{MODEL_IDENTIFIER}{library_suffix()}"
    assert built.exists(), f"array FMU binary was not built at {built}"
    return built


@pytest.fixture
def make_fmu(tmp_path, binary):
    """The fixture's archive, its description optionally rewritten."""
    def _make(rewrite=lambda text: text, name: str = "ArrayEcho") -> Path:
        return array_fmu(tmp_path / f"{name}.fmu", binary,
                         platform_directory(), rewrite)
    return _make


@pytest.fixture
def fmu(make_fmu) -> Path:
    return make_fmu()


def init_line(channels: dict = CHANNELS, schemas: dict = SCHEMAS) -> dict:
    return {
        "op": "init", "name": "fmu", "schemas": schemas,
        "channels": {
            channel: {"schema": schema_name, "direction": direction}
            for channel, (schema_name, direction) in channels.items()
        },
    }


@pytest.fixture
def importer(fmu):
    """Build an initialized importer over the fixture, torn down after."""
    built = []

    def _build(*, binds=BINDINGS, starts=(), channels=CHANNELS,
               schemas=SCHEMAS, archive=fmu):
        participant = FmuParticipant(archive, binds=list(binds),
                                     starts=list(starts))
        built.append(participant)
        participant.on_init(init_line(channels, schemas))
        return participant

    yield _build
    for participant in built:
        try:
            participant.close()
        except ParticipantFailure:
            pass


def with_fields(edit) -> dict:
    """The Channels' schema with its fields edited."""
    fields = copy.deepcopy(FIELDS)
    edit({f["name"]: f for f in fields})
    return {"a.Objects": {"fields": fields}}


def redeclared(name: str, dimensions: str, kind: str | None = None):
    """A description rewrite that gives `name` other dimensions or type."""
    _, declared_kind, _, shape, _ = VARIABLES[name]
    old = "".join(f'<Dimension start="{extent}"/>' for extent in shape)

    def rewrite(text: str) -> str:
        start = text.index(f'name="{name}"')
        opening = text.rindex("<", 0, start)
        end = text.index(f"</{declared_kind}>", start) + len(
            f"</{declared_kind}>")
        element = text[opening:end].replace(old, dimensions, 1) if old else (
            text[opening:end].replace(f"></{declared_kind}>",
                                      f">{dimensions}</{declared_kind}>"))
        if kind is not None:
            element = element.replace(f"<{declared_kind} ", f"<{kind} ")
            element = element.replace(f"</{declared_kind}>", f"</{kind}>")
            element = element.replace(' start="0 0 0 0 0 0 0 0"', "")
        return text[:opening] + element + text[end:]
    return rewrite


def variable(kind: str, shape: tuple, count) -> Variable:
    return Variable(name="v", reference=1, kind=kind, causality="input",
                    max_size=None, value_count=count, shape=shape)


# The description ------------------------------------------------------------------


class TestShape:

    def test_each_literal_dimension_is_kept_with_its_count(self, fmu,
                                                          tmp_path):
        report = inspect(fmu)
        declared = {v["name"]: v for v in report["variables"]}
        assert declared["matrix_in"]["dimensions"] == [{"start": 2},
                                                       {"start": 3}]
        assert declared["matrix_in"]["value_count"] == 6
        assert declared["range_in"]["value_count"] == 8
        assert declared["gain_in"]["dimensions"] == []
        assert declared["gain_in"]["value_count"] == 1
        assert all(v["unmappable"] is None for v in report["variables"]
                   if v["name"] != "time")

    @pytest.mark.parametrize(("kind", "shape", "count", "reason"), [
        ("Boolean", (4,), 4, "arrays of Float64, Float32, Int32, UInt32, "
                             "UInt64, UInt8, Int64 only"),
        ("Binary", (2,), 2, "Binary variables of one value"),
        ("Float64", (None,), None, "dimensions with a literal start only"),
        ("Float64", (2, None), None, "dimensions with a literal start only"),
        ("Float32", (0,), None, "a dimension is a positive integer"),
        ("Int32", (3, -1), None, "a dimension is a positive integer"),
        ("Float64", (2**32, 2**29), 2**61, "overflow a size_t buffer"),
        ("UInt32", (2**62,), 2**62, "overflow a size_t buffer"),
    ])
    def test_an_unsupported_array_is_unmappable(self, kind, shape, count,
                                                reason):
        assert reason in unmappable(variable(kind, shape, count))

    @pytest.mark.parametrize("kind", ["Float32", "Float64", "Int32", "UInt32",
                                      "UInt64", "UInt8", "Int64"])
    def test_a_literal_array_of_a_mapped_type_is_mappable(self, kind):
        assert unmappable(variable(kind, (2, 3), 6)) is None

    @pytest.mark.parametrize("kind", ["Float32", "Boolean", "UInt64"])
    def test_dimensions_of_one_value_are_a_scalar(self, kind):
        """`<Dimension start="1"/>` is one value written the long way, as it
        was before arrays were mapped."""
        one = variable(kind, (1, 1), 1)
        assert not one.is_array and unmappable(one) is None
        assert start_value(one, "true" if kind == "Boolean" else "1") in (
            1, 1.0)

    def test_a_dimension_of_one_keeps_its_scalar_field(self, importer,
                                                        make_fmu):
        archive = make_fmu(redeclared("gain_in", '<Dimension start="1"/>'),
                           "one")
        participant = importer(archive=archive, starts=["gain_in=0.5"])
        (_, published), = participant.on_step(0, STEP_PERIOD_NS,
                                              [Input(IN, 0, WRITTEN)])
        assert published["gain"] == 2.0

    def test_a_buffer_too_large_to_allocate_is_refused_before_loading(
        self, importer, make_fmu, monkeypatch
    ):
        """2^60 Float32 values fit a size_t count, but not this process."""
        calls = record_calls(monkeypatch)
        archive = make_fmu(redeclared(
            "trim_in", f'<Dimension start="{2**60}"/>'), "huge")
        schemas = with_fields(lambda f: f["trim"].update(count=2**60))
        with pytest.raises(ManifestError, match="cannot be allocated"):
            importer(archive=archive, schemas=schemas,
                     binds=[b for b in BINDINGS if b.startswith(IN)])
        assert calls == []

    def test_a_dimension_sized_by_a_structural_parameter_is_refused(
        self, importer, make_fmu
    ):
        archive = make_fmu(redeclared(
            "range_in", '<Dimension valueReference="7"/>'), "sized")
        with pytest.raises(ManifestError, match="literal start only"):
            importer(archive=archive)

    def test_a_zero_dimension_is_refused_before_loading(
        self, importer, make_fmu, monkeypatch
    ):
        calls = record_calls(monkeypatch)
        archive = make_fmu(redeclared("trim_in", '<Dimension start="0"/>'),
                           "zero")
        with pytest.raises(ManifestError, match="positive integer"):
            importer(archive=archive)
        assert calls == []

    def test_a_boolean_array_is_refused(self, importer, make_fmu):
        archive = make_fmu(redeclared("class_in", '<Dimension start="8"/>',
                                      "Boolean"), "boolean")
        schemas = with_fields(lambda f: f["class"].update(type="u8"))
        with pytest.raises(ManifestError, match="'class_in'.*arrays of"):
            importer(archive=archive, schemas=schemas)


# Binding ---------------------------------------------------------------------------


class TestBinding:

    @pytest.mark.parametrize(("edit", "declared", "carried"), [
        (lambda f: f["range"].update(count=7), "'f32' array of 7",
         "'f32' array of 8"),
        (lambda f: f["range"].pop("count"), "'f32' scalar",
         "'f32' array of 8"),
        (lambda f: f["range"].update(type="f64"), "'f64' array of 8",
         "'f32' array of 8"),
        (lambda f: f["id"].update(type="u32"), "'u32' array of 8",
         "'i32' array of 8"),
        (lambda f: f["matrix"].update(count=5), "'f64' array of 5",
         "'f64' array of 6"),
        (lambda f: f["gain"].update(count=1), "'f32' array of 1",
         "'f32' scalar"),
    ])
    def test_a_field_of_another_shape_or_type_is_refused(
        self, importer, edit, declared, carried
    ):
        with pytest.raises(ManifestError) as raised:
            importer(schemas=with_fields(edit))
        message = str(raised.value)
        assert f"as a {declared};" in message
        assert f"is carried by a {carried}" in message

    def test_the_rejection_names_the_declared_dimensions(self, importer):
        with pytest.raises(ManifestError,
                           match=r"'matrix_in' of dimensions \[2x3\] of 6 "
                                 r"values is carried by a 'f64' array of 6"):
            importer(schemas=with_fields(
                lambda f: f["matrix"].update(count=3)))

    def test_inspection_reaches_the_importer_s_verdict(self, fmu):
        rejected = inspect(fmu, mapping_document(
            schemas=with_fields(lambda f: f["sample"].update(count=9))))
        assert rejected["verdict"] == "mapping-rejected"
        assert "'u64' array of 8" in rejected["mapping"]["rejection"]
        assert inspect(fmu, mapping_document())["mapping"]["accepted"]


def mapping_document(binds=BINDINGS, schemas=SCHEMAS, starts=()) -> dict:
    return {
        "sil_fmi_mapping": 1, "schemas": schemas,
        "channels": {IN: {"schema": "a.Objects", "direction": "in"},
                     OUT: {"schema": "a.Objects", "direction": "out"}},
        "bind": list(binds), "start": list(starts),
    }


# Start values -------------------------------------------------------------------


class TestArrayStartValues:

    def test_a_start_lists_every_value_in_row_major_order(self):
        assert start_value(variable("Float64", (2, 3), 6),
                           "1 2 3 4 5 6") == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        assert start_value(variable("UInt64", (2,), 2),
                           "18446744073709551615 9007199254740993") == [
            2**64 - 1, 2**53 + 1]
        assert start_value(variable("UInt32", (8,), 8),
                           "0 1 2 3 4 5 6 4294967295")[-1] == 2**32 - 1

    @pytest.mark.parametrize("text", ["1", "1 2 3 4 5", "1 2 3 4 5 6 7",
                                      "1  2 3 4 5 6", " 1 2 3 4 5 6",
                                      "1,2,3,4,5,6", ""])
    def test_any_other_count_is_refused(self, text):
        with pytest.raises(ManifestError):
            start_value(variable("Float64", (2, 3), 6), text)

    def test_one_value_is_not_broadcast(self):
        with pytest.raises(ManifestError,
                           match=r"lists 1 values .* dimensions \[2x3\] of 6"):
            start_value(variable("Float64", (2, 3), 6), "0")

    @pytest.mark.parametrize(("kind", "text", "problem"), [
        ("Int32", "0 2147483648", "outside the Int32 range"),
        ("UInt32", "1 -1", "is negative"),
        ("Float32", "0 1e39", "outside the Float32 range"),
        ("Float32", "1e-46 0", "underflows to zero"),
    ])
    def test_each_element_is_read_by_its_type(self, kind, text, problem):
        with pytest.raises(ManifestError, match=problem):
            start_value(variable(kind, (2,), 2), text)

    @pytest.mark.parametrize(("kind", "shape", "count"), [
        ("Boolean", (2,), 2), ("Float64", (None,), None),
        ("Float64", (0,), None),
    ])
    def test_an_unmapped_array_takes_no_start(self, kind, shape, count):
        with pytest.raises(ManifestError, match="start value for FMU variable"):
            start_value(variable(kind, shape, count), "1 2")

    def test_a_parameter_start_is_written_before_initialization(
        self, importer, monkeypatch
    ):
        calls = record_calls(monkeypatch, with_counts=True)
        participant = importer(starts=["bias=0.5 -1 2.25 100 -0.125 7",
                                       "matrix_in=1 2 3 4 5 6"])
        initializing = calls.index(("fmi3EnterInitializationMode",))
        assert calls[:initializing] == [("fmi3SetFloat64", 1, 6),
                                        ("fmi3SetFloat64", 1, 6)]
        bias = [[0.5, -1.0, 2.25], [100.0, -0.125, 7.0]]
        (_, published), = participant.on_step(0, STEP_PERIOD_NS, [])
        assert published["matrix"] == row_major(expected_matrix(MATRIX, bias))

    def test_the_fixture_refuses_a_parameter_after_initialization(
        self, importer
    ):
        """The lifecycle the start is applied in is the one the FMU takes."""
        participant = importer()
        fmu = participant._fmu
        with pytest.raises(ParticipantFailure, match="returned Error"):
            ScalarBuffer("Float64", [VARIABLES["bias"][0]], 6).write(
                fmu, [0.0] * 6)


# Native calls ------------------------------------------------------------------


def record_calls(monkeypatch, with_counts: bool = False) -> list:
    """Each FMI call; an accessor with its nValueReferences and nValues."""
    calls: list = []
    called = CoSimulation._call

    def record(self, name, *arguments):
        if not with_counts:
            calls.append(name)
        elif name.startswith(("fmi3Get", "fmi3Set")) and len(arguments) == 4:
            calls.append((name, arguments[1], arguments[3]))
        else:
            calls.append((name,))
        return called(self, name, *arguments)

    monkeypatch.setattr(CoSimulation, "_call", record)
    return calls


class TestNativeCalls:

    def test_one_call_per_type_counts_references_and_values_apart(
        self, importer, monkeypatch
    ):
        participant = importer()
        calls = record_calls(monkeypatch, with_counts=True)
        participant.on_step(0, STEP_PERIOD_NS, [Input(IN, 0, WRITTEN)])
        accessors = {call for call in calls if len(call) == 3}
        assert accessors == {
            ("fmi3SetFloat64", 1, 6), ("fmi3GetFloat64", 1, 6),
            # range [8], trim [3] and the gain scalar in one call
            ("fmi3SetFloat32", 3, 12), ("fmi3GetFloat32", 3, 12),
            ("fmi3SetInt32", 1, 8), ("fmi3GetInt32", 1, 8),
            # the count scalar and class [8] in one call
            ("fmi3SetUInt32", 2, 9), ("fmi3GetUInt32", 2, 9),
            ("fmi3SetUInt64", 1, 8), ("fmi3GetUInt64", 1, 8),
        }

    @pytest.fixture
    def instance(self, fmu, tmp_path):
        """The fixture, initialized and driven through buffers alone."""
        import zipfile
        extracted = tmp_path / "extracted"
        with zipfile.ZipFile(fmu) as archive:
            archive.extractall(extracted)
        description = ModelDescription.read(extracted)
        instance = CoSimulation(description.binary(extracted), description)
        instance.initialize()
        yield instance
        instance.close()

    def test_one_value_per_reference_is_refused_by_the_fixture(self, instance):
        """The control: the importer's nValues is what the fixture checks."""
        references = [VARIABLES[n][0] for n in ("range_in", "trim_in",
                                                "gain_in")]
        with pytest.raises(ParticipantFailure,
                           match="fmi3SetFloat32 returned Error"):
            ScalarBuffer("Float32", references).write(instance, [0.0] * 3)
        ScalarBuffer("Float32", references, 12).write(instance, [0.0] * 12)

    def test_a_buffer_refuses_a_value_list_of_another_length(self, instance):
        buffer = ScalarBuffer("Float32", [VARIABLES["range_in"][0]], 8)
        with pytest.raises(ParticipantFailure, match="7 Float32 values"):
            buffer.write(instance, [0.0] * 7)


# The round trip -----------------------------------------------------------------


class TestRoundTrip:

    def test_every_element_survives_the_round_trip(self, importer):
        participant = importer()
        (_, published), = participant.on_step(
            0, STEP_PERIOD_NS, [Input(IN, 0, WRITTEN)])
        assert published == echoed(WRITTEN)

    def test_the_matrix_is_flattened_row_major(self, importer):
        """The negative control: the transposed order is not what the FMU
        computed, so a test that expected it would fail."""
        participant = importer()
        (_, published), = participant.on_step(
            0, STEP_PERIOD_NS, [Input(IN, 0, WRITTEN)])
        expected = expected_matrix(MATRIX, ZERO)
        assert published["matrix"] == row_major(expected)
        assert published["matrix"] != column_major(expected)
        assert published["matrix"] == [12.0, 14.0, 16.0, 25.0, 27.0, 29.0]

    def test_float32_elements_round_to_float32(self, importer):
        participant = importer()
        written = {**WRITTEN, "range": [2.0**24 + 1] + WRITTEN["range"][1:]}
        (_, published), = participant.on_step(
            0, STEP_PERIOD_NS, [Input(IN, 0, written)])
        assert published["range"][0] == 2.0**24

    def test_scalars_beside_arrays_keep_their_fields(self, importer):
        participant = importer()
        (_, published), = participant.on_step(
            0, STEP_PERIOD_NS, [Input(IN, 0, WRITTEN)])
        assert published["gain"] == 2.0 and type(published["count"]) is int
        assert published["trim"] == [1.5, -2.5, 0.125]


# The recorded-input Run -----------------------------------------------------------


def independent_outputs(binary: Path, rows: list[dict], bias: list[float]
                        ) -> list[dict]:
    """The fixture driven by its own C interface, without the Importer.

    Every input is set and every output read with the value counts stated
    here, from the value references the description declares. What it
    returns per row is what the FMU computed in the Step that row fed.
    """
    library = ctypes.CDLL(str(binary))
    library.fmi3InstantiateCoSimulation.restype = ctypes.c_void_p
    instance = ctypes.c_void_p(library.fmi3InstantiateCoSimulation(
        b"independent", b"", None, False, False, False, False, None, 0,
        None, None, None))
    elements = {"Float32": ctypes.c_float, "Float64": ctypes.c_double,
                "Int32": ctypes.c_int32, "UInt32": ctypes.c_uint32,
                "UInt64": ctypes.c_uint64}

    def call(name, *arguments):
        assert getattr(library, name)(instance, *arguments) == 0, name

    def set_values(name, values):
        reference, kind = VARIABLES[name][:2]
        flat = values if isinstance(values, list) else [values]
        call(f"fmi3Set{kind}", (ctypes.c_uint32 * 1)(reference),
             ctypes.c_size_t(1), (elements[kind] * len(flat))(*flat),
             ctypes.c_size_t(len(flat)))

    def get_values(name, count):
        reference, kind = VARIABLES[name][:2]
        values = (elements[kind] * count)()
        call(f"fmi3Get{kind}", (ctypes.c_uint32 * 1)(reference),
             ctypes.c_size_t(1), values, ctypes.c_size_t(count))
        return list(values)

    set_values("bias", bias)
    call("fmi3EnterInitializationMode", False, ctypes.c_double(0), ctypes.c_double(0),
         False, ctypes.c_double(0))
    call("fmi3ExitInitializationMode")
    outputs = []
    for row in rows:
        for name in INPUTS:
            set_values(name, row[name.rsplit("_", 1)[0]])
        outputs.append({
            f["name"]: (get_values(f"{f['name']}_out", f["count"])
                        if "count" in f
                        else get_values(f"{f['name']}_out", 1)[0])
            for f in FIELDS
        })
    library.fmi3FreeInstance(instance)
    return outputs


@pytest.fixture(scope="module")
def prepared(tmp_path_factory, build_dir) -> dict:
    workdir = tmp_path_factory.mktemp("fmu-array")
    binary = build_dir / f"{MODEL_IDENTIFIER}{library_suffix()}"
    fmu = array_fmu(workdir / "ArrayEcho.fmu", binary, platform_directory())
    convert(EXAMPLE / "mapping.json", EXAMPLE / "recorded.csv",
            workdir / "recorded.mcap")
    convert(EXAMPLE / "reference-mapping.json", EXAMPLE / "reference.csv",
            workdir / "reference.mcap")
    inline = workdir / "inline.json"
    receipt = author(EXAMPLE / "authoring.json", fmu,
                     workdir / "recorded.mcap", inline)
    shm = json.loads(inline.read_text())
    for channel in shm["channels"].values():
        channel.update(transport="shm", slots=2)
    (workdir / "shm.json").write_text(json.dumps(shm))
    return {"dir": workdir, "fmu": fmu, "binary": binary, "receipt": receipt}


def ran(sil_run: Path, manifest: Path, name: str) -> Path:
    proc = run_manifest(sil_run, manifest, manifest.with_name(f"{name}.mcap"))
    assert proc.returncode == 0, proc.stderr
    return proc.mcap_path


def published(recording: Path, manifest: Path) -> list[dict]:
    objects = schema.load(
        json.loads(manifest.read_text())["schemas"])["array.Objects"]
    return [objects.unpack(data) for topic, _, data in read_records(recording)
            if topic == "sensor.out"]


def recorded_rows() -> list[dict]:
    """The example's recorded input, as the fixture's field names hold it."""
    names = {"range_m": "range", "object_id": "id", "object_count": "count",
             "class_code": "class", "sample_time_ns": "sample"}
    objects = schema.load(json.loads(
        (EXAMPLE / "mapping.json").read_text())["schemas"])["array.Objects"]
    with open(EXAMPLE / "recorded.csv") as handle:
        rows = list(csv.DictReader(handle))
    mapping = json.loads((EXAMPLE / "mapping.json").read_text())
    columns = mapping["channels"][0]["fields"]
    result = []
    for row in rows:
        values = {}
        for f in objects.field_names:
            spec = columns[f]
            kind = next(s["type"] for s in mapping["schemas"]["array.Objects"]
                        ["fields"] if s["name"] == f)
            read = float if kind.startswith("f") else int
            values[names.get(f, f)] = (
                [read(row[c]) for c in spec["columns"]] if "columns" in spec
                else read(row[spec["column"]]))
        result.append(values)
    return result


class TestRecordedInputRun:

    @pytest.mark.parametrize("transport", ["inline", "shm"])
    def test_each_manifest_records_identical_bytes_twice(
        self, prepared, sil_run, transport
    ):
        manifest = prepared["dir"] / f"{transport}.json"
        first = ran(sil_run, manifest, f"{transport}-1")
        assert first.read_bytes() == ran(sil_run, manifest,
                                         f"{transport}-2").read_bytes()

    def test_both_transports_carry_the_same_values(self, prepared, sil_run):
        inline, shm = (prepared["dir"] / f"{t}.json" for t in ("inline", "shm"))
        assert published(ran(sil_run, inline, "values-inline"), inline) == (
            published(ran(sil_run, shm, "values-shm"), shm))

    def test_every_element_equals_an_independent_execution(
        self, prepared, sil_run
    ):
        manifest = prepared["dir"] / "inline.json"
        actual = published(ran(sil_run, manifest, "independent"), manifest)
        bias = [0.5, -1.0, 2.25, 100.0, -0.125, 7.0]
        rename = {"range_m": "range", "trim": "trim", "gain": "gain",
                  "object_id": "id", "object_count": "count",
                  "class_code": "class", "sample_time_ns": "sample",
                  "matrix": "matrix"}
        expected = independent_outputs(prepared["binary"], recorded_rows(),
                                       bias)
        # The output published in a Slot is the Step its input fed.
        assert [{rename[k]: v for k, v in message.items()}
                for message in actual] == expected

    def test_the_outputs_agree_with_the_hand_stated_reference(
        self, prepared, sil_run
    ):
        manifest = prepared["dir"] / "inline.json"
        report = compare(read_contract(EXAMPLE / "contract.json"),
                         ran(sil_run, manifest, "against-reference"),
                         prepared["dir"] / "reference.mcap")
        assert report["verdict"] == "pass", (report["coverage"],
                                             report["first_divergence"])
        assert report["channels"]["sensor.out"]["checked"] == 4

    def test_a_transposed_reference_fails(self, prepared, sil_run, tmp_path):
        """The negative control: the reference read in column-major order."""
        mapping = json.loads((EXAMPLE / "reference-mapping.json").read_text())
        mapping["channels"][0]["fields"]["matrix"]["columns"] = [
            f"matrix_{i}_{j}" for j in range(3) for i in range(2)]
        transposed = tmp_path / "transposed.json"
        transposed.write_text(json.dumps(mapping))
        convert(transposed, EXAMPLE / "reference.csv",
                tmp_path / "transposed.mcap")
        manifest = prepared["dir"] / "inline.json"
        report = compare(read_contract(EXAMPLE / "contract.json"),
                         ran(sil_run, manifest, "transposed"),
                         tmp_path / "transposed.mcap")
        assert report["verdict"] == "fail"
        first = report["first_divergence"]
        assert (first["field"], first["element"]) == ("matrix", 1)

    def test_the_recording_holds_the_boundary_elements(self, prepared,
                                                       sil_run):
        manifest = prepared["dir"] / "inline.json"
        first = published(ran(sil_run, manifest, "boundaries"), manifest)[0]
        assert first["range_m"][:5] == [F32_TENTH, F32_MAX, -F32_MAX,
                                        F32_TRUE_MIN, 2.0**24]
        assert first["object_id"][:2] == [-(2**31), 2**31 - 1]
        assert first["class_code"][1] == 2**32 - 1
        assert first["sample_time_ns"][:3] == [2**64 - 1, 2**53 + 1, 2**63]
        assert first["matrix"] == [12.5, 13.0, 18.25, 125.0, 26.875, 36.0]

    def test_the_receipt_records_each_shape(self, prepared):
        bindings = {b["variable"]: b for b in prepared["receipt"]["bindings"]}
        assert (bindings["matrix_in"]["dimensions"],
                bindings["matrix_in"]["value_count"]) == ([2, 3], 6)
        assert bindings["range_out"]["value_count"] == 8
        assert "dimensions" not in bindings["gain_in"]
        start, = prepared["receipt"]["starts"]
        assert (start["variable"], start["dimensions"],
                start["value"]) == ("bias", [2, 3], "0.5 -1 2.25 100 -0.125 7")

    def test_a_short_start_is_refused_before_anything_is_written(
        self, prepared, tmp_path
    ):
        document = json.loads((EXAMPLE / "authoring.json").read_text())
        document["start"][0]["value"] = "0.5 -1 2.25"
        path = tmp_path / "authoring.json"
        path.write_text(json.dumps(document))
        out = tmp_path / "manifest.json"
        with pytest.raises(AuthoringError, match="lists 3 values"):
            author(path, prepared["fmu"], prepared["dir"] / "recorded.mcap",
                   out)
        assert not out.exists()

    def test_a_wrong_count_is_a_manifest_error_at_the_run_boundary(
        self, prepared, run_sil, tmp_path
    ):
        """A Manifest written by hand reaches the Importer's own check."""
        manifest = json.loads((prepared["dir"] / "inline.json").read_text())
        for f in manifest["schemas"]["array.Objects"]["fields"]:
            if f["name"] == "object_id":
                f["count"] = 7
        path = tmp_path / "short.json"
        path.write_text(json.dumps(manifest))
        proc = run_sil(path)
        assert proc.returncode == 2, proc.stderr
        assert "'i32' array of 8" in proc.stderr


# Signal coupling ------------------------------------------------------------------


COUPLING = json.loads((EXAMPLE / "coupling.json").read_text())


def coupling_document(count: int = 6) -> dict:
    """The example's matrix feedback loop, its fields of `count` values."""
    document = copy.deepcopy(COUPLING)
    for channel in document["channels"].values():
        channel["fields"][0]["count"] = count
    return document


def biases() -> dict[str, list[list[float]]]:
    return {name: nested([float(v) for v in fmu["start"][0]["value"].split()])
            for name, fmu in COUPLING["fmus"].items()}


class TestCoupling:

    def coupled(self, tmp_path, fmu, document: dict) -> tuple[Path, dict]:
        path = tmp_path / "coupling.json"
        path.write_text(json.dumps(document))
        manifest = tmp_path / "manifest.json"
        receipt = couple(path, {"left": fmu, "right": fmu}, manifest)
        return manifest, receipt

    def test_a_matrix_goes_round_the_loop_by_the_fixture_s_rule(
        self, tmp_path, fmu, sil_run
    ):
        manifest, receipt = self.coupled(tmp_path, fmu, coupling_document())
        assert {(c["channel"], c["dimensions"][0], c["value_count"])
                for c in receipt["connections"]} == {
            ("left.matrix", 2, 6), ("right.matrix", 2, 6)}
        recording = ran(sil_run, manifest, "coupled")
        assert recording.read_bytes() == ran(sil_run, manifest,
                                             "coupled-again").read_bytes()
        values: dict[str, list] = {"left.matrix": [], "right.matrix": []}
        for topic, _, data in read_records(recording):
            values[topic].append(list(ctypes_doubles(data)))
        bias = biases()
        # Each FMU holds its input at the start, 0, until the first
        # delivery, and each delivery is the other's previous Message.
        previous = {"left": ZERO, "right": ZERO}
        for step in range(4):
            for name in ("left", "right"):
                expected = expected_matrix(previous[name], bias[name])
                assert values[f"{name}.matrix"][step] == row_major(expected)
            previous = {"left": nested(values["right.matrix"][step]),
                        "right": nested(values["left.matrix"][step])}

    def test_a_connection_of_another_count_is_refused(self, tmp_path, fmu):
        with pytest.raises(AuthoringError, match="'f64' array of 6"):
            self.coupled(tmp_path, fmu, coupling_document(count=3))

    def test_a_connection_of_another_shape_is_refused(
        self, tmp_path, make_fmu
    ):
        """Six values either way, but [3,2] is not [2,3]."""
        other = make_fmu(redeclared(
            "matrix_in", '<Dimension start="3"/><Dimension start="2"/>'),
            "transposed")
        path = tmp_path / "coupling.json"
        path.write_text(json.dumps(coupling_document()))
        with pytest.raises(AuthoringError, match="dimensions"):
            couple(path, {"left": make_fmu(), "right": other},
                   tmp_path / "manifest.json")


def ctypes_doubles(data: bytes) -> list[float]:
    return list((ctypes.c_double * (len(data) // 8)).from_buffer_copy(data))
