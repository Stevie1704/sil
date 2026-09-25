"""`sil-fmi-inspect`: what an archive offers the importer, read statically.

The inspection states a verdict the importer would also reach, so every
rejection here is compared with the one `FmuParticipant.on_init` raises for the
same archive and the same proposed mapping. None of these archives is loaded:
an inspection that loaded the binary could not tell a broken binary from an
archive it cannot drive.
"""

from __future__ import annotations

import ctypes
import json
import re
import zipfile
from pathlib import Path

import pytest
from can_fixture import CAN_PROFILE, bus_fmu, node_fmu
from conftest import ROOT
from sil.fmi import FmuParticipant, library_suffix, platform_directory
from sil.fmi.inspection import (
    EXIT_MAPPING_REJECTED,
    EXIT_UNUSABLE,
    EXIT_USAGE,
    inspect,
    main,
)
from sil.participant import ManifestError

FIXTURES = ROOT / "tests" / "fixtures" / "reference-fmus" / "3.0"
FEEDTHROUGH = FIXTURES / "Feedthrough.fmu"

FEEDTHROUGH_SCHEMAS = {
    "fmu.In": {"fields": [
        {"name": "Float64_continuous_input", "type": "f64"},
        {"name": "Float64_discrete_input", "type": "f64"},
    ]},
    "fmu.Out": {"fields": [
        {"name": "Float64_continuous_output", "type": "f64"},
        {"name": "Float64_discrete_output", "type": "f64"},
    ]},
}
FEEDTHROUGH_MAPPING = {
    "sil_fmi_mapping": 1,
    "schemas": FEEDTHROUGH_SCHEMAS,
    "channels": {
        "fmu.In": {"schema": "fmu.In", "direction": "in"},
        "fmu.Out": {"schema": "fmu.Out", "direction": "out"},
    },
}


class BinaryLoaded(AssertionError):
    """Raised where a binary would have been loaded."""


def refuse_to_load(*args, **kwargs):
    raise BinaryLoaded("an FMU binary was loaded")


@pytest.fixture(autouse=True)
def no_binary_is_loaded(monkeypatch):
    """Fail any test whose inspection reaches for a shared library."""
    monkeypatch.setattr(ctypes, "CDLL", refuse_to_load)


def init_line(mapping: dict) -> dict:
    """The initialization line the kernel would send for a proposed mapping."""
    return {
        "op": "init", "name": "fmu",
        "schemas": mapping["schemas"], "channels": mapping["channels"],
    }


def runtime_rejection(archive: Path, mapping: dict, monkeypatch,
                      tmp_path: Path) -> str:
    """What `FmuParticipant.on_init` rejects this archive and mapping with."""
    monkeypatch.chdir(tmp_path)
    participant = FmuParticipant(
        archive, binds=mapping.get("bind", []), starts=mapping.get("start", [])
    )
    try:
        with pytest.raises(ManifestError) as raised:
            participant.on_init(init_line(mapping))
    finally:
        participant.close()
    # The report names a path into the archive where the runtime names one
    # into its own extraction, which is gone by the time anyone reads it.
    return re.sub(r"[^'\s]*/sil-fmu-[^/'\s]+", str(archive), str(raised.value))


def rewritten(tmp_path: Path, name: str, rewrite=None, *,
              drop=lambda member: False) -> Path:
    """Feedthrough, with its description rewritten or members left out."""
    path = tmp_path / name
    with zipfile.ZipFile(FEEDTHROUGH) as source, \
            zipfile.ZipFile(path, "w") as target:
        for member in source.infolist():
            if drop(member.filename):
                continue
            data = source.read(member)
            if member.filename == "modelDescription.xml" and rewrite:
                data = rewrite(data.decode()).encode()
            target.writestr(member, data)
    return path


def with_array_input(text: str) -> str:
    return text.replace(
        "</ModelVariables>",
        '<Float64 name="Float64_array_input" valueReference="99" '
        'causality="input" start="0 0 0"><Dimension start="3"/></Float64>'
        "</ModelVariables>",
    )


def variables(report: dict) -> dict[str, dict]:
    return {variable["name"]: variable for variable in report["variables"]}


