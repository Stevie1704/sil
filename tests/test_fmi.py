"""The FMI 3.0 co-simulation importer, at the run boundary and in-process.

The conformance target is the Modelica Association's own Reference FMUs,
vendored under `tests/fixtures/reference-fmus/`. `Feedthrough` sets every
output equal to its input, so a Run over it proves the whole path: the
subscribed Channel is written into the FMU's input variables, the FMU is
stepped, and its output variables are published on the Channel it publishes.
"""

import copy
import sys
import zipfile
from pathlib import Path

import pytest
from conftest import COMPAT_ROUTE_CAPACITY, ROOT
from sil.fmi import (
    NS_PER_S,
    CoSimulation,
    FmuParticipant,
    ModelDescription,
    platform_directory,
)
from sil.manifest import Manifest, SubscriberRoute
from sil.participant import ConfigurationError, Input
from sil.testing import run_simulation

FIXTURES = ROOT / "tests" / "fixtures" / "reference-fmus" / "3.0"
FEEDTHROUGH = FIXTURES / "Feedthrough.fmu"

STEP_PERIOD_NS = 10_000_000
DURATION_NS = 100_000_000

# The FMU variable names are the schema field names — that is the whole of the
# mapping, and the reason the importer needs no configuration of its own.
FMI_SCHEMAS = {
    "fmu.In": {
        "fields": [
            {"name": "Float64_continuous_input", "type": "f64"},
            {"name": "Float64_discrete_input", "type": "f64"},
        ]
    },
    "fmu.Out": {
        "fields": [
            {"name": "Float64_continuous_output", "type": "f64"},
            {"name": "Float64_discrete_output", "type": "f64"},
        ]
    },
}


def init_line(directions: dict[str, str]) -> dict:
    """The initialization line the kernel would send for these Channels."""
    return {
        "op": "init",
        "name": "fmu",
        "schemas": FMI_SCHEMAS,
        "channels": {
            channel: {"schema": channel, "direction": direction}
            for channel, direction in directions.items()
        },
    }


@pytest.fixture
def feedthrough():
    """One initialized `Feedthrough` instance, torn down after the test."""
    participant = FmuParticipant(FEEDTHROUGH)
    participant.on_init(init_line({"fmu.In": "in", "fmu.Out": "out"}))
    yield participant
    participant.close()


def stimulus(step: int) -> dict:
    return {
        "Float64_continuous_input": float(step),
        "Float64_discrete_input": 100.0 - step,
    }


def fmu_manifest(
    duration_ns: int = DURATION_NS,
    *,
    fmu=FEEDTHROUGH,
    schemas: dict = FMI_SCHEMAS,
) -> Manifest:
    """A ramp source feeding the imported FMU, which publishes its outputs."""
    m = Manifest(duration_ns=duration_ns)
    m.add_schemas(schemas)
    m.add_channel("fmu.In", schema="fmu.In")
    m.add_channel("fmu.Out", schema="fmu.Out")
    m.add_process(
        "source",
        command=[
            sys.executable,
            str(ROOT / "tests" / "participants" / "float64_ramp.py"),
            "fmu.In",
        ],
        step_period_ns=STEP_PERIOD_NS,
        publishes=["fmu.In"],
    )
    # The importer is named as an ordinary process participant's command, and
    # the FMU path is a command argument — which the Manifest already hashes.
    m.add_process(
        "feedthrough",
        command=[sys.executable, "-m", "sil.fmi", str(fmu)],
        step_period_ns=STEP_PERIOD_NS,
        subscribes=[SubscriberRoute("fmu.In", capacity=COMPAT_ROUTE_CAPACITY)],
        publishes=["fmu.Out"],
        priority=1,
    )
    return m


@pytest.fixture(scope="module")
def feedthrough_result(sil_run, tmp_path_factory):
    """One Run of the importer, shared by every assertion made about it."""
    return run_simulation(
        fmu_manifest(), runner=sil_run, workdir=tmp_path_factory.mktemp("fmi")
    )


class TestImportedFmu:
    """The importer driven directly, without the kernel in the way."""

    def test_channel_direction_decides_which_variables_are_written(
        self, feedthrough
    ):
        published = feedthrough.on_step(
            0, STEP_PERIOD_NS, [Input("fmu.In", 0, stimulus(3))]
        )
        assert published == [(
            "fmu.Out",
            {
                "Float64_continuous_output": 3.0,
                "Float64_discrete_output": 97.0,
            },
        )]

    def test_an_unfed_step_holds_the_variables_the_fmu_owns(self, feedthrough):
        feedthrough.on_step(0, STEP_PERIOD_NS, [Input("fmu.In", 0, stimulus(3))])
        (_, held), = feedthrough.on_step(STEP_PERIOD_NS, STEP_PERIOD_NS, [])
        assert held["Float64_continuous_output"] == 3.0

    def test_the_newest_message_in_a_burst_is_the_one_the_step_sees(
        self, feedthrough
    ):
        (_, published), = feedthrough.on_step(
            0,
            STEP_PERIOD_NS,
            [Input("fmu.In", 0, stimulus(1)), Input("fmu.In", 0, stimulus(2))],
        )
        assert published["Float64_continuous_output"] == 2.0


