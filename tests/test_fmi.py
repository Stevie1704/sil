"""The FMI 3.0 co-simulation importer, at the run boundary and in-process.

The conformance target is the Modelica Association's own Reference FMUs,
vendored under `tests/fixtures/reference-fmus/`. `Feedthrough` sets every
output equal to its input, so a Run over it proves the whole path: the
subscribed Channel is written into the FMU's input variables, the FMU is
stepped, and its output variables are published on the Channel it publishes.
"""

import copy
import csv
import io
import itertools
import json
import math
import signal
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

import pytest
from conftest import COMPAT_ROUTE_CAPACITY, ROOT
from sil.fmi import (
    NS_PER_S,
    CoSimulation,
    FmuParticipant,
    ModelDescription,
    library_suffix,
    platform_directory,
)
from sil import fmi as sil_fmi
from sil import participant
from sil.fmi import runtime as fmi_runtime
from sil.fmi.description import SCALARS
from sil.fmi.runtime import BinaryBuffer, ScalarBuffer
from sil.manifest import Manifest, SubscriberRoute
from sil.participant import Input, ManifestError, ParticipantFailure
from sil.testing import run_simulation

FIXTURES = ROOT / "tests" / "fixtures" / "reference-fmus" / "3.0"
FEEDTHROUGH = FIXTURES / "Feedthrough.fmu"
BOUNCING_BALL = FIXTURES / "BouncingBall.fmu"

# The FMI-LS-REF layered standard's directory inside an FMU, and the reference
# CSV `BouncingBall.fmu` declares there.
LS_REF = "extra/org.fmi-standard.fmi-ls-ref"
LS_REF_MANIFEST = f"{LS_REF}/fmi-ls-manifest.xml"
BALL_REFERENCE_CSV = "BouncingBall_out.csv"
RESULT_ROLE = "result"

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

    @pytest.mark.parametrize(
        "status", ["Warning", "Discard", "Error", "Fatal"]
    )
    def test_an_fmu_status_failure_aborts_the_run(
        self, run_sil, tmp_path, build_dir, status
    ):
        fmu = failing_fmu(tmp_path, build_dir, status)
        proc = run_sil(
            fmu_manifest(fmu=fmu).write(tmp_path / f"failure-{status}.json").path
        )

        assert proc.returncode == 1
        assert "participant 'feedthrough' failed" in proc.stderr
        assert f"fmi3DoStep returned {status}" in proc.stderr

    def test_a_terminate_status_failure_aborts_the_run(
        self, run_sil, tmp_path, build_dir
    ):
        fmu = failing_fmu(tmp_path, build_dir, "Terminate")
        proc = run_sil(
            fmu_manifest(fmu=fmu).write(tmp_path / "failure-Terminate.json").path
        )

        assert proc.returncode == 1
        assert (
            "participant 'feedthrough' failed: fmi3Terminate returned Error"
            in proc.stderr
        )

    def test_an_fmu_termination_request_aborts_the_run(
        self, run_sil, tmp_path, build_dir
    ):
        fmu = failing_fmu(tmp_path, build_dir, "TerminateFlag")
        proc = run_sil(
            fmu_manifest(fmu=fmu).write(
                tmp_path / "failure-TerminateFlag.json"
            ).path
        )

        assert proc.returncode == 1
        assert "participant 'feedthrough' failed" in proc.stderr
        assert (
            "fmi3DoStep requested termination via terminateSimulation"
            in proc.stderr
        )

    def test_an_initialization_failure_is_a_run_failure(
        self, run_sil, tmp_path, build_dir
    ):
        """The importer accepted the init line, then its own call failed.

        That is a Run failure (exit 1), not a Manifest error (exit 2): the
        Manifest named an FMU the importer could drive.
        """
        fmu = failing_fmu(tmp_path, build_dir, "Initialize")
        proc = run_sil(
            fmu_manifest(fmu=fmu).write(tmp_path / "failure-Initialize.json").path
        )

        assert proc.returncode == 1, proc.stderr
        assert "participant 'feedthrough' failed" in proc.stderr
        assert "fmi3ExitInitializationMode returned Error" in proc.stderr


