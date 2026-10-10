"""`sil fmi replay`: author a recorded-data Run into one FMU (issue #186).

The helper turns an authoring document, an FMU and a converted Recording into
an ordinary canonical Manifest. Every check it makes is made before anything
runs, and the FMU checks are the inspection's, so none of these archives is
loaded: the binary each one carries is empty.
"""

from __future__ import annotations

import copy
import ctypes
import json
import zipfile
from pathlib import Path

import pytest
from conftest import ROOT, load_module

from sil.csv_recording import convert
from sil.fmi.authoring import AuthoringError, author, main

EXAMPLE = ROOT / "examples" / "fmu-replay"
FEEDTHROUGH = ROOT / "tests" / "fixtures" / "reference-fmus" / "3.0" / "Feedthrough.fmu"
DOCUMENT = json.loads((EXAMPLE / "authoring.json").read_text())

FEEDTHROUGH_INPUTS = [
    "Float32_continuous_input", "Float32_discrete_input",
    "Float64_continuous_input", "Float64_discrete_input",
    "Int8_input", "UInt8_input", "Int16_input", "UInt16_input",
    "Int32_input", "UInt32_input", "Int64_input", "UInt64_input",
    "Boolean_input", "String_input", "Binary_input", "Enumeration_input",
]
# The recorded acceleration, carried into Feedthrough's one unitless input.
FEEDTHROUGH_DOCUMENT = {
    **DOCUMENT,
    "schemas": {"ego.Acceleration": DOCUMENT["schemas"]["ego.Acceleration"]},
    "channels": {"ego.accel": DOCUMENT["channels"]["ego.accel"]},
    "bind": [{"channel": "ego.accel", "field": "accel_mps2",
              "variable": "Float64_continuous_input", "unit": None}],
    "start": [],
    "hold": [],
}


packaging = load_module("fmu_replay_package", EXAMPLE / "package.py")


def refuse_to_load(*args, **kwargs):
    raise AssertionError("an FMU binary was loaded")


@pytest.fixture(autouse=True)
def no_binary_is_loaded(monkeypatch):
    monkeypatch.setattr(ctypes, "CDLL", refuse_to_load)


@pytest.fixture
def fmu(tmp_path: Path) -> Path:
    """The example's FMU around an empty binary: authoring never loads it."""
    binary = tmp_path / "empty.so"
    binary.write_bytes(b"")
    return packaging.package(binary, tmp_path / "EgoMotion.fmu")


@pytest.fixture
def recording(tmp_path: Path) -> Path:
    out = tmp_path / "recorded.mcap"
    convert(EXAMPLE / "mapping.json", EXAMPLE / "recorded.csv", out)
    return out


def bounded_feedthrough(tmp_path: Path) -> Path:
    """Feedthrough with a Binary input that declares maxSize 4."""
    bounded = tmp_path / "Bounded.fmu"
    with zipfile.ZipFile(FEEDTHROUGH) as source, \
            zipfile.ZipFile(bounded, "w") as target:
        for member in source.infolist():
            data = source.read(member)
            if member.filename == "modelDescription.xml":
                data = data.replace(
                    b'name="Binary_input" valueReference="31" '
                    b'causality="input"',
                    b'name="Binary_input" valueReference="31" '
                    b'causality="input" maxSize="4"',
                )
            target.writestr(member, data)
    return bounded