class TestCommunicationPoints:
    """The FMU's time base is derived from the kernel's integers, not summed."""

    def test_the_communication_point_never_accumulates(self, monkeypatch):
        """The same integer produces the same double, on step 10 000 as on 1.

        The step period is chosen to have no exact binary64 representation, so
        a running `point += size` separates from the integer-derived value
        well inside the step count a long Run reaches. Capturing the arguments
        at the co-simulation seam is the only place the contract is visible;
        the FMU's own state is not part of the assertion.
        """
        given = []
        monkeypatch.setattr(
            CoSimulation, "do_step",
            lambda self, point, size: given.append((point, size)),
        )
        period_ns = 7_000_003
        steps = 10_000
        participant = FmuParticipant(FEEDTHROUGH)
        participant.on_init(init_line({"fmu.In": "in", "fmu.Out": "out"}))
        try:
            for step in range(steps):
                participant.on_step(step * period_ns, period_ns, [])
        finally:
            participant.close()

        assert given == [
            (step * period_ns / NS_PER_S, period_ns / NS_PER_S)
            for step in range(steps)
        ]
        accumulated = 0.0
        for _ in range(steps):
            accumulated += period_ns / NS_PER_S
        assert accumulated != steps * period_ns / NS_PER_S


class TestRunBoundary:
    """The importer as a process participant in a complete Run."""

    def test_the_run_completes(self, feedthrough_result):
        assert len(feedthrough_result.messages("fmu.Out")) == (
            DURATION_NS // STEP_PERIOD_NS
        )

    def test_published_outputs_are_the_channel_inputs_one_step_later(
        self, feedthrough_result
    ):
        # The Channels take the default Latency, so a Message published at `t`
        # is visible to the importer at its next activation. Its first step
        # therefore publishes the FMU's start values, and every step after
        # publishes the value the source published one Step earlier.
        outputs = feedthrough_result.messages("fmu.Out")
        inputs = feedthrough_result.messages("fmu.In")
        assert outputs[0][1] == {
            "Float64_continuous_output": 0.0,
            "Float64_discrete_output": 0.0,
        }
        assert [
            (t, fields["Float64_continuous_output"],
             fields["Float64_discrete_output"])
            for t, fields in outputs[1:]
        ] == [
            (t + STEP_PERIOD_NS, fields["Float64_continuous_input"],
             fields["Float64_discrete_input"])
            for t, fields in inputs[:-1]
        ]


def described(tmp_path, rewrite=lambda text: text) -> Path:
    """An extracted FMU directory holding `Feedthrough`'s description alone.

    Parsing needs nothing else, so a rejection can be provoked by rewriting
    the vendored description rather than by hand-writing a second one.
    """
    with zipfile.ZipFile(FEEDTHROUGH) as archive:
        text = archive.read("modelDescription.xml").decode()
    (tmp_path / "modelDescription.xml").write_text(rewrite(text))
    return tmp_path


def fmu_variant(tmp_path, name: str, *, rewrite=lambda text: text,
                drop=lambda member: False) -> Path:
    """A copy of `Feedthrough.fmu` with members rewritten or left out."""
    path = tmp_path / f"{name}.fmu"
    with zipfile.ZipFile(FEEDTHROUGH) as source, zipfile.ZipFile(path, "w") as target:
        for member in source.infolist():
            if drop(member.filename):
                continue
            data = source.read(member.filename)
            if member.filename == "modelDescription.xml":
                data = rewrite(data.decode()).encode()
            target.writestr(member, data)
    return path


def without_co_simulation(text: str) -> str:
    """The description with its `CoSimulation` element removed."""
    start = text.index("<CoSimulation")
    return text[:start] + text[text.index("/>", start) + 2:]


def schemas_with(fmu_out: list[dict]) -> dict:
    """The Channel schemas, with the FMU's published fields replaced."""
    schemas = copy.deepcopy(FMI_SCHEMAS)
    schemas["fmu.Out"]["fields"] = fmu_out
    return schemas


