"""OSMP binary variables at the FMI 2.0 Importer edge (issue #244).

OSI Sensor Model Packaging passes a serialized OSI message between FMI 2.0
FMUs as three fmi2Integer variables: the address of the bytes in two 32-bit
halves (`base.lo`, `base.hi`) and their count (`size`). A Channel carries the
bytes themselves, bounded like a Binary variable: a `u8` array and the length
beside it. The Importer hands an input as an address into its own memory and
copies an output out of the FMU's memory once, right after the step. It moves
bytes only and never decodes OSI.

The fake OSMP FMU of `tests/osmp_fixture.py` is built for the host, so every
check runs on every host the suite runs on. Running `OSMPDummySource` into
`OSMPDummySensor` is the proof's (`proofs/osmp-importer/`), because the OSI
FMUs need Protobuf and Linux x86-64.
"""

from __future__ import annotations

import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest
from conftest import ROOT
from osmp_fixture import MIME_TYPE, annotation, osmp_fmu, variable
from sil.fmi import FmuParticipant, ModelDescription
from sil.fmi.description import fmi2_platform_directory, library_suffix
from sil.fmi.inspection import inspect
from sil.manifest import Manifest, SubscriberRoute
from sil.participant import Input, ManifestError, ParticipantFailure
from sil.testing import run_simulation

STEP_PERIOD_NS = 10_000_000
CAPACITY = 16

SCHEMAS = {
    "osmp.Payload": {"fields": [
        {"name": "payload", "type": "u8", "count": CAPACITY},
        {"name": "payload_length", "type": "u32"},
    ]},
}
BINDS = ["osmp.In:payload=OSMPIn", "osmp.Out:payload=OSMPOut"]
INIT = {
    "op": "init", "name": "osmp", "schemas": SCHEMAS,
    "channels": {
        "osmp.In": {"schema": "osmp.Payload", "direction": "in"},
        "osmp.Out": {"schema": "osmp.Payload", "direction": "out"},
    },
}


@pytest.fixture
def fake(build_dir, tmp_path):
    """The archive of one build of the fake OSMP FMU, by its variant name."""

    def build(variant: str = "Ok", rewrite=lambda text: text) -> Path:
        binary = build_dir / f"Osmp{variant}{library_suffix()}"
        assert binary.exists(), f"fake OSMP binary was not built at {binary}"
        return osmp_fmu(
            tmp_path / f"Osmp{variant}.fmu", binary,
            fmi2_platform_directory(), rewrite,
        )

    return build


def read(archive: Path, tmp_path: Path) -> ModelDescription:
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(archive) as opened:
        opened.extractall(extracted)
    return ModelDescription.read(extracted)


def replace(old: str, new: str):
    """A description rewrite that must change the text."""

    def rewrite(text: str) -> str:
        assert old in text, old
        return text.replace(old, new, 1)

    return rewrite


def message(data: bytes, capacity: int = CAPACITY) -> dict:
    return {"payload": data.ljust(capacity, b"\0"),
            "payload_length": len(data)}


def with_unannotated(*names: str):
    """A description rewrite that adds unannotated input Integers."""
    declared = "\n".join(
        variable(name, 9 + index, "input") for index, name in enumerate(names)
    )
    return replace("  </ModelVariables>", f"{declared}\n  </ModelVariables>")


INT_INIT = {**INIT, "schemas": {
    "osmp.Int": {"fields": [{"name": "v", "type": "i32"}]}},
    "channels": {"osmp.In": {"schema": "osmp.Int", "direction": "in"}}}


def participant(archive: Path, *, init=INIT, binds=BINDS, starts=()):
    imported = FmuParticipant(archive, binds=binds, starts=starts)
    try:
        imported.on_init(init)
    except BaseException:
        imported.close()
        raise
    return imported


