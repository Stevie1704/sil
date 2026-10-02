"""The FMI 2.0 co-simulation profile of the importer (issue #191).

Two kinds of FMU back these tests. The fake FMU of `tests/fmi2_fixture.py` is
built for the host, so the lifecycle, the status rules and the cleanup are
checked on every host the suite runs on. The Modelica Reference FMUs
`v0.0.41`, vendored under `tests/fixtures/reference-fmus/2.0/`, ship FMI 2.0
binaries for x86-64 only, so a Run of one is checked on Linux x86-64 only;
reading and inspecting them needs no binary and is checked everywhere.

The comparison with FMPy on an observation grid is the proof's
(`proofs/fmi2-importer/`), because FMPy is not a dependency of this suite.
"""

from __future__ import annotations

import platform
import re
import sys
import zipfile
from pathlib import Path

import pytest
from conftest import COMPAT_ROUTE_CAPACITY, ROOT
from fmi2_fixture import fake_fmu
from sil.fmi import FmuGroupParticipant, FmuParticipant, ModelDescription
from sil.fmi.description import fmi2_platform_directory, library_suffix
from sil.fmi.inspection import inspect, render
from sil.manifest import Manifest, SubscriberRoute
from sil.participant import Input, ManifestError, ParticipantFailure
from sil.testing import run_simulation

REFERENCE = ROOT / "tests" / "fixtures" / "reference-fmus" / "2.0"
DAHLQUIST = REFERENCE / "Dahlquist.fmu"
VAN_DER_POL = REFERENCE / "VanDerPol.fmu"
BOUNCING_BALL = REFERENCE / "BouncingBall.fmu"
STAIR = REFERENCE / "Stair.fmu"
FEEDTHROUGH = REFERENCE / "Feedthrough.fmu"

STEP_PERIOD_NS = 10_000_000

# The Reference FMUs' FMI 2.0 binaries are x86-64; the profile is Linux.
LINUX_X86_64 = (platform.system(), platform.machine()) == ("Linux", "x86_64")
reference_binaries = pytest.mark.skipif(
    not LINUX_X86_64,
    reason="the Reference FMUs ship FMI 2.0 binaries for x86-64 only",
)

FAKE_SCHEMAS = {
    "fake.In": {"fields": [
        {"name": "u", "type": "f64"},
        {"name": "n", "type": "i32"},
        {"name": "b", "type": "u8"},
    ]},
    "fake.Out": {"fields": [
        {"name": "y", "type": "f64"},
        {"name": "m", "type": "i32"},
        {"name": "c", "type": "u8"},
    ]},
}
FAKE_BINDS = [
    "fake.In:u=u", "fake.In:n=n", "fake.In:b=b",
    "fake.Out:y=y", "fake.Out:m=m", "fake.Out:c=c",
]
FAKE_INIT = {
    "op": "init", "name": "fake", "schemas": FAKE_SCHEMAS,
    "channels": {
        "fake.In": {"schema": "fake.In", "direction": "in"},
        "fake.Out": {"schema": "fake.Out", "direction": "out"},
    },
}


@pytest.fixture
def fake(build_dir, tmp_path):
    """The archive of one build of the fake FMU, by its variant name."""

    def build(variant: str = "Ok", rewrite=lambda text: text) -> Path:
        binary = build_dir / f"Fmi2{variant}{library_suffix()}"
        assert binary.exists(), f"fake FMI 2.0 binary was not built at {binary}"
        return fake_fmu(
            tmp_path / f"Fmi2{variant}.fmu", binary,
            fmi2_platform_directory(), rewrite,
        )

    return build


def described(tmp_path: Path, archive: Path, rewrite=lambda text: text) -> Path:
    """An archive's files, extracted, with its description rewritten."""
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(archive) as opened:
        opened.extractall(extracted)
    path = extracted / "modelDescription.xml"
    path.write_text(rewrite(path.read_text()))
    return extracted


def calls(stderr: str) -> list[str]:
    """The lifecycle calls the fake FMU reported, in order, by name."""
    return re.findall(r"^fmu: (fmi2\w+)", stderr, re.MULTILINE)