@pytest.fixture(scope="module")
def report():
    """Feedthrough's report, read once for every assertion about it."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ctypes, "CDLL", refuse_to_load)
        return inspect(FEEDTHROUGH)


class TestTheArchiveFacts:
    """What the report states about a supported archive."""

    def test_the_verdict(self, report):
        assert report["sil_fmi_inspection"] == 1
        assert report["verdict"] == "compatible"
        assert report["unusable"] == []
        assert report["mapping"] is None

    def test_the_fmi_version_and_interfaces(self, report):
        facts = report["facts"]
        assert facts["fmi_version"] == "3.0"
        assert facts["model_name"] == "Feedthrough"
        assert facts["generation_tool"] == "Reference FMUs (v0.0.41)"
        assert set(facts["interfaces"]) == {"CoSimulation", "ModelExchange"}
        assert facts["interfaces"]["CoSimulation"]["hasEventMode"] == "true"

    def test_the_platform_binaries(self, report):
        assert report["platform"] == platform_directory()
        assert report["facts"]["platforms"] == [
            "aarch64-darwin", "aarch64-linux", "x86-windows", "x86_64-darwin",
            "x86_64-linux", "x86_64-windows",
        ]

    def test_each_variable_and_its_declaration(self, report):
        declared = variables(report)
        assert declared["Float64_continuous_input"] == {
            "name": "Float64_continuous_input", "type": "Float64",
            "value_reference": 7, "causality": "input",
            "variability": None, "start": "0", "unit": None,
            "declared_type": None, "dimensions": [], "max_size": None,
            "mime_type": None, "clocks": [], "interval_variability": None,
            "unmappable": None,
        }
        assert declared["Binary_input"]["start"] == "666f6f"
        assert declared["Enumeration_input"]["declared_type"] == "Option"

    def test_unused_unsupported_types_do_not_make_it_unusable(self, report):
        """Feedthrough declares every type there is and runs all the same:
        a variable no Channel names is never touched."""
        declared = variables(report)
        assert declared["Int32_input"]["unmappable"] == (
            "which is a Int32 variable; this importer maps Binary and the "
            "scalar types Float64, Boolean"
        )
        assert declared["Boolean_input"]["unmappable"] is None
        assert declared["Binary_output"]["unmappable"] is None

    def test_what_is_not_verified_statically(self, report):
        unverified = "\n".join(report["unverified"])
        assert f"binaries/{platform_directory()}/Feedthrough" in unverified
        assert "loads" in unverified


class TestArraysAndUnits:

    def test_an_array_is_reported_as_unmappable(self, tmp_path):
        report = inspect(rewritten(tmp_path, "array.fmu", with_array_input))
        array = variables(report)["Float64_array_input"]
        assert array["dimensions"] == [{"start": 3}]
        assert array["unmappable"] == (
            "which declares dimensions of 3 values; this importer maps "
            "variables of one value"
        )
        assert report["verdict"] == "compatible"

    def test_a_unit_from_the_declared_type(self, tmp_path):
        def typed(text: str) -> str:
            return text.replace(
                "<TypeDefinitions>",
                '<TypeDefinitions><Float64Type name="Speed" unit="m/s"/>',
            ).replace(
                '<Float64 name="Float64_continuous_input" ',
                '<Float64 name="Float64_continuous_input" declaredType="Speed" ',
            ).replace(
                '<Float64 name="Float64_discrete_input" ',
                '<Float64 name="Float64_discrete_input" unit="rad" ',
            )
        declared = variables(inspect(rewritten(tmp_path, "unit.fmu", typed)))
        assert declared["Float64_continuous_input"]["unit"] == "m/s"
        assert declared["Float64_discrete_input"]["unit"] == "rad"


def _not_a_zip(tmp_path: Path) -> Path:
    path = tmp_path / "not-a-zip.fmu"
    path.write_text("this is not a zip archive")
    return path


UNUSABLE = {
    "unreadable archive": _not_a_zip,
    "absent archive": lambda tmp_path: tmp_path / "absent.fmu",
    "no description": lambda tmp_path: rewritten(
        tmp_path, "bare.fmu", drop=lambda m: m == "modelDescription.xml"
    ),
    "unparsable description": lambda tmp_path: rewritten(
        tmp_path, "broken.fmu", lambda text: text[: len(text) // 2]
    ),
    "FMI 2.0": lambda tmp_path: rewritten(
        tmp_path, "fmi2.fmu",
        lambda text: text.replace('fmiVersion="3.0"', 'fmiVersion="2.0"'),
    ),
    "Model Exchange only": lambda tmp_path: rewritten(
        tmp_path, "me.fmu",
        lambda text: re.sub(r"<CoSimulation[^>]*/>", "", text),
    ),
    "Scheduled Execution only": lambda tmp_path: rewritten(
        tmp_path, "se.fmu",
        lambda text: re.sub(r"<(ModelExchange|CoSimulation)[^>]*/>", "",
                            text).replace(
            "<TypeDefinitions>",
            '<ScheduledExecution modelIdentifier="Feedthrough"/>'
            "<TypeDefinitions>",
        ),
    ),
    "no binary for this platform": lambda tmp_path: rewritten(
        tmp_path, "nobinary.fmu",
        drop=lambda m: m.startswith(f"binaries/{platform_directory()}/"),
    ),
}


class TestUnusableArchives:
    """Each archive the importer refuses, refused for the same reason."""

    @pytest.mark.parametrize("case", UNUSABLE)
    def test_the_report_agrees_with_the_runtime(
        self, case, tmp_path, monkeypatch
    ):
        archive = UNUSABLE[case](tmp_path)
        report = inspect(archive)
        assert report["verdict"] == "unusable"
        expected = runtime_rejection(
            archive, FEEDTHROUGH_MAPPING, monkeypatch, tmp_path
        )
        assert report["unusable"] == [expected]

    def test_facts_survive_an_unsupported_version(self, tmp_path):
        report = inspect(UNUSABLE["FMI 2.0"](tmp_path))
        assert report["facts"]["fmi_version"] == "2.0"
        assert report["variables"] == []

    @pytest.mark.parametrize("case, interface", [
        ("Model Exchange only", "ModelExchange"),
        ("Scheduled Execution only", "ScheduledExecution"),
    ])
    def test_the_interfaces_an_unsupported_archive_declares(
        self, tmp_path, case, interface
    ):
        report = inspect(UNUSABLE[case](tmp_path))
        assert list(report["facts"]["interfaces"]) == [interface]

    @pytest.mark.parametrize("rewrite", [
        lambda text: text.replace('valueReference="7"', 'valueReference="x"'),
        lambda text: re.sub(r"<ModelVariables>.*</ModelVariables>", "",
                            text, flags=re.DOTALL),
        lambda text: with_array_input(text).replace(
            '<Dimension start="3"/>', '<Dimension start="three"/>'
        ),
    ], ids=["valueReference", "no ModelVariables", "Dimension"])
    def test_a_malformed_description_is_unusable(self, tmp_path, rewrite):
        """A description the reader cannot make sense of is a verdict, not a
        traceback: the exit code has to keep meaning what it documents."""
        report = inspect(rewritten(tmp_path, "malformed.fmu", rewrite))
        assert report["verdict"] == "unusable"
        [reason] = report["unusable"]
        assert reason.startswith("FMU declares a malformed modelDescription.xml")


def feedthrough_mapping(**changes) -> dict:
    mapping = json.loads(json.dumps(FEEDTHROUGH_MAPPING))
    mapping.update(changes)
    return mapping


BOUND_SCHEMAS = {
    "fmu.In": {"fields": [{"name": "value", "type": "i32"}]},
}
REJECTED_MAPPINGS = {
    "unknown variable": feedthrough_mapping(
        schemas={"fmu.In": {"fields": [{"name": "nope", "type": "f64"}]}},
        channels={"fmu.In": {"schema": "fmu.In", "direction": "in"}},
    ),
    "integer binding": feedthrough_mapping(
        schemas=BOUND_SCHEMAS,
        channels={"fmu.In": {"schema": "fmu.In", "direction": "in"}},
        bind=["fmu.In:value=Int32_input"],
    ),
    "type mismatch": feedthrough_mapping(
        schemas={"fmu.In": {"fields": [{"name": "value", "type": "f32"}]}},
        channels={"fmu.In": {"schema": "fmu.In", "direction": "in"}},
        bind=["fmu.In:value=Float64_continuous_input"],
    ),
    "causality mismatch": feedthrough_mapping(
        schemas={"fmu.In": {"fields": [{"name": "value", "type": "f64"}]}},
        channels={"fmu.In": {"schema": "fmu.In", "direction": "in"}},
        bind=["fmu.In:value=Float64_continuous_output"],
    ),
    "unbound field": feedthrough_mapping(
        bind=["fmu.In:Float64_continuous_input=Float64_continuous_input"],
    ),
    "bad start value": feedthrough_mapping(
        start=["Float64_fixed_parameter=fast"],
    ),
}


class TestProposedMappings:
    """A proposed mapping is checked by the checks initialization runs."""

    def test_an_accepted_mapping(self, tmp_path, monkeypatch):
        report = inspect(FEEDTHROUGH, FEEDTHROUGH_MAPPING)
        assert report["verdict"] == "compatible"
        assert report["mapping"]["accepted"] is True
        assert report["mapping"]["rejection"] is None
        # The runtime accepts it too: the first thing it cannot do without
        # the binary is load it.
        monkeypatch.chdir(tmp_path)
        participant = FmuParticipant(FEEDTHROUGH)
        try:
            with pytest.raises(BinaryLoaded):
                participant.on_init(init_line(FEEDTHROUGH_MAPPING))
        finally:
            participant.close()

    @pytest.mark.parametrize("case", REJECTED_MAPPINGS)
    def test_a_rejection_agrees_with_the_runtime(
        self, case, tmp_path, monkeypatch
    ):
        mapping = REJECTED_MAPPINGS[case]
        report = inspect(FEEDTHROUGH, mapping)
        assert report["verdict"] == "mapping-rejected"
        assert report["mapping"]["accepted"] is False
        assert report["mapping"]["rejection"] == runtime_rejection(
            FEEDTHROUGH, mapping, monkeypatch, tmp_path
        )

    def test_the_variables_a_mapping_leaves_unbound(self):
        """An unbound input keeps its start value; an unbound output is not
        published. Neither is a mistake, but a reader authoring a Run wants
        to see them."""
        mapping = feedthrough_mapping(start=["Float64_fixed_parameter=2"])
        unbound = inspect(FEEDTHROUGH, mapping)["mapping"]["unbound"]
        assert "Float64_continuous_input" not in unbound
        assert "Float64_fixed_parameter" not in unbound
        assert "time" not in unbound
        assert "Int32_input" in unbound
        assert "Float64_tunable_parameter" in unbound
        assert "Binary_output" in unbound

    def test_declared_bindings_name_what_is_bound(self):
        mapping = feedthrough_mapping(
            schemas={"fmu.In": {"fields": [{"name": "v", "type": "f64"}]}},
            channels={"fmu.In": {"schema": "fmu.In", "direction": "in"}},
            bind=["fmu.In:v=Float64_discrete_input"],
        )
        report = inspect(FEEDTHROUGH, mapping)
        assert report["mapping"]["accepted"] is True
        assert "Float64_discrete_input" not in report["mapping"]["unbound"]
        assert "Float64_continuous_input" in report["mapping"]["unbound"]

    def test_an_array_binding(self, tmp_path, monkeypatch):
        archive = rewritten(tmp_path, "array.fmu", with_array_input)
        mapping = feedthrough_mapping(
            schemas={"fmu.In": {"fields": [
                {"name": "value", "type": "f64", "count": 3}
            ]}},
            channels={"fmu.In": {"schema": "fmu.In", "direction": "in"}},
            bind=["fmu.In:value=Float64_array_input"],
        )
        report = inspect(archive, mapping)
        assert report["mapping"]["rejection"] == runtime_rejection(
            archive, mapping, monkeypatch, tmp_path
        )
        assert "dimensions of 3 values" in report["mapping"]["rejection"]

    def test_an_unusable_archive_is_not_mapped(self, tmp_path):
        report = inspect(UNUSABLE["FMI 2.0"](tmp_path), FEEDTHROUGH_MAPPING)
        assert report["verdict"] == "unusable"
        assert report["mapping"] is None


# The stand-in binary is never loaded, so any bytes will do: they only have to
# be where the importer looks for them.
def can_archive(tmp_path: Path, package, name: str, **rewrites) -> Path:
    identifier = f"Clocked{name}"
    binary = tmp_path / f"{identifier}{library_suffix()}"
    binary.write_bytes(b"not a shared library")
    return package(
        tmp_path / f"{name}.fmu", model_identifier=identifier, binary=binary,
        platform_directory=platform_directory(), **rewrites,
    )


def periodic_tx_clock(text: str) -> str:
    """The node's send Clock, redeclared as a periodic output Clock."""
    declared = 'intervalVariability="triggered" name="CanChannel.Tx_Clock"'
    assert declared in text
    return text.replace(
        declared, 'intervalVariability="constant" name="CanChannel.Tx_Clock"'
    )