class TestDescription:
    """The OSMP annotations, read into one Binary variable per triple."""

    def test_a_triple_is_one_binary_variable(self, fake, tmp_path):
        description = read(fake(), tmp_path)

        binary = description.variables["OSMPIn"]
        assert binary.kind == "Binary"
        assert binary.causality == "input"
        assert binary.mime_type == MIME_TYPE
        assert (binary.osmp.lo, binary.osmp.hi, binary.osmp.size) == (0, 1, 2)
        assert description.variables["OSMPOut"].causality == "output"

    def test_the_binary_variable_has_no_reference_of_its_own(
            self, fake, tmp_path):
        description = read(fake(), tmp_path)

        assert description.variables["OSMPIn"].reference is None
        assert description.variables["OSMPIn.base.lo"].reference == 0

    def test_an_unannotated_address_and_its_size_are_marked(
            self, fake, tmp_path):
        rewrite = with_unannotated("Other.base.hi", "Other.size", "Count.size")
        variables = read(fake(rewrite=rewrite), tmp_path).variables

        assert variables["Other.base.hi"].osmp_unannotated == "Other"
        assert variables["Other.size"].osmp_unannotated == "Other"
        assert variables["Count.size"].osmp_unannotated is None
        assert variables["OSMPIn.base.lo"].osmp_unannotated is None

    def test_each_member_names_its_binary_variable(self, fake, tmp_path):
        description = read(fake(), tmp_path)

        assert description.variables["OSMPIn.base.hi"].osmp_member == "OSMPIn"
        assert description.variables["OSMPOut.size"].osmp_member == "OSMPOut"

    def test_an_incomplete_triple_is_refused(self, fake, tmp_path):
        rewrite = replace(annotation("OSMPIn", "size"), "")
        with pytest.raises(ManifestError) as refused:
            read(fake(rewrite=rewrite), tmp_path)

        assert "'OSMPIn'" in str(refused.value)
        assert "'size'" in str(refused.value)

    def test_an_unknown_role_is_refused(self, fake, tmp_path):
        rewrite = replace(annotation("OSMPIn", "size"),
                          annotation("OSMPIn", "length"))
        with pytest.raises(ManifestError, match="role 'length'"):
            read(fake(rewrite=rewrite), tmp_path)

    def test_a_role_declared_twice_is_refused(self, fake, tmp_path):
        rewrite = replace(annotation("OSMPIn", "size"),
                          annotation("OSMPIn", "base.lo"))
        with pytest.raises(ManifestError, match="'base.lo' twice"):
            read(fake(rewrite=rewrite), tmp_path)

    def test_members_of_two_causalities_are_refused(self, fake, tmp_path):
        rewrite = replace(
            'name="OSMPIn.size" valueReference="2" causality="input"',
            'name="OSMPIn.size" valueReference="2" causality="output"',
        )
        with pytest.raises(ManifestError, match="causality"):
            read(fake(rewrite=rewrite), tmp_path)

    def test_members_of_two_mime_types_are_refused(self, fake, tmp_path):
        rewrite = replace(annotation("OSMPIn", "size"),
                          annotation("OSMPIn", "size", "application/other"))
        with pytest.raises(ManifestError, match="mime-type"):
            read(fake(rewrite=rewrite), tmp_path)

    def test_a_member_not_named_for_its_role_is_refused(self, fake, tmp_path):
        rewrite = replace('name="OSMPIn.size"', 'name="OSMPIn.length"')
        with pytest.raises(ManifestError, match="'OSMPIn.size'"):
            read(fake(rewrite=rewrite), tmp_path)

    def test_a_member_of_another_type_is_refused(self, fake, tmp_path):
        rewrite = replace(
            'causality="input" variability="discrete">\n'
            '      <Integer start="0"/>',
            'causality="input" variability="discrete">\n'
            '      <Real start="0"/>',
        )
        with pytest.raises(ManifestError, match="Integer"):
            read(fake(rewrite=rewrite), tmp_path)

    def test_member_annotations_need_the_model_annotation(
        self, fake, tmp_path
    ):
        def drop_model_annotation(text: str) -> str:
            start = text.index("<VendorAnnotations>")
            end = text.index("</VendorAnnotations>") + len(
                "</VendorAnnotations>")
            return text[:start] + text[end:]

        with pytest.raises(ManifestError, match="VendorAnnotations"):
            read(fake(rewrite=drop_model_annotation), tmp_path)

    def test_a_binary_name_that_is_a_variable_is_refused(
        self, fake, tmp_path
    ):
        rewrite = replace(
            "  </ModelVariables>",
            variable("OSMPIn", 9, "input") + "\n  </ModelVariables>",
        )
        with pytest.raises(ManifestError, match="already"):
            read(fake(rewrite=rewrite), tmp_path)