class TestDescription:
    """Reading an FMI 2.0 `modelDescription.xml` into the importer's terms."""

    def test_an_fmi2_co_simulation_description_is_read(self, tmp_path):
        description = ModelDescription.read(described(tmp_path, DAHLQUIST))

        assert description.fmi_version == "2.0"
        assert description.model_identifier == "Dahlquist"
        assert description.instantiation_token == (
            "{221063D2-EF4A-45FE-B954-B5BFEEA9A59B}"
        )
        assert description.outputs == {"x": 1}
        assert description.variables["k"].causality == "parameter"

    def test_each_fmi2_type_is_carried_as_its_fmi3_counterpart(self, tmp_path):
        """Real, Integer and Boolean are Float64, Int32 and Boolean.

        They are carried by the same fields and read by the same start-value
        grammar as the FMI 3.0 types of the same width, so the mapping is the
        one the FMI 3.0 profile already states.
        """
        description = ModelDescription.read(described(tmp_path, STAIR))
        assert description.variables["counter"].kind == "Int32"

    def test_string_and_enumeration_variables_refuse_the_fmu(self, tmp_path):
        with pytest.raises(ManifestError) as refused:
            ModelDescription.read(described(tmp_path, FEEDTHROUGH))

        message = str(refused.value)
        for name in ("String_input", "String_output", "Enumeration_input",
                     "Enumeration_output"):
            assert repr(name) in message
        assert "FMI 2.0 profile" in message

    def test_model_exchange_alone_is_refused(self, tmp_path):
        def model_exchange_only(text: str) -> str:
            start = text.index("<CoSimulation")
            end = text.index("</CoSimulation>") + len("</CoSimulation>")
            return text[:start] + text[end:]

        with pytest.raises(ManifestError, match="co-simulation"):
            ModelDescription.read(
                described(tmp_path, DAHLQUIST, model_exchange_only)
            )

    def test_the_binary_is_looked_up_in_the_fmi2_platform_directory(
        self, tmp_path
    ):
        extracted = described(tmp_path, DAHLQUIST)
        description = ModelDescription.read(extracted)
        if fmi2_platform_directory() is None:
            with pytest.raises(ManifestError, match="FMI 2.0"):
                description.binary(extracted)
            return
        assert description.binary(extracted) == (
            extracted / "binaries" / fmi2_platform_directory()
            / f"Dahlquist{library_suffix()}"
        )


class TestInspection:
    """`sil-fmi-inspect` reports the version and checks its own profile."""

    def test_an_fmi2_reference_fmu_is_selected(self):
        report = inspect(DAHLQUIST)

        assert report["facts"]["fmi_version"] == "2.0"
        assert "profile: FMI 2.0 co-simulation" in render(report)
        assert report["facts"]["instantiation_token"] == (
            "{221063D2-EF4A-45FE-B954-B5BFEEA9A59B}"
        )
        variables = {v["name"]: v for v in report["variables"]}
        assert variables["x"]["type"] == "Real"
        assert variables["x"]["start"] == "1"
        assert variables["x"]["unmappable"] is None
        if fmi2_platform_directory() is not None:
            assert report["verdict"] == "compatible", report["unusable"]

    def test_feedthrough_is_refused_with_the_reason_named(self):
        report = inspect(FEEDTHROUGH)

        assert report["verdict"] == "unusable"
        (reason,) = report["unusable"]
        assert "String" in reason and "Enumeration" in reason

    def test_a_refused_archive_names_the_fmi2_platform(self):
        """The platform follows the declared version on a refusal too."""
        report = inspect(FEEDTHROUGH)

        assert report["verdict"] == "unusable"
        assert report["platform"] == fmi2_platform_directory()

    def test_an_unbound_calculated_parameter_is_listed(self, fake):
        mapping = {"sil_fmi_mapping": 1, "schemas": FAKE_SCHEMAS,
                   "channels": FAKE_INIT["channels"], "bind": FAKE_BINDS}
        report = inspect(fake(), mapping)

        assert report["mapping"]["accepted"], report["mapping"]
        assert "offset" in report["mapping"]["unbound"]

    def test_an_fmi3_report_names_its_own_profile(self):
        fmi3 = ROOT / "tests" / "fixtures" / "reference-fmus" / "3.0"
        report = inspect(fmi3 / "BouncingBall.fmu")

        assert "profile: FMI 3.0 co-simulation" in render(report)


def fake_participant(archive: Path, *, starts=(), binds=FAKE_BINDS):
    participant = FmuParticipant(archive, binds=binds, starts=starts)
    participant.on_init(FAKE_INIT)
    return participant


def fields(u: float = 0.0, n: int = 0, b: int = 0) -> dict:
    return {"u": u, "n": n, "b": b}