def write(tmp_path: Path, document: dict, name: str = "authoring.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(document))
    return path


def edited(edit) -> dict:
    document = copy.deepcopy(DOCUMENT)
    edit(document)
    return document


def rejection(tmp_path: Path, document: dict, fmu: Path,
              recording: Path) -> str:
    out = tmp_path / "manifest.json"
    with pytest.raises(AuthoringError) as raised:
        author(write(tmp_path, document), fmu, recording, out)
    assert not out.exists()
    return str(raised.value)


class TestManifest:
    def test_the_manifest_states_every_choice(self, tmp_path, fmu, recording):
        out = tmp_path / "manifest.json"
        author(EXAMPLE / "authoring.json", fmu, recording, out)
        manifest = json.loads(out.read_text())

        assert manifest["duration_ns"] == 1_000_000_000
        assert manifest["schemas"] == DOCUMENT["schemas"]
        assert manifest["channels"] == {
            "ego.accel": {"schema": "ego.Acceleration", "latency_ns": 0},
            "ego.motion": {"schema": "ego.Motion", "latency_ns": 10_000_000},
        }
        assert manifest["participants"]["replay"] == {
            "type": "replay",
            "recording": str(recording.resolve()),
            "recording_hash": manifest["participants"]["replay"]["recording_hash"],
            "channels": ["ego.accel"],
        }
        assert manifest["participants"]["fmu"] == {
            "type": "process",
            # The FMU path is an argument of its own, so the runner resolves
            # it against the Manifest and digests it into the provenance.
            "command": [
                "python3", "-m", "sil.fmi", str(fmu.resolve()),
                "--bind", "ego.accel:accel_mps2=acceleration",
                "--bind", "ego.motion:speed_mps=speed",
                "--bind", "ego.motion:position_m=position",
                "--start", "initial_speed=20",
                "--start", "initial_position=5",
            ],
            "step_period_ns": 10_000_000,
            "subscribes": [
                {"channel": "ego.accel", "capacity": 1, "overflow": "fail"},
            ],
            "publishes": ["ego.motion"],
            "priority": 0,
        }

    def test_the_manifest_is_canonical(self, tmp_path, fmu, recording):
        out = tmp_path / "manifest.json"
        receipt = author(EXAMPLE / "authoring.json", fmu, recording, out)
        text = out.read_text()
        assert text == json.dumps(json.loads(text), sort_keys=True,
                                  separators=(",", ":")) + "\n"
        assert receipt["manifest"]["sha256"] == (
            __import__("hashlib").sha256(text.encode()).hexdigest())

    def test_authoring_twice_writes_the_same_bytes(
        self, tmp_path, fmu, recording
    ):
        receipts = []
        for attempt in ("1", "2"):
            receipts.append(author(EXAMPLE / "authoring.json", fmu, recording,
                                   tmp_path / f"manifest-{attempt}.json"))
        assert (tmp_path / "manifest-1.json").read_bytes() == (
            tmp_path / "manifest-2.json").read_bytes()
        receipts[1]["manifest"]["file"] = receipts[0]["manifest"]["file"]
        assert receipts[0] == receipts[1]

    def test_the_receipt_records_every_unit(self, tmp_path, fmu, recording):
        receipt = author(EXAMPLE / "authoring.json", fmu, recording,
                         tmp_path / "manifest.json")
        assert receipt["bindings"] == [
            {"channel": "ego.accel", "field": "accel_mps2",
             "variable": "acceleration", "type": "Float64",
             "causality": "input", "unit": "m/s2"},
            {"channel": "ego.motion", "field": "speed_mps",
             "variable": "speed", "type": "Float64",
             "causality": "output", "unit": "m/s"},
            {"channel": "ego.motion", "field": "position_m",
             "variable": "position", "type": "Float64",
             "causality": "output", "unit": "m"},
        ]
        assert receipt["starts"] == [
            {"variable": "initial_speed", "value": "20", "type": "Float64",
             "causality": "parameter", "unit": "m/s"},
            {"variable": "initial_position", "value": "5", "type": "Float64",
             "causality": "parameter", "unit": "m"},
        ]
        assert receipt["held"] == []
        assert receipt["fmu"]["model_name"] == "EgoMotion"

    def test_a_held_input_keeps_its_declared_start(self, tmp_path, recording):
        held = [name for name in FEEDTHROUGH_INPUTS
                if name != "Float64_continuous_input"]
        document = {**FEEDTHROUGH_DOCUMENT, "hold": held}
        receipt = author(write(tmp_path, document), FEEDTHROUGH, recording,
                         tmp_path / "manifest.json")
        assert [entry["variable"] for entry in receipt["held"]] == held
        assert {"variable": "Binary_input", "start": "666f6f",
                "unit": None} in receipt["held"]
        # A held input is not on the command line: it keeps the start value
        # the FMU declares.
        command = json.loads((tmp_path / "manifest.json").read_text())[
            "participants"]["fmu"]["command"]
        assert "Binary_input" not in " ".join(command)

    def test_a_start_value_declares_an_input(self, tmp_path, recording):
        held = [name for name in FEEDTHROUGH_INPUTS
                if name not in ("Float64_continuous_input",
                                "Float64_discrete_input")]
        document = {
            **FEEDTHROUGH_DOCUMENT, "hold": held,
            "start": [{"variable": "Float64_discrete_input", "value": "2.5",
                       "unit": None}],
        }
        receipt = author(write(tmp_path, document), FEEDTHROUGH, recording,
                         tmp_path / "manifest.json")
        assert receipt["starts"][0]["causality"] == "input"


class TestRejection:
    def test_an_unknown_variable(self, tmp_path, fmu, recording):
        def rename(document):
            document["bind"][1]["variable"] = "velocity"

        assert "names FMU variable 'velocity', which FMU 'EgoMotion' does " \
            "not declare" in rejection(tmp_path, edited(rename), fmu, recording)

    def test_a_variable_of_the_other_direction(self, tmp_path, fmu, recording):
        def swap(document):
            document["bind"][1]["variable"] = "acceleration"
            document["bind"][1]["unit"] = "m/s2"

        message = rejection(tmp_path, edited(swap), fmu, recording)
        assert "whose causality is 'input'" in message
        assert "binds output variables" in message

    def test_a_field_of_the_wrong_type(self, tmp_path, fmu, recording):
        def narrow(document):
            document["schemas"]["ego.Motion"]["fields"][0]["type"] = "f32"

        assert "declares field 'speed_mps' as a 'f32' scalar" in rejection(
            tmp_path, edited(narrow), fmu, recording)

    def test_a_variable_of_an_unmapped_type(self, tmp_path, recording):
        document = {
            **DOCUMENT,
            "schemas": {"Out": {"fields": [{"name": "s", "type": "u8"}]},
                        **DOCUMENT["schemas"]},
            "channels": {
                "ego.accel": DOCUMENT["channels"]["ego.accel"],
                "out": {"schema": "Out", "direction": "out", "latency_ns": 0},
            },
            "bind": [
                {"channel": "ego.accel", "field": "accel_mps2",
                 "variable": "Float64_continuous_input", "unit": None},
                {"channel": "out", "field": "s", "variable": "String_output",
                 "unit": None},
            ],
            "start": [],
            "hold": [],
        }
        assert "which is a String variable" in rejection(
            tmp_path, document, FEEDTHROUGH, recording)

    def test_a_unit_that_differs_from_the_variable(
        self, tmp_path, fmu, recording
    ):
        def kmh(document):
            document["bind"][1]["unit"] = "km/h"

        message = rejection(tmp_path, edited(kmh), fmu, recording)
        assert "Channel 'ego.motion' field 'speed_mps' is stated in 'km/h'" \
            in message
        assert "FMU variable 'speed' is in 'm/s'" in message
        assert "convert it at the edge" in message

    def test_a_unit_on_a_variable_that_declares_none(
        self, tmp_path, recording
    ):
        document = copy.deepcopy(FEEDTHROUGH_DOCUMENT)
        document["bind"][0]["unit"] = "m/s2"
        assert "FMU variable 'Float64_continuous_input' declares no unit" in (
            rejection(tmp_path, document, FEEDTHROUGH, recording))

    def test_a_run_that_replays_nothing(self, tmp_path, fmu, recording):
        def output_only(document):
            document["bind"] = document["bind"][1:]
            document["hold"] = ["acceleration"]
            document["channels"].pop("ego.accel")
            document["schemas"].pop("ego.Acceleration")

        assert "declares no input-direction Channel" in rejection(
            tmp_path, edited(output_only), fmu, recording)

    def test_a_start_in_another_unit(self, tmp_path, fmu, recording):
        def kmh(document):
            document["start"][0]["unit"] = "km/h"

        assert "start value for FMU variable 'initial_speed' is stated in " \
            "'km/h'" in rejection(tmp_path, edited(kmh), fmu, recording)

    def test_a_start_that_does_not_parse(self, tmp_path, fmu, recording):
        def text(document):
            document["start"][0]["value"] = "twenty"

        assert "cannot read 'twenty' as Float64" in rejection(
            tmp_path, edited(text), fmu, recording)

    def test_a_start_of_an_output(self, tmp_path, fmu, recording):
        def output(document):
            document["start"].append(
                {"variable": "speed", "value": "1", "unit": "m/s"})

        assert "start value for FMU variable 'speed': its causality is " \
            "'output'" in rejection(tmp_path, edited(output), fmu, recording)

    def test_a_start_that_is_not_text(self, tmp_path, fmu, recording):
        def number(document):
            document["start"][0]["value"] = 20

        assert "start[0] 'value' must be a non-empty string" in rejection(
            tmp_path, edited(number), fmu, recording)

    def test_an_input_that_nothing_declares(self, tmp_path, fmu, recording):
        def unbound(document):
            document["bind"] = document["bind"][1:]
            document["channels"].pop("ego.accel")
            document["schemas"].pop("ego.Acceleration")

        message = rejection(tmp_path, edited(unbound), fmu, recording)
        assert "FMU input variable 'acceleration' is neither bound, started " \
            "nor held" in message

    def test_a_held_variable_that_is_no_input(self, tmp_path, fmu, recording):
        def hold(document):
            document["hold"] = ["initial_speed"]

        assert "holds FMU variable 'initial_speed', whose causality is " \
            "'parameter'" in rejection(tmp_path, edited(hold), fmu, recording)

    def test_a_held_input_that_is_bound(self, tmp_path, fmu, recording):
        def hold(document):
            document["hold"] = ["acceleration"]

        assert "holds FMU variable 'acceleration', which is also bound" in (
            rejection(tmp_path, edited(hold), fmu, recording))

    def test_a_binary_payload_above_the_declared_bound(
        self, tmp_path, recording
    ):
        bounded = bounded_feedthrough(tmp_path)
        document = {
            **DOCUMENT,
            "schemas": {
                "ego.Acceleration": DOCUMENT["schemas"]["ego.Acceleration"],
                "Frame": {"fields": [
                    {"name": "data", "type": "u8", "count": 8},
                    {"name": "data_length", "type": "u8"},
                ]},
            },
            "channels": {
                "ego.accel": DOCUMENT["channels"]["ego.accel"],
                "frame": {"schema": "Frame", "direction": "in",
                          "latency_ns": 0,
                          "route": {"capacity": 1, "overflow": "fail"}},
            },
            "bind": [
                {"channel": "ego.accel", "field": "accel_mps2",
                 "variable": "Float64_continuous_input", "unit": None},
                {"channel": "frame", "field": "data",
                 "variable": "Binary_input", "unit": None},
            ],
            "start": [],
        }
        assert "carries 8 bytes, but input variable 'Binary_input' declares " \
            "maxSize 4" in rejection(tmp_path, document, bounded, recording)

    def test_a_binary_start_above_the_declared_bound(
        self, tmp_path, recording
    ):
        bounded = bounded_feedthrough(tmp_path)
        held = [name for name in FEEDTHROUGH_INPUTS
                if name not in ("Float64_continuous_input", "Binary_input")]
        document = {
            **FEEDTHROUGH_DOCUMENT, "hold": held,
            "start": [{"variable": "Binary_input", "value": "0102030405",
                       "unit": None}],
        }
        assert "5 bytes, but input variable 'Binary_input' declares maxSize " \
            "4" in rejection(tmp_path, document, bounded, recording)

    def test_an_fmu_the_importer_cannot_drive(self, tmp_path, recording):
        fmi2 = tmp_path / "Fmi2.fmu"
        with zipfile.ZipFile(fmi2, "w") as archive:
            archive.writestr("modelDescription.xml",
                             '<fmiModelDescription fmiVersion="2.0"/>')
        assert "declares fmiVersion '2.0'" in rejection(
            tmp_path, DOCUMENT, fmi2, recording)

    @pytest.mark.parametrize("key", ["latency_ns", "route"])
    def test_an_input_channel_without_an_explicit_choice(
        self, tmp_path, fmu, recording, key
    ):
        def drop(document):
            document["channels"]["ego.accel"].pop(key)

        assert f"channel 'ego.accel' is missing key(s) '{key}'" in rejection(
            tmp_path, edited(drop), fmu, recording)

    def test_a_route_on_an_output_channel(self, tmp_path, fmu, recording):
        def route(document):
            document["channels"]["ego.motion"]["route"] = {
                "capacity": 1, "overflow": "fail"}

        assert "channel 'ego.motion' has unknown key(s) 'route'" in rejection(
            tmp_path, edited(route), fmu, recording)

    def test_an_unbounded_route(self, tmp_path, fmu, recording):
        def unbounded(document):
            document["channels"]["ego.accel"]["route"]["capacity"] = 0

        assert "capacity must be >= 1" in rejection(
            tmp_path, edited(unbounded), fmu, recording)

    def test_a_binding_without_a_unit(self, tmp_path, fmu, recording):
        def drop(document):
            document["bind"][0].pop("unit")

        assert "bind[0] is missing key(s) 'unit'" in rejection(
            tmp_path, edited(drop), fmu, recording)

    def test_no_binding(self, tmp_path, fmu, recording):
        def drop(document):
            document["bind"] = []

        assert "'bind' must be a non-empty array" in rejection(
            tmp_path, edited(drop), fmu, recording)

    def test_a_recording_without_an_input_channel(
        self, tmp_path, fmu, recording
    ):
        def rename(document):
            document["channels"]["ego.acceleration"] = (
                document["channels"].pop("ego.accel"))
            document["bind"][0]["channel"] = "ego.acceleration"

        assert "carries no Channel 'ego.acceleration'" in rejection(
            tmp_path, edited(rename), fmu, recording)

    def test_a_recording_of_another_schema(self, tmp_path, fmu):
        mapping = json.loads((EXAMPLE / "mapping.json").read_text())
        mapping["schemas"]["ego.Acceleration"]["fields"][0]["type"] = "f32"
        (tmp_path / "mapping.json").write_text(json.dumps(mapping))
        narrow = tmp_path / "narrow.mcap"
        convert(tmp_path / "mapping.json", EXAMPLE / "recorded.csv", narrow)

        assert "Channel 'ego.accel' is recorded with schema" in rejection(
            tmp_path, DOCUMENT, fmu, narrow)

    @pytest.mark.parametrize("key", ["step_period_ns", "duration_ns"])
    def test_a_period_or_duration_of_zero(self, tmp_path, fmu, recording, key):
        def zero(document):
            document[key] = 0

        assert "must be >= 1, got 0" in rejection(
            tmp_path, edited(zero), fmu, recording)

    def test_a_document_of_another_version(self, tmp_path, fmu, recording):
        def version(document):
            document["sil_fmu_replay"] = 2

        assert "'sil_fmu_replay' must be 1, got 2" in rejection(
            tmp_path, edited(version), fmu, recording)

    def test_a_duplicate_key(self, tmp_path, fmu, recording):
        path = tmp_path / "authoring.json"
        path.write_text('{"sil_fmu_replay": 1, "sil_fmu_replay": 1}')
        with pytest.raises(AuthoringError, match="duplicate key"):
            author(path, fmu, recording, tmp_path / "manifest.json")


class TestPaths:
    """The Manifest never replaces a file the Run reads, or the receipt."""

    @pytest.mark.parametrize("role", ["document", "fmu", "recording"])
    def test_the_manifest_does_not_replace_an_input(
        self, tmp_path, fmu, recording, role
    ):
        document = write(tmp_path, DOCUMENT)
        inputs = {"document": document, "fmu": fmu, "recording": recording}
        before = inputs[role].read_bytes()
        with pytest.raises(AuthoringError, match=f"is the {role}"):
            author(document, fmu, recording, inputs[role])
        assert inputs[role].read_bytes() == before

    @pytest.mark.parametrize("role", ["manifest", "recording"])
    def test_the_receipt_does_not_replace_the_manifest_or_an_input(
        self, tmp_path, fmu, recording, role, capsys
    ):
        out = tmp_path / "manifest.json"
        target = {"manifest": out, "recording": recording}[role]
        before = recording.read_bytes()
        code = main([str(EXAMPLE / "authoring.json"), str(fmu),
                     "--recording", str(recording), "-o", str(out),
                     "--receipt", str(target)])
        assert code == 2
        assert f"is the {role}" in capsys.readouterr().err
        assert not out.exists()
        assert recording.read_bytes() == before

    def test_a_path_is_compared_after_it_is_resolved(
        self, tmp_path, fmu, recording, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(AuthoringError, match="is the recording"):
            author(EXAMPLE / "authoring.json", fmu, recording,
                   Path("sub") / ".." / recording.name)


class TestCommand:
    def test_it_writes_the_manifest_and_the_receipt(
        self, tmp_path, fmu, recording
    ):
        code = main([str(EXAMPLE / "authoring.json"), str(fmu),
                     "--recording", str(recording),
                     "-o", str(tmp_path / "manifest.json"),
                     "--receipt", str(tmp_path / "receipt.json")])
        assert code == 0
        receipt = json.loads((tmp_path / "receipt.json").read_text())
        assert receipt["sil_fmu_replay_receipt"] == 1
        assert (tmp_path / "manifest.json").is_file()

    def test_a_rejection_exits_2_and_names_the_reason(
        self, tmp_path, fmu, recording, capsys
    ):
        def kmh(document):
            document["bind"][1]["unit"] = "km/h"

        code = main([str(write(tmp_path, edited(kmh))), str(fmu),
                     "--recording", str(recording),
                     "-o", str(tmp_path / "manifest.json")])
        assert code == 2
        assert capsys.readouterr().err.startswith("sil fmi replay: error: ")
        assert not (tmp_path / "manifest.json").exists()