class TestMapping:
    """A Channel binds the binary variable, never one of its integers."""

    @pytest.mark.parametrize("member", [
        "OSMPIn.base.lo", "OSMPIn.base.hi", "OSMPIn.size",
    ])
    def test_a_member_is_not_bound_on_its_own(self, fake, member):
        with pytest.raises(ManifestError) as refused:
            participant(fake(), init=INT_INIT, binds=[f"osmp.In:v={member}"])

        assert "OSMP binary variable 'OSMPIn'" in str(refused.value)

    @pytest.mark.parametrize("name", ["Other.base.lo", "Other.size"])
    def test_an_unannotated_address_is_not_bound(self, fake, name):
        rewrite = with_unannotated("Other.base.lo", "Other.size")
        with pytest.raises(ManifestError, match="OSMP address 'Other'"):
            participant(fake(rewrite=rewrite), init=INT_INIT,
                        binds=[f"osmp.In:v={name}"])

    def test_an_unannotated_address_takes_no_start_value(self, fake):
        rewrite = with_unannotated("Other.base.lo")
        with pytest.raises(ManifestError, match="OSMP address 'Other'"):
            participant(fake(rewrite=rewrite), starts=["Other.base.lo=1"])

    def test_a_size_without_an_address_is_an_ordinary_integer(self, fake):
        participant(fake(rewrite=with_unannotated("Count.size")),
                    init=INT_INIT, binds=["osmp.In:v=Count.size"]).close()

    @pytest.mark.parametrize("start", ["OSMPIn=00", "OSMPIn.size=3"])
    def test_no_start_value_is_set(self, fake, start):
        with pytest.raises(ManifestError, match="OSMP"):
            participant(fake(), starts=[start])

    def test_the_payload_is_a_bounded_u8_array(self, fake):
        init = {**INIT, "schemas": {"osmp.Payload": {"fields": [
            {"name": "payload", "type": "i32"}]}}}
        with pytest.raises(ManifestError, match="bounded 'u8' array"):
            participant(fake(), init=init)

    def test_a_bound_above_the_fmi2_integer_range_is_refused(self, fake):
        capacity = 2**31
        init = {**INIT, "schemas": {"osmp.Payload": {"fields": [
            {"name": "payload", "type": "u8", "count": capacity},
            {"name": "payload_length", "type": "u32"}]}}}
        with pytest.raises(ManifestError, match="fmi2Integer"):
            participant(fake(), init=init)


class TestStep:
    """The bytes cross by address, in both directions."""

    def test_the_payload_crosses_the_step(self, fake):
        imported = participant(fake())
        try:
            published = imported.on_step(
                0, STEP_PERIOD_NS, [Input("osmp.In", 0, message(b"\x01\0\x03"))]
            )
        finally:
            imported.close()

        assert published == [("osmp.Out", message(b"\x03\0\x01"))]

    def test_the_input_stays_valid_until_the_next_message(self, fake):
        """A Step without a Message hands the FMU the last payload again."""
        imported = participant(fake())
        try:
            imported.on_step(0, STEP_PERIOD_NS,
                             [Input("osmp.In", 0, message(b"abc"))])
            published = imported.on_step(STEP_PERIOD_NS, STEP_PERIOD_NS, [])
        finally:
            imported.close()

        assert published == [("osmp.Out", message(b"cba"))]

    def test_an_empty_payload_is_an_empty_output(self, fake):
        imported = participant(fake())
        try:
            published = imported.on_step(0, STEP_PERIOD_NS, [])
        finally:
            imported.close()

        assert published == [("osmp.Out", message(b""))]

    def test_an_output_above_the_bound_fails_the_run(self, fake):
        """The input fits its Channel, the reversed output does not."""
        small = 4
        init = {**INIT, "schemas": {
            **SCHEMAS,
            "osmp.Small": {"fields": [
                {"name": "payload", "type": "u8", "count": small},
                {"name": "payload_length", "type": "u32"}]},
        }, "channels": {
            "osmp.In": {"schema": "osmp.Payload", "direction": "in"},
            "osmp.Out": {"schema": "osmp.Small", "direction": "out"},
        }}
        imported = participant(fake(), init=init)
        try:
            with pytest.raises(ParticipantFailure) as failed:
                imported.on_step(0, STEP_PERIOD_NS,
                                 [Input("osmp.In", 0, message(b"abcdef"))])
        finally:
            imported.close()

        assert "'OSMPOut'" in str(failed.value)
        assert "6 bytes" in str(failed.value)
        assert f"carries {small}" in str(failed.value)

    def test_an_oversized_size_fails_before_any_copy(self, fake):
        imported = participant(fake("Oversize"))
        try:
            with pytest.raises(ParticipantFailure) as failed:
                imported.on_step(0, STEP_PERIOD_NS, [])
        finally:
            imported.close()

        assert "2147483647 bytes" in str(failed.value)

    def test_a_negative_size_fails_the_run(self, fake):
        imported = participant(fake("NegativeSize"))
        try:
            with pytest.raises(ParticipantFailure, match="size -1"):
                imported.on_step(0, STEP_PERIOD_NS, [])
        finally:
            imported.close()

    def test_a_null_address_fails_the_run(self, fake):
        imported = participant(fake("NullAddress"))
        try:
            with pytest.raises(ParticipantFailure, match="address 0"):
                imported.on_step(0, STEP_PERIOD_NS,
                                 [Input("osmp.In", 0, message(b"abc"))])
        finally:
            imported.close()