class TestLifecycle:
    """The FMI 2.0 lifecycle, driven in-process against the fake FMU."""

    def test_the_calls_follow_the_fmi2_co_simulation_lifecycle(
        self, fake, capfd
    ):
        participant = fake_participant(fake())
        participant.on_step(0, STEP_PERIOD_NS, [])
        participant.on_step(STEP_PERIOD_NS, STEP_PERIOD_NS, [])
        participant.close()

        assert calls(capfd.readouterr().err) == [
            "fmi2Instantiate", "fmi2SetupExperiment",
            "fmi2EnterInitializationMode", "fmi2ExitInitializationMode",
            "fmi2DoStep", "fmi2DoStep", "fmi2Terminate", "fmi2FreeInstance",
        ]

    def test_the_resources_directory_is_a_file_uri(self, fake, capfd):
        participant = fake_participant(fake())
        participant.close()

        (uri,) = re.findall(r"fmi2Instantiate (\S+)", capfd.readouterr().err)
        assert uri.startswith("file:///")
        assert uri.endswith("/resources")

    def test_the_experiment_starts_at_zero_with_no_stop_time(
        self, fake, capfd
    ):
        participant = fake_participant(fake())
        participant.close()

        assert "fmi2SetupExperiment 0 0" in capfd.readouterr().err

    def test_each_step_covers_the_kernel_interval(self, fake, capfd):
        participant = fake_participant(fake())
        participant.on_step(0, STEP_PERIOD_NS, [])
        participant.on_step(STEP_PERIOD_NS, STEP_PERIOD_NS, [])
        participant.close()

        assert re.findall(r"fmi2DoStep (\S+ \S+)", capfd.readouterr().err) == [
            "0 0.01", "0.01 0.01",
        ]

    def test_real_integer_and_boolean_cross_the_step(self, fake):
        participant = fake_participant(fake())
        try:
            published = participant.on_step(
                0, STEP_PERIOD_NS, [Input("fake.In", 0, fields(2.5, 40, 1))]
            )
        finally:
            participant.close()

        assert published == [("fake.Out", {"y": 2.5, "m": 41, "c": 0})]

    def test_a_parameter_start_value_is_applied_before_initialization(
        self, fake
    ):
        participant = fake_participant(fake(), starts=["gain=3"])
        try:
            (_, out), = participant.on_step(
                0, STEP_PERIOD_NS, [Input("fake.In", 0, fields(2.0))]
            )
        finally:
            participant.close()

        assert out["y"] == 6.0

    def test_a_calculated_parameter_takes_no_start_value(self, fake):
        participant = FmuParticipant(
            fake(), binds=FAKE_BINDS, starts=["offset=1"]
        )
        try:
            with pytest.raises(ManifestError, match="calculatedParameter"):
                participant.on_init(FAKE_INIT)
        finally:
            participant.close()

    def test_a_calculated_parameter_is_read_after_initialization(self, fake):
        """`offset` is computed as 2 * gain when initialization ends."""
        schemas = {**FAKE_SCHEMAS,
                   "fake.Offset": {"fields": [{"name": "o", "type": "f64"}]}}
        init = {**FAKE_INIT, "schemas": schemas, "channels": {
            **FAKE_INIT["channels"],
            "fake.Offset": {"schema": "fake.Offset", "direction": "out"},
        }}
        participant = FmuParticipant(
            fake(), binds=[*FAKE_BINDS, "fake.Offset:o=offset"],
            starts=["gain=4"],
        )
        try:
            participant.on_init(init)
            published = dict(participant.on_step(0, STEP_PERIOD_NS, []))
        finally:
            participant.close()

        assert published["fake.Offset"] == {"o": 8.0}

    def test_a_calculated_parameter_is_never_written(self, fake):
        participant = FmuParticipant(fake(), binds=["fake.In:u=offset"])
        init = {**FAKE_INIT, "schemas": {
            "fake.In": {"fields": [{"name": "u", "type": "f64"}]}},
            "channels": {"fake.In": {"schema": "fake.In", "direction": "in"}}}
        try:
            with pytest.raises(ManifestError, match="calculatedParameter"):
                participant.on_init(init)
        finally:
            participant.close()

    def test_a_warning_continues_with_a_log_line(self, fake, capfd):
        participant = fake_participant(fake("StepWarning"))
        try:
            participant.on_step(0, STEP_PERIOD_NS, [])
            participant.on_step(STEP_PERIOD_NS, STEP_PERIOD_NS, [])
        finally:
            participant.close()

        err = capfd.readouterr().err
        assert err.count("fmi2DoStep returned Warning") == 2
        assert calls(err)[-2:] == ["fmi2Terminate", "fmi2FreeInstance"]

    @pytest.mark.parametrize(
        "status", ["Discard", "Error", "Fatal", "Pending"]
    )
    def test_a_failing_status_names_the_call(self, fake, status):
        participant = fake_participant(fake(f"Step{status}"))
        try:
            with pytest.raises(ParticipantFailure) as failed:
                participant.on_step(0, STEP_PERIOD_NS, [])
        finally:
            participant.close()

        assert f"fmi2DoStep returned {status}" in str(failed.value)

    def test_pending_is_named_as_unsupported_asynchronous_stepping(self, fake):
        participant = fake_participant(fake("StepPending"))
        try:
            with pytest.raises(ParticipantFailure, match="asynchronous"):
                participant.on_step(0, STEP_PERIOD_NS, [])
        finally:
            participant.close()

    def test_after_an_error_the_instance_is_freed_without_terminate(
        self, fake, capfd
    ):
        participant = fake_participant(fake("StepErrorTerminateError"))
        with pytest.raises(ParticipantFailure):
            participant.on_step(0, STEP_PERIOD_NS, [])
        participant.close()

        assert calls(capfd.readouterr().err)[-2:] == [
            "fmi2DoStep", "fmi2FreeInstance",
        ]

    def test_after_fatal_no_further_call_is_made(self, fake, capfd):
        participant = fake_participant(fake("StepFatal"))
        with pytest.raises(ParticipantFailure):
            participant.on_step(0, STEP_PERIOD_NS, [])
        participant.close()

        assert calls(capfd.readouterr().err)[-1] == "fmi2DoStep"

    def test_a_failing_terminate_still_frees(self, fake, capfd):
        participant = fake_participant(fake("TerminateError"))
        participant.on_step(0, STEP_PERIOD_NS, [])
        with pytest.raises(ParticipantFailure, match="fmi2Terminate returned"):
            participant.close()

        assert calls(capfd.readouterr().err)[-1] == "fmi2FreeInstance"

    def test_a_group_refuses_an_fmi2_instance(self, fake):
        group = FmuGroupParticipant(
            [("node", str(fake()))], connects=[], binds=[], starts=[],
            profile="application/vnd.example",
        )
        try:
            with pytest.raises(ManifestError, match="FMI 2.0"):
                group.on_init({**FAKE_INIT, "channels": {}})
        finally:
            group.close()