class TestClocksAndTerminals:
    """The BUS-group profile, read from the terminals an archive declares."""

    def test_a_node_terminal_the_group_drives(self, tmp_path):
        report = inspect(can_archive(tmp_path, node_fmu, "CanNode"))
        assert report["verdict"] == "compatible"
        assert report["bus"] == {
            "version": "1.0.0-beta.1", "bus_simulation": False
        }
        [terminal] = report["terminals"]
        assert terminal["name"] == "CanChannel"
        assert terminal["bus_profile"] == CAN_PROFILE
        assert terminal["unsupported"] is None
        clocked = variables(report)["CanChannel.Tx_Data"]
        assert clocked["clocks"] == ["CanChannel.Tx_Clock"]
        assert clocked["unmappable"] is None

    def test_both_bus_terminals(self, tmp_path):
        report = inspect(can_archive(tmp_path, bus_fmu, "CanBus"))
        assert report["bus"]["bus_simulation"] is True
        assert [(t["name"], t["unsupported"]) for t in report["terminals"]] == [
            ("Node1", None), ("Node2", None)
        ]

    def test_an_incompatible_clock(self, tmp_path, monkeypatch):
        archive = can_archive(
            tmp_path, node_fmu, "CanNode", rewrite=periodic_tx_clock
        )
        report = inspect(archive)
        assert report["verdict"] == "compatible"
        reason = variables(report)["CanChannel.Tx_Data"]["unmappable"]
        assert "of intervalVariability 'constant'" in reason
        [terminal] = report["terminals"]
        assert "member 'Tx_Clock'" in terminal["unsupported"]
        assert "intervalVariability 'constant'" in terminal["unsupported"]

        mapping = {
            "sil_fmi_mapping": 1,
            "schemas": {"can.Buffer": {"fields": [
                {"name": "data_length", "type": "u16"},
                {"name": "data", "type": "u8", "count": 2048},
                {"name": "data_event_time_ns", "type": "u64"},
            ]}},
            "channels": {"can.Tx": {"schema": "can.Buffer", "direction": "out"}},
            "bind": ["can.Tx:data=CanChannel.Tx_Data"],
        }
        rejected = inspect(archive, mapping)
        assert rejected["mapping"]["rejection"] == runtime_rejection(
            archive, mapping, monkeypatch, tmp_path
        )
        assert rejected["mapping"]["rejection"].endswith(reason)

    def test_an_fmu_without_the_layered_standard(self, tmp_path):
        report = inspect(can_archive(
            tmp_path, node_fmu, "CanNode", rewrite_manifest=lambda text: None
        ))
        [terminal] = report["terminals"]
        assert "carries no extra/org.fmi-standard.fmi-ls-bus" in (
            terminal["unsupported"]
        )