class TestInspection:
    """`sil fmi inspect` reports the OSMP variables and checks a mapping."""

    def test_the_binary_variables_and_members_are_reported(self, fake):
        report = inspect(fake())

        assert report["osmp"] == [
            {"name": "OSMPIn", "causality": "input", "mime_type": MIME_TYPE,
             "value_references": {"base.lo": 0, "base.hi": 1, "size": 2}},
            {"name": "OSMPOut", "causality": "output", "mime_type": MIME_TYPE,
             "value_references": {"base.lo": 3, "base.hi": 4, "size": 5}},
        ]
        variables = {v["name"]: v for v in report["variables"]}
        assert "OSMP binary variable 'OSMPIn'" in (
            variables["OSMPIn.base.lo"]["unmappable"])

    def test_an_unannotated_address_is_reported_unmappable(self, fake):
        rewrite = with_unannotated("Other.base.lo", "Other.size")
        report = inspect(fake(rewrite=rewrite))

        variables = {v["name"]: v for v in report["variables"]}
        for name in ("Other.base.lo", "Other.size"):
            assert "OSMP address 'Other'" in variables[name]["unmappable"]

    def test_a_mapping_of_the_binary_variables_is_accepted(self, fake):
        mapping = {"sil_fmi_mapping": 1, "schemas": SCHEMAS,
                   "channels": INIT["channels"], "bind": BINDS}
        report = inspect(fake(), mapping)

        assert report["mapping"]["accepted"], report["mapping"]
        assert report["mapping"]["unbound"] == []

    def test_an_unbound_binary_variable_is_listed_by_its_name(self, fake):
        mapping = {"sil_fmi_mapping": 1, "schemas": SCHEMAS,
                   "channels": {"osmp.Out": INIT["channels"]["osmp.Out"]},
                   "bind": [BINDS[1]]}
        report = inspect(fake(), mapping)

        assert report["mapping"]["unbound"] == ["OSMPIn"]


STIMULUS = ROOT / "tests" / "participants" / "binary_stimulus.py"


def _payload():
    spec = importlib.util.spec_from_file_location("binary_stimulus", STIMULUS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.payload


def run_manifest(archive: Path, duration_ns: int = 100_000_000) -> Manifest:
    """A binary stimulus feeding the OSMP FMU, which publishes it reversed."""
    m = Manifest(duration_ns=duration_ns)
    m.add_schemas(SCHEMAS)
    m.add_channel("osmp.In", schema="osmp.Payload")
    m.add_channel("osmp.Out", schema="osmp.Payload")
    m.add_process(
        "stimulus",
        command=[sys.executable, str(STIMULUS), "osmp.In", "payload",
                 str(CAPACITY), "0"],
        step_period_ns=STEP_PERIOD_NS, publishes=["osmp.In"],
    )
    m.add_process(
        "osmp",
        command=[sys.executable, "-m", "sil.fmi", str(archive),
                 *(argument for bind in BINDS for argument in ("--bind", bind))],
        step_period_ns=STEP_PERIOD_NS,
        subscribes=[SubscriberRoute("osmp.In", capacity=2)],
        publishes=["osmp.Out"], priority=1,
    )
    return m


class TestRunBoundary:
    """The OSMP mapping in a complete Run."""

    def test_every_payload_comes_back_reversed(self, fake, sil_run, tmp_path):
        result = run_simulation(
            run_manifest(fake()), runner=sil_run, workdir=tmp_path
        )
        payload = _payload()
        outputs = [bytes(f["payload"][:f["payload_length"]])
                   for _, f in result.messages("osmp.Out")]

        # The FMU reads each payload one Step after it is published.
        assert outputs == [b""] + [
            payload(step, CAPACITY)[::-1] for step in range(len(outputs) - 1)
        ]

    def test_a_wrong_size_fails_the_run_and_names_it(
        self, fake, run_sil, tmp_path
    ):
        proc = run_sil(
            run_manifest(fake("Oversize")).write(tmp_path / "m.json").path
        )

        assert proc.returncode == 1
        assert "participant 'osmp' failed" in proc.stderr
        assert "'OSMPOut'" in proc.stderr
        assert "2147483647 bytes" in proc.stderr