class TestExtractionLifetime:
    """An imported FMU's extracted archive belongs to one Run only."""

    def test_extraction_is_under_the_working_directory_and_is_cleaned(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.chdir(tmp_path)
        participant = FmuParticipant(FEEDTHROUGH)
        try:
            participant.on_init(init_line({"fmu.In": "in", "fmu.Out": "out"}))
            extracted = list(tmp_path.glob("sil-fmu-*"))
            assert len(extracted) == 1
            assert extracted[0].is_dir()
        finally:
            participant.close()

        assert list(tmp_path.glob("sil-fmu-*")) == []

    def test_a_run_leaves_no_extraction_directory(
        self, sil_run, tmp_path
    ):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        manifest = fmu_manifest().write(run_dir / "manifest.json").path
        proc = subprocess.run(
            [str(sil_run), str(manifest), "-o", str(run_dir / "out.mcap")],
            capture_output=True,
            text=True,
            cwd=run_dir,
        )

        assert proc.returncode == 0, proc.stderr
        assert list(run_dir.glob("sil-fmu-*")) == []

    def test_a_failed_run_leaves_no_extraction_directory(
        self, sil_run, tmp_path, build_dir
    ):
        """The Run the issue is about: an FMU that cannot continue.

        Aborting must not be the path that litters, so the failing Run gets
        the same assertion the passing one does.
        """
        fmu = failing_fmu(tmp_path, build_dir, "Error")
        run_dir = tmp_path / "failed-run"
        run_dir.mkdir()
        manifest = fmu_manifest(fmu=fmu).write(run_dir / "manifest.json").path
        proc = subprocess.run(
            [str(sil_run), str(manifest), "-o", str(run_dir / "out.mcap")],
            capture_output=True,
            text=True,
            cwd=run_dir,
        )

        assert proc.returncode == 1, proc.stderr
        assert list(run_dir.glob("sil-fmu-*")) == []

    def test_a_fatal_status_leaves_the_instance_alone(
        self, monkeypatch, tmp_path, build_dir
    ):
        """FMI 3.0 allows no further call on an instance that answered Fatal.

        This build's `fmi3Terminate` answers Error, so an importer that still
        terminated the instance would raise out of `close`.
        """
        monkeypatch.chdir(tmp_path)
        fmu = failing_fmu(tmp_path, build_dir, "FatalTerminateError")
        participant = FmuParticipant(fmu)
        participant.on_init(init_line({"fmu.In": "in", "fmu.Out": "out"}))
        with pytest.raises(ParticipantFailure, match="fmi3DoStep returned Fatal"):
            participant.on_step(0, STEP_PERIOD_NS, [])

        participant.close()

        assert list(tmp_path.glob("sil-fmu-*")) == []

    def test_a_fatal_terminate_still_drops_the_extraction(
        self, monkeypatch, tmp_path, build_dir
    ):
        """Fatal from `fmi3Terminate` bars the free that would have followed.

        Freeing is a call like any other, so the instance is abandoned; the
        extraction is the importer's own and is dropped either way.
        """
        monkeypatch.chdir(tmp_path)
        fmu = failing_fmu(tmp_path, build_dir, "TerminateFatal")
        participant = FmuParticipant(fmu)
        participant.on_init(init_line({"fmu.In": "in", "fmu.Out": "out"}))
        participant.on_step(0, STEP_PERIOD_NS, [])

        with pytest.raises(ParticipantFailure, match="fmi3Terminate returned Fatal"):
            participant.close()

        assert list(tmp_path.glob("sil-fmu-*")) == []

    def test_sigterm_leaves_no_extraction_directory(self, tmp_path):
        """A wedged importer is asked with SIGTERM before it is killed.

        The kernel escalates SIGTERM before SIGKILL precisely so this cleanup
        still runs; under SIGKILL the extraction would survive the Run.
        """
        importer = subprocess.Popen(
            [sys.executable, "-m", "sil.fmi", str(FEEDTHROUGH)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, cwd=tmp_path,
        )
        try:
            importer.stdin.write(
                json.dumps(init_line({"fmu.In": "in", "fmu.Out": "out"})) + "\n"
            )
            importer.stdin.flush()
            assert json.loads(importer.stdout.readline())["op"] == "ready"
            assert len(list(tmp_path.glob("sil-fmu-*"))) == 1

            importer.send_signal(signal.SIGTERM)
            assert importer.wait(timeout=10) != 0
        finally:
            if importer.poll() is None:
                importer.kill()
                importer.wait()

        assert list(tmp_path.glob("sil-fmu-*")) == []


def described(tmp_path, rewrite=lambda text: text) -> Path:
    """An extracted FMU directory holding `Feedthrough`'s description alone.

    Parsing needs nothing else, so a rejection can be provoked by rewriting
    the vendored description rather than by hand-writing a second one.
    """
    with zipfile.ZipFile(FEEDTHROUGH) as archive:
        text = archive.read("modelDescription.xml").decode()
    (tmp_path / "modelDescription.xml").write_text(rewrite(text))
    return tmp_path


def fmu_variant(tmp_path, name: str, *, source_fmu=FEEDTHROUGH,
                rewrite=lambda text: text,
                drop=lambda member: False) -> Path:
    """A copy of a vendored FMU with members rewritten or left out."""
    path = tmp_path / f"{name}.fmu"
    with zipfile.ZipFile(source_fmu) as source, zipfile.ZipFile(path, "w") as target:
        for member in source.infolist():
            if drop(member.filename):
                continue
            data = source.read(member.filename)
            if member.filename == "modelDescription.xml":
                data = rewrite(data.decode()).encode()
            target.writestr(member, data)
    return path


def failing_fmu(tmp_path, build_dir, status: str) -> Path:
    """Package the test FMU binary with the Reference description."""
    suffix = library_suffix()
    model_identifier = f"Failing{status}"
    binary = build_dir / f"{model_identifier}{suffix}"
    assert binary.exists(), f"failing FMU binary was not built at {binary}"
    path = tmp_path / f"failing-{status}.fmu"
    with zipfile.ZipFile(FEEDTHROUGH) as source, zipfile.ZipFile(path, "w") as target:
        for member in source.infolist():
            if member.filename.startswith("binaries/") and not member.filename.endswith("/"):
                continue
            data = source.read(member.filename)
            if member.filename == "modelDescription.xml":
                data = data.replace(
                    b'modelIdentifier="Feedthrough"',
                    b'modelIdentifier="' + model_identifier.encode() + b'"',
                )
            target.writestr(member, data)
        target.write(
            binary,
            arcname=(
                f"binaries/{platform_directory()}/{model_identifier}{suffix}"
            ),
        )
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
        with pytest.raises(ManifestError, match="2.0"):
            ModelDescription.read(extracted)

    def test_a_description_without_a_co_simulation_interface_is_rejected(
        self, tmp_path
    ):
        extracted = described(tmp_path, without_co_simulation)
        with pytest.raises(ManifestError, match="co-simulation"):
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


class TestTheCompatibilitySurface:
    """What `sil.fmi` exports, which SUPPORT.md covers by name.

    The policy excludes leading-underscore names and unlisted modules, and
    nothing else — so every other name this module exported stays exported
    until a release says otherwise. That includes the four it only ever
    imported in order to use them: they were importable from here, so they are
    still importable from here.
    """

    def test_the_importer_exports_what_it_owns(self):
        assert set(sil_fmi.__all__) == {
            "NS_PER_S",
            "BusProfile",
            "CoSimulation",
            "FmuGroupParticipant",
            "FmuParticipant",
            "ModelDescription",
            "Terminal",
            "Variable",
            "library_suffix",
            "main",
            "platform_directory",
        }

    @pytest.mark.parametrize(
        "name", ["ManifestError", "ParticipantFailure", "StepParticipant", "run"]
    )
    def test_a_name_this_module_carried_is_still_importable(self, name):
        """These belong to `sil.participant`, which is where new code takes
        them from; they stay reachable here because they always were."""
        assert getattr(sil_fmi, name) is getattr(participant, name)


class TestNativeBuffers:
    """The seam the layers above the native calls hand values across.

    A buffer owns the memory one FMI call reads or writes, and it is allocated
    before any FMU is loaded — a mapping is resolved, and rejected, without
    loading one. So what a buffer takes and hands back is Python: value
    references and numbers or `bytes`, never a ctypes array. These drive one
    real `Feedthrough` instance through the buffers alone, because the
    ownership rule is only true if the FMU actually reads what was written.
    """

    # `Feedthrough` copies each input to the output of the same name. The
    # value references are its own, out of `modelDescription.xml`.
    CONTINUOUS_IN, CONTINUOUS_OUT = 7, 8
    DISCRETE_IN, DISCRETE_OUT = 9, 10
    BINARY_IN, BINARY_OUT = 31, 32

    @pytest.fixture
    def instance(self, tmp_path):
        """One initialized `Feedthrough`, driven through buffers alone."""
        with zipfile.ZipFile(FEEDTHROUGH) as archive:
            archive.extractall(tmp_path)
        description = ModelDescription.read(tmp_path)
        fmu = CoSimulation(description.binary(tmp_path), description)
        fmu.initialize()
        yield fmu
        fmu.close()

    def test_one_scalar_buffer_carries_several_variables_in_one_call(
        self, instance
    ):
        """`Feedthrough` copies each input to the output of the same name, so
        what one buffer wrote is what the other reads back after a step."""
        written = ScalarBuffer(
            "Float64", [self.CONTINUOUS_IN, self.DISCRETE_IN]
        )
        read = ScalarBuffer(
            "Float64", [self.CONTINUOUS_OUT, self.DISCRETE_OUT]
        )

        written.write(instance, [2.5, -4.0])
        instance.do_step(0.0, 1e-3)

        assert read.read(instance) == [2.5, -4.0]

    def test_a_scalar_buffer_is_reused_across_steps(self, instance):
        """The buffer is allocated once and holds the values of the step it is
        in; a second step must not see the first one's."""
        written = ScalarBuffer("Float64", [self.CONTINUOUS_IN])
        read = ScalarBuffer("Float64", [self.CONTINUOUS_OUT])
        observed = []
        for step, value in enumerate([1.0, 2.0, 3.0]):
            written.write(instance, [value])
            instance.do_step(step * 1e-3, 1e-3)
            observed.append(read.read(instance)[0])
        assert observed == [1.0, 2.0, 3.0]

    def test_a_written_binary_buffer_owns_what_it_hands_the_fmu(
        self, instance
    ):
        """A payload shorter than the capacity is handed over at its own
        length, and the buffer behind it is the importer's for the whole
        call."""
        written = BinaryBuffer([self.BINARY_IN], [1024])
        read = BinaryBuffer([self.BINARY_OUT])

        written.write(instance, [b"\x01\x02\x03"])
        instance.do_step(0.0, 1e-3)

        assert read.read(instance) == [b"\x01\x02\x03"]

    def test_a_read_binary_buffer_copies_out_before_the_next_call(
        self, instance
    ):
        """The pointers a read buffer hands over are the FMU's own and valid
        only until its next call, so what comes back has to survive one."""
        written = BinaryBuffer([self.BINARY_IN], [1024])
        read = BinaryBuffer([self.BINARY_OUT])

        written.write(instance, [b"first"])
        instance.do_step(0.0, 1e-3)
        first = read.read(instance)[0]

        written.write(instance, [b"second"])
        instance.do_step(1e-3, 1e-3)
        second = read.read(instance)[0]

        assert (first, second) == (b"first", b"second")

    def test_every_scalar_type_the_mapping_declares_has_a_native_element(self):
        """The two halves of one scalar type are keyed alike in two modules; a
        type declared on one side and not the other would fail on the first Run
        that bound it rather than here."""
        assert set(fmi_runtime._ELEMENTS) == set(SCALARS)


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

    Each of these is a Manifest error (exit 2), distinct from a Run
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


@dataclass(frozen=True)
class ReferenceResult:
    """The trajectory an FMU ships for its own default experiment.

    `rows` is the reference CSV in file order, each row mapping a column name
    to its value, the independent variable `time` included. The first row is
    the post-initialization value, read after initialization and before the
    first step, so an importer that steps first produces its `k`-th value
    for row `k + 1`.

    `step_size` is the default experiment's declared step, in seconds. The
    trajectory is only valid at that step: a different step is a different
    experiment, with no reference to check against.
    """

    step_size: float
    rows: list[dict[str, float]]


def _result_source(ls_ref_manifest: bytes, fmu_path: Path) -> str:
    """The related file the FMI-LS-REF manifest gives the `result` role."""
    for related in ElementTree.fromstring(ls_ref_manifest).findall("Related"):
        if related.get("role") == RESULT_ROLE and related.get("source"):
            return related.get("source")
    raise AssertionError(
        f"FMU {str(fmu_path)!r} declares no FMI-LS-REF related file naming a "
        f"source for the {RESULT_ROLE!r} role"
    )


def _default_step_size(description: bytes, fmu_path: Path) -> float:
    """The step the default experiment declares, in seconds."""
    experiment = ElementTree.fromstring(description).find("DefaultExperiment")
    step_size = None if experiment is None else experiment.get("stepSize")
    if step_size is None:
        raise AssertionError(
            f"FMU {str(fmu_path)!r} declares no default experiment step size, "
            f"so its reference result names no step to reproduce it at"
        )
    return float(step_size)


def _reference_rows(result: bytes) -> list[dict[str, float]]:
    """The reference CSV, one dict per row, at the precision it was written."""
    return [
        {name: float(value) for name, value in row.items()}
        for row in csv.DictReader(io.StringIO(result.decode()))
    ]


def reference_result(fmu_path: Path) -> ReferenceResult:
    """Read the reference trajectory the FMU carries under FMI-LS-REF.

    Everything comes out of the archive: the layered standard's manifest names
    the related file holding the result, so nothing has to be vendored beside
    the FMU and kept in sync with it.

    This reads an FMU but is not part of driving one, so it lives with the
    checks that need it rather than in `sil.fmi`. A Run never asks an FMU what
    it should have computed; only a test does.

    Every way the archive can disappoint — absent, not an archive, missing a
    member, or carrying one that does not parse — fails loudly here, because
    the alternative is a comparison that comes back empty and passes.
    """
    try:
        with zipfile.ZipFile(fmu_path) as archive:
            source = _result_source(archive.read(LS_REF_MANIFEST), fmu_path)
            rows = _reference_rows(archive.read(f"{LS_REF}/{source}"))
            step_size = _default_step_size(
                archive.read("modelDescription.xml"), fmu_path
            )
    except (OSError, zipfile.BadZipFile, KeyError, ElementTree.ParseError,
            UnicodeDecodeError, ValueError) as error:
        raise AssertionError(
            f"cannot read the reference result of FMU {str(fmu_path)!r}: "
            f"{error}"
        ) from error
    return ReferenceResult(step_size=step_size, rows=rows)


def ball_with_reference(tmp_path, name: str, *,
                        csv_name: str = BALL_REFERENCE_CSV,
                        ls_ref_xml: str | None = None) -> Path:
    """`BouncingBall.fmu` with its FMI-LS-REF members altered.

    Renaming the CSV rewrites the layered-standard manifest to name it, so an
    importer that reads the manifest still finds the trajectory and one that
    guesses the filename no longer does. Replacing the manifest outright is
    how a declaration the importer must reject is provoked.
    """
    path = tmp_path / f"{name}.fmu"
    with zipfile.ZipFile(BOUNCING_BALL) as source, zipfile.ZipFile(path, "w") as target:
        for member in source.infolist():
            data = source.read(member.filename)
            if member.filename == f"{LS_REF}/{BALL_REFERENCE_CSV}":
                member.filename = f"{LS_REF}/{csv_name}"
            elif member.filename == f"{LS_REF}/fmi-ls-manifest.xml":
                data = (
                    data.replace(BALL_REFERENCE_CSV.encode(), csv_name.encode())
                    if ls_ref_xml is None
                    else ls_ref_xml.encode()
                )
            target.writestr(member, data)
    return path


class TestReferenceResultDiscovery:
    """Finding the shipped trajectory inside the FMU, under FMI-LS-REF.

    The Reference FMU carries its own result: a layered-standard manifest
    declaring a related CSV with the `result` role, and the CSV itself. That
    is the whole source — no side file and no vendored copy to keep in sync
    with the archive it came from.

    An archive that cannot give up its trajectory has to say so. Coming back
    with no rows would leave the comparison below with nothing to compare and
    passing on an empty answer.
    """

    def test_the_layered_standard_manifest_names_the_file_that_is_read(
        self, tmp_path
    ):
        """Discovery goes through the manifest, not through a known filename."""
        renamed = ball_with_reference(
            tmp_path, "renamed", csv_name="somewhere-else.csv"
        )
        assert reference_result(renamed).rows == reference_result(BOUNCING_BALL).rows

    def test_an_fmu_carrying_no_reference_result_cannot_be_read(self, tmp_path):
        stripped = fmu_variant(
            tmp_path,
            "no-reference",
            source_fmu=BOUNCING_BALL,
            drop=lambda member: member.startswith(f"{LS_REF}/"),
        )
        with pytest.raises(AssertionError, match=LS_REF):
            reference_result(stripped)

    def test_an_fmu_declaring_no_result_role_cannot_be_read(self, tmp_path):
        """A related file of another role is not a trajectory to check against."""
        roleless = ball_with_reference(
            tmp_path,
            "no-result-role",
            ls_ref_xml=(
                '<fmiReferences><Related type="text/csv" '
                f'source="{BALL_REFERENCE_CSV}" role="input"/></fmiReferences>'
            ),
        )
        with pytest.raises(AssertionError, match="result"):
            reference_result(roleless)

    def test_an_fmu_declaring_no_default_step_size_cannot_be_read(self, tmp_path):
        """The trajectory is only valid at the step it was produced at."""
        stepless = fmu_variant(
            tmp_path,
            "no-step-size",
            source_fmu=BOUNCING_BALL,
            rewrite=lambda text: text.replace(' stepSize="1e-2"', ""),
        )
        with pytest.raises(AssertionError, match="step size"):
            reference_result(stepless)

    def test_the_declared_step_size_comes_back_in_seconds(self):
        assert reference_result(BOUNCING_BALL).step_size == 1e-2


BALL_CHANNEL = "ball.State"
BALL_SCHEMAS = {
    BALL_CHANNEL: {
        "fields": [{"name": "h", "type": "f64"}, {"name": "v", "type": "f64"}]
    }
}
# `BouncingBall`'s default experiment: dropped from 1 m, stopping at 3 s, and
# stepped at the 10 ms its description declares. The shipped trajectory is the
# output of exactly this experiment, so the Manifest has to declare the same
# step — that equality is asserted below rather than left as a comment.
BALL_DURATION_NS = 3_000_000_000
BALL_STEP_PERIOD_NS = 10_000_000
DROP_HEIGHT = 1.0

# The tolerance the reference comparison holds to. Well above the deviation
# measured here (1.144e-14 relative) and far below anything a mapping or
# stepping mistake would produce.
RELATIVE_TOLERANCE = 1e-12


def bouncing_ball_manifest() -> Manifest:
    """`BouncingBall`'s default experiment as a Run.

    The FMU takes no input, so the importer only publishes: the Channel
    carries the two output variables the model declares.
    """
    m = Manifest(duration_ns=BALL_DURATION_NS)
    m.add_schemas(BALL_SCHEMAS)
    m.add_channel(BALL_CHANNEL, schema=BALL_CHANNEL)
    m.add_process(
        "ball",
        command=[sys.executable, "-m", "sil.fmi", str(BOUNCING_BALL)],
        step_period_ns=BALL_STEP_PERIOD_NS,
        publishes=[BALL_CHANNEL],
    )
    return m


@pytest.fixture(scope="module")
def bouncing_ball_result(sil_run, tmp_path_factory):
    """One Run of the default experiment, shared by every check made on it."""
    return run_simulation(
        bouncing_ball_manifest(),
        runner=sil_run,
        workdir=tmp_path_factory.mktemp("ball"),
    )


@pytest.fixture(scope="module")
def shipped_reference():
    return reference_result(BOUNCING_BALL)


def trajectory(result) -> list[tuple[float, float]]:
    """The recorded Run as `(h, v)` pairs, in the order they were published."""
    return [(fields["h"], fields["v"]) for _, fields in result.messages(BALL_CHANNEL)]


class TestShippedReference:
    """The recorded trajectory against the result the FMU ships for itself.

    This is what the Determinism check cannot answer. Running twice and
    bit-comparing catches a Run that is not reproducible; it says nothing
    about a Run that is reproducibly wrong. An importer that maps the wrong
    variable, drops an input, or steps at the wrong communication point is
    perfectly deterministic and perfectly incorrect, and only the vendor's own
    trajectory tells the two apart.
    """

    def test_the_manifest_steps_at_the_declared_default_step_size(
        self, shipped_reference
    ):
        """A different step is a different experiment with no reference."""
        assert BALL_STEP_PERIOD_NS == shipped_reference.step_size * NS_PER_S

    def test_the_first_reference_row_is_read_before_the_first_step(
        self, bouncing_ball_result, shipped_reference
    ):
        """The first row is the post-initialization value, which no Message carries.

        The importer publishes after its step, so the first recorded Message
        already holds the state one step in and the recorded trajectory lines
        up with the reference from its second row. A comparison that starts at
        the first row is off by one on every row after it — which the recorded
        times make visible, because being off by one shifts each of them by a
        whole step.
        """
        recorded = bouncing_ball_result.messages(BALL_CHANNEL)
        assert shipped_reference.rows[0] == {
            "time": 0.0, "h": DROP_HEIGHT, "v": 0.0
        }
        assert len(recorded) == len(shipped_reference.rows) - 1
        assert [
            (t + BALL_STEP_PERIOD_NS) / NS_PER_S for t, _ in recorded
        ] == pytest.approx([row["time"] for row in shipped_reference.rows[1:]])

    def test_the_recorded_trajectory_matches_the_shipped_reference(
        self, bouncing_ball_result, shipped_reference
    ):
        """A tolerance check, and never a byte or bit comparison.

        The distinction is measured, not assumed. Stepping this FMU through
        its full default experiment on macOS aarch64 leaves 399 of the 600
        recorded values
        bit-identical to the shipped ones and 201 differing, at a maximum
        relative deviation of 1.144e-14 — a worst case of 59 units in the last
        place near a bounce, where the height approaches zero and cancellation
        amplifies the difference. It is not accumulated time: driving the
        communication point from integer nanoseconds and accumulating it in a
        double diverge first on the same row. It is the machine class — the
        model integrates with a multiply-add that one architecture contracts
        into a single rounding and another compiles as two.

        Determinism here is scoped to the same artifacts on the same machine
        class, and this CSV was produced elsewhere — on the vendor's machine
        class the same 600 values would be bit-identical. Bit-comparing would
        assert the cross-platform bit-exactness `CONTEXT.md` (Determinism)
        declares is not claimed, and would fail on a machine the importer is
        correct on.
        """
        deviations = [
            (row["time"], name, row[name], fields[name])
            for (_, fields), row in zip(
                bouncing_ball_result.messages(BALL_CHANNEL),
                shipped_reference.rows[1:],
                strict=True,
            )
            for name in ("h", "v")
            if not math.isclose(
                fields[name], row[name],
                rel_tol=RELATIVE_TOLERANCE, abs_tol=0.0,
            )
        ]
        assert deviations == []

    def test_the_determinism_check_still_passes_for_an_fmu_run(
        self, sil_run, tmp_path
    ):
        """The two checks answer different questions and both belong.

        The reference check asks whether the importer is correct; the
        Determinism check asks whether the Run reproduces. Importing an FMU
        leaves the second one exactly as it was.
        """
        ref = bouncing_ball_manifest().write(tmp_path / "ball.json")
        proc = subprocess.run(
            [sys.executable, "-m", "sil.check", str(ref.path),
             "--runner", str(sil_run)],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.startswith("deterministic: ")


def rebound_peaks(states: list[tuple[float, float]]) -> list[float]:
    """The height reached after each bounce.

    A bounce reverses the velocity, so every stretch of upward motion ends at
    one peak, and the coefficient of restitution is what takes the next one
    down.
    """
    peaks = []
    climb: list[float] = []
    for height, velocity in states:
        if velocity > 0:
            climb.append(height)
        elif climb:
            peaks.append(max(climb))
            climb = []
    return peaks


class TestRecordedBehavior:
    """The recorded trajectory read as behavior rather than as numbers.

    A Run that stepped a dead instance — never entered, or entered and never
    advanced — would hold its start values and still reproduce bit-for-bit on
    a second Run. Requiring the ball to fall, to bounce lower each time, and
    to come to rest is what a constant trajectory cannot satisfy.
    """

    def test_the_ball_falls_until_it_first_bounces(self, bouncing_ball_result):
        states = trajectory(bouncing_ball_result)
        falling = [h for h, v in itertools.takewhile(lambda s: s[1] < 0, states)]
        assert len(falling) > 1
        assert falling[0] < DROP_HEIGHT
        assert all(a > b for a, b in zip(falling, falling[1:]))

    def test_each_rebound_is_lower_than_the_one_before(self, bouncing_ball_result):
        peaks = rebound_peaks(trajectory(bouncing_ball_result))
        assert len(peaks) >= 3
        assert peaks[0] < DROP_HEIGHT
        assert all(a > b for a, b in zip(peaks, peaks[1:]))

    def test_the_ball_comes_to_rest_on_the_floor(self, bouncing_ball_result):
        states = trajectory(bouncing_ball_result)
        assert min(h for h, _ in states) >= 0.0
        height, velocity = states[-1]
        assert velocity == 0.0
        assert height < 1e-9