class TestTheCommand:
    """Exit codes and the two report formats."""

    def test_a_compatible_archive(self, capsys):
        assert main([str(FEEDTHROUGH), "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["verdict"] == "compatible"

    def test_the_readable_report(self, capsys):
        assert main([str(FEEDTHROUGH)]) == 0
        out = capsys.readouterr().out
        assert "verdict: compatible" in out
        assert "Int32_input" in out
        assert "not verified statically" in out

    def test_an_unusable_archive(self, tmp_path, capsys):
        archive = UNUSABLE["FMI 2.0"](tmp_path)
        assert main([str(archive)]) == EXIT_UNUSABLE
        assert "fmiVersion '2.0'" in capsys.readouterr().out

    def test_a_rejected_mapping(self, tmp_path, capsys):
        path = tmp_path / "mapping.json"
        path.write_text(json.dumps(REJECTED_MAPPINGS["integer binding"]))
        assert main([str(FEEDTHROUGH), "--mapping", str(path)]) == (
            EXIT_MAPPING_REJECTED
        )
        assert "Int32_input" in capsys.readouterr().out

    @pytest.mark.parametrize("document, message", [
        ("not json", "is not JSON"),
        ("[]", "is not a JSON object"),
        ('{"sil_fmi_mapping": 2}', "sil_fmi_mapping"),
        ('{"sil_fmi_mapping": 1, "schemas": {}, "channels": {}, "x": 1}',
         "unknown key 'x'"),
        ('{"sil_fmi_mapping": 1, "schemas": {"s": {"fields": []}}, '
         '"channels": {}}', "has no fields"),
        ('{"sil_fmi_mapping": 1, "schemas": {}, '
         '"channels": {"c": {"schema": "s", "direction": "in"}}}',
         "unknown schema 's'"),
        ('{"sil_fmi_mapping": 1, "schemas": {"s": {"fields": '
         '[{"name": "a", "type": "f64"}]}}, '
         '"channels": {"c": {"schema": "s", "direction": "up"}}}',
         "direction"),
        ('{"sil_fmi_mapping": 1, "schemas": {}, "channels": {}, '
         '"bind": "x"}', "'bind'"),
    ])
    def test_an_unreadable_mapping_document(
        self, tmp_path, capsys, document, message
    ):
        path = tmp_path / "mapping.json"
        path.write_text(document)
        assert main([str(FEEDTHROUGH), "--mapping", str(path)]) == EXIT_USAGE
        assert message in capsys.readouterr().err

    def test_an_absent_mapping_document(self, tmp_path, capsys):
        missing = tmp_path / "absent.json"
        assert main([str(FEEDTHROUGH), "--mapping", str(missing)]) == EXIT_USAGE
        assert "cannot read" in capsys.readouterr().err