class TestDescription:
    """Reading `modelDescription.xml`, against the vendored fixture."""

    def test_a_version_other_than_3_0_is_rejected(self, tmp_path):
        extracted = described(
            tmp_path, lambda text: text.replace('fmiVersion="3.0"', 'fmiVersion="2.0"')
        )
        with pytest.raises(ConfigurationError, match="2.0"):
            ModelDescription.read(extracted)

    def test_a_description_without_a_co_simulation_interface_is_rejected(
        self, tmp_path
    ):
        extracted = described(tmp_path, without_co_simulation)
        with pytest.raises(ConfigurationError, match="co-simulation"):
            ModelDescription.read(extracted)

    def test_causality_decides_which_variables_take_part_in_the_mapping(
        self, tmp_path
    ):
        """Only `input` and `output` variables map to Channel fields.

        `Feedthrough` also declares Float64 parameters and the independent
        variable `time`, which belong to the FMU itself and must not appear on
        either side of the mapping.
        """
        description = ModelDescription.read(described(tmp_path))
        assert description.inputs == {
            "Float64_continuous_input": 7,
            "Float64_discrete_input": 9,
        }
        assert description.outputs == {
            "Float64_continuous_output": 8,
            "Float64_discrete_output": 10,
        }


class TestPlatformDirectory:
    """The `binaries/` subdirectory the running platform loads from."""

    @pytest.mark.parametrize(
        ("machine", "system", "directory"),
        [
            ("arm64", "Darwin", "aarch64-darwin"),
            ("x86_64", "Darwin", "x86_64-darwin"),
            ("aarch64", "Linux", "aarch64-linux"),
            ("AMD64", "Linux", "x86_64-linux"),
        ],
    )
    def test_the_platform_names_the_directory(
        self, monkeypatch, machine, system, directory
    ):
        monkeypatch.setattr("platform.machine", lambda: machine)
        monkeypatch.setattr("platform.system", lambda: system)
        assert platform_directory() == directory

    def test_the_selected_directory_is_one_the_vendored_fmu_ships(
        self, tmp_path
    ):
        """The mapping has to land where `Feedthrough` carries a binary."""
        with zipfile.ZipFile(FEEDTHROUGH) as archive:
            archive.extractall(tmp_path)
        binary = ModelDescription.read(tmp_path).binary(tmp_path)
        assert binary.parent.name == platform_directory()
        assert binary.exists()


class TestRejectedAtStartup:
    """Every way of pointing the importer at the wrong FMU, at the Run boundary.

    Each of these is a configuration error (exit 2), distinct from a Run
    failure (exit 1), and each is raised before the first Step is taken.
    """

    def run_rejection(self, run_sil, tmp_path, **manifest) -> str:
        """Run a Manifest expected to be rejected; return its diagnostic."""
        proc = run_sil(
            fmu_manifest(**manifest).write(tmp_path / "manifest.json").path
        )
        assert proc.returncode == 2, proc.stderr
        return proc.stderr

    def test_a_version_other_than_3_0_is_rejected(self, run_sil, tmp_path):
        stderr = self.run_rejection(
            run_sil,
            tmp_path,
            fmu=fmu_variant(
                tmp_path,
                "fmi2",
                rewrite=lambda text: text.replace(
                    'fmiVersion="3.0"', 'fmiVersion="2.0"'
                ),
            ),
        )
        assert "2.0" in stderr

    def test_an_fmu_without_a_co_simulation_interface_is_rejected(
        self, run_sil, tmp_path
    ):
        stderr = self.run_rejection(
            run_sil,
            tmp_path,
            fmu=fmu_variant(tmp_path, "me-only", rewrite=without_co_simulation),
        )
        assert "co-simulation" in stderr

    def test_an_fmu_without_a_binary_for_this_platform_names_the_platform(
        self, run_sil, tmp_path
    ):
        directory = platform_directory()
        stderr = self.run_rejection(
            run_sil,
            tmp_path,
            fmu=fmu_variant(
                tmp_path,
                "foreign",
                drop=lambda member: member.startswith(f"binaries/{directory}/"),
            ),
        )
        assert f"carries no binary for {directory}" in stderr

    def test_a_schema_field_matching_no_fmu_variable_is_rejected(
        self, run_sil, tmp_path
    ):
        stderr = self.run_rejection(
            run_sil,
            tmp_path,
            schemas=schemas_with([
                {"name": "Float64_continuous_output", "type": "f64"},
                {"name": "Float64_typo_output", "type": "f64"},
            ]),
        )
        assert "Float64_typo_output" in stderr
        assert "fmu.Out" in stderr
        assert "Feedthrough" in stderr

    def test_an_fmu_variable_matching_no_schema_field_is_rejected(
        self, run_sil, tmp_path
    ):
        stderr = self.run_rejection(
            run_sil,
            tmp_path,
            schemas=schemas_with([
                {"name": "Float64_continuous_output", "type": "f64"},
            ]),
        )
        assert "Float64_discrete_output" in stderr
        assert "Feedthrough" in stderr
        assert "fmu.Out" in stderr

    @pytest.mark.parametrize("name", ["absent.fmu", "not-an-archive.fmu"])
    def test_an_unreadable_fmu_path_is_rejected(self, run_sil, tmp_path, name):
        fmu = tmp_path / name
        if name != "absent.fmu":
            fmu.write_text("this is not a zip archive")
        stderr = self.run_rejection(run_sil, tmp_path, fmu=fmu)
        assert f"cannot read FMU '{fmu}'" in stderr