class TestExtraction:
    """The extracted files go after success and after failure."""

    def test_extraction_is_removed_after_success(
        self, fake, monkeypatch, tmp_path
    ):
        archive = fake()
        monkeypatch.chdir(tmp_path)
        participant = fake_participant(archive)
        participant.on_step(0, STEP_PERIOD_NS, [])
        assert list(tmp_path.glob("sil-fmu-*"))
        participant.close()

        assert list(tmp_path.glob("sil-fmu-*")) == []

    def test_extraction_is_removed_after_failure(
        self, fake, monkeypatch, tmp_path
    ):
        archive = fake("StepError")
        monkeypatch.chdir(tmp_path)
        participant = fake_participant(archive)
        with pytest.raises(ParticipantFailure):
            participant.on_step(0, STEP_PERIOD_NS, [])
        participant.close()

        assert list(tmp_path.glob("sil-fmu-*")) == []


def fake_manifest(archive: Path, duration_ns: int = 50_000_000) -> Manifest:
    """A stimulus participant feeding the fake FMU, which publishes back."""
    m = Manifest(duration_ns=duration_ns)
    m.add_schemas({
        "fmu.In": {"fields": [{"name": "value", "type": "f64"},
                              {"name": "flag", "type": "u8"}]},
        "fmu.Out": {"fields": [{"name": "y", "type": "f64"},
                               {"name": "c", "type": "u8"}]},
    })
    m.add_channel("fmu.In", schema="fmu.In")
    m.add_channel("fmu.Out", schema="fmu.Out")
    m.add_process(
        "source",
        command=[sys.executable,
                 str(ROOT / "tests" / "participants" / "scalar_stimulus.py"),
                 "fmu.In"],
        step_period_ns=STEP_PERIOD_NS, publishes=["fmu.In"],
    )
    m.add_process(
        "fake",
        command=[sys.executable, "-m", "sil.fmi", str(archive),
                 "--bind", "fmu.In:value=u", "--bind", "fmu.In:flag=b",
                 "--bind", "fmu.Out:y=y", "--bind", "fmu.Out:c=c"],
        step_period_ns=STEP_PERIOD_NS,
        subscribes=[SubscriberRoute("fmu.In", capacity=COMPAT_ROUTE_CAPACITY)],
        publishes=["fmu.Out"], priority=1,
    )
    return m


class TestRunBoundary:
    """The FMI 2.0 importer as a process participant in a complete Run."""

    def test_the_run_completes_with_one_step_of_latency(
        self, fake, sil_run, tmp_path
    ):
        result = run_simulation(
            fake_manifest(fake()), runner=sil_run, workdir=tmp_path
        )
        outputs = [(f["y"], f["c"]) for _, f in result.messages("fmu.Out")]
        inputs = [(f["value"], 1 - f["flag"])
                  for _, f in result.messages("fmu.In")]

        assert len(outputs) == 5
        assert outputs[1:] == inputs[:-1]

    @pytest.mark.parametrize("status", ["Discard", "Error", "Fatal", "Pending"])
    def test_a_status_failure_fails_the_run_with_the_call_named(
        self, fake, run_sil, tmp_path, status
    ):
        manifest = fake_manifest(fake(f"Step{status}"))
        proc = run_sil(manifest.write(tmp_path / f"{status}.json").path)

        assert proc.returncode == 1
        assert "participant 'fake' failed" in proc.stderr
        assert f"fmi2DoStep returned {status}" in proc.stderr

    def test_an_initialization_failure_fails_the_run(
        self, fake, run_sil, tmp_path
    ):
        manifest = fake_manifest(fake("InitializeError"))
        proc = run_sil(manifest.write(tmp_path / "init.json").path)

        assert proc.returncode == 1
        assert "fmi2ExitInitializationMode returned Error" in proc.stderr


def reference_manifest(fmu: Path, outputs: list[str], *, instances=1,
                       duration_ns: int = 1_000_000_000) -> Manifest:
    """One or more Reference FMU participants that only publish."""
    m = Manifest(duration_ns=duration_ns)
    m.add_schemas({"ref.Out": {"fields": [
        {"name": name, "type": "f64"} for name in outputs
    ]}})
    for index in range(instances):
        channel = f"ref.Out{index}"
        m.add_channel(channel, schema="ref.Out")
        m.add_process(
            f"ref{index}",
            command=[sys.executable, "-m", "sil.fmi", str(fmu)],
            step_period_ns=STEP_PERIOD_NS, publishes=[channel],
        )
    return m


@reference_binaries
class TestReferenceFmus:
    """The Reference FMUs' FMI 2.0 binaries, in complete Runs."""

    def test_dahlquist_decays(self, sil_run, tmp_path):
        result = run_simulation(
            reference_manifest(DAHLQUIST, ["x"]), runner=sil_run,
            workdir=tmp_path,
        )
        x = [fields["x"] for _, fields in result.messages("ref.Out0")]

        # The Reference FMU integrates x' = -k x with explicit Euler at its
        # fixed 0.1 s solver step, so x(1 s) is 0.9 ** 10 with k = 1.
        assert len(x) == 100
        assert all(later < earlier for earlier, later in zip(x[9::10], x[19::10]))
        assert x[-1] == pytest.approx(0.9 ** 10, rel=1e-12)

    def test_two_runs_record_identical_bytes(self, run_sil, tmp_path):
        manifest = reference_manifest(BOUNCING_BALL, ["h", "v"]).write(
            tmp_path / "ball.json"
        ).path
        first = run_sil(manifest, tmp_path / "1.mcap")
        second = run_sil(manifest, tmp_path / "2.mcap")

        assert first.returncode == 0, first.stderr
        assert second.returncode == 0, second.stderr
        assert (tmp_path / "1.mcap").read_bytes() == (
            tmp_path / "2.mcap"
        ).read_bytes()

    def test_two_instances_in_one_run_agree(self, sil_run, tmp_path):
        result = run_simulation(
            reference_manifest(VAN_DER_POL, ["x0", "x1"], instances=2),
            runner=sil_run, workdir=tmp_path,
        )

        first = result.messages("ref.Out0")
        assert len(first) == 100
        assert first == result.messages("ref.Out1")

    def test_an_integer_output_runs(self, sil_run, tmp_path):
        m = Manifest(duration_ns=2_000_000_000)
        m.add_schemas({"stair.Out": {"fields": [
            {"name": "counter", "type": "i32"}]}})
        m.add_channel("stair.Out", schema="stair.Out")
        m.add_process(
            "stair",
            command=[sys.executable, "-m", "sil.fmi", str(STAIR),
                     "--bind", "stair.Out:counter=counter"],
            step_period_ns=200_000_000, publishes=["stair.Out"],
        )
        result = run_simulation(m, runner=sil_run, workdir=tmp_path)
        counter = [fields["counter"] for _, fields in
                   result.messages("stair.Out")]

        # One time event per second; a sample is the end of its Step.
        assert counter == [1, 1, 1, 1, 2, 2, 2, 2, 2, 3]
