"""The FMI 3.0 co-simulation importer, at the run boundary and in-process.

The conformance target is the Modelica Association's own Reference FMUs,
vendored under `tests/fixtures/reference-fmus/`. `Feedthrough` sets every
output equal to its input, so a Run over it proves the whole path: the
subscribed Channel is written into the FMU's input variables, the FMU is
stepped, and its output variables are published on the Channel it publishes.
"""

import sys

import pytest
from conftest import COMPAT_ROUTE_CAPACITY, ROOT
from sil.fmi import NS_PER_S, CoSimulation, FmuParticipant
from sil.manifest import Manifest, SubscriberRoute
from sil.participant import Input
from sil.testing import run_simulation

FIXTURES = ROOT / "tests" / "fixtures" / "reference-fmus" / "3.0"
FEEDTHROUGH = FIXTURES / "Feedthrough.fmu"
BOUNCING_BALL = FIXTURES / "BouncingBall.fmu"

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

# `BouncingBall` owns state and publishes it; its two outputs are named `h`
# and `v`, so those are the field names of the Channel it publishes.
BALL_SCHEMAS = {
    "ball.State": {
        "fields": [{"name": "h", "type": "f64"}, {"name": "v", "type": "f64"}]
    }
}
BALL_DURATION_NS = 1_000_000_000


def init_line(**directions: str) -> dict:
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
    participant.on_init(init_line(**{"fmu.In": "in", "fmu.Out": "out"}))
    yield participant
    participant.close()


def stimulus(step: int) -> dict:
    return {
        "Float64_continuous_input": float(step),
        "Float64_discrete_input": 100.0 - step,
    }


def fmu_manifest(duration_ns: int = DURATION_NS) -> Manifest:
    """A ramp source feeding the imported FMU, which publishes its outputs."""
    m = Manifest(duration_ns=duration_ns)
    m.add_schemas(FMI_SCHEMAS)
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
        command=[sys.executable, "-m", "sil.fmi", str(FEEDTHROUGH)],
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

    def test_an_output_only_fmu_needs_no_subscribed_channel(self):
        """Direction comes from the init line, so one side may be absent."""
        participant = FmuParticipant(FEEDTHROUGH)
        participant.on_init(init_line(**{"fmu.Out": "out"}))
        try:
            (channel, published), = participant.on_step(0, STEP_PERIOD_NS, [])
            assert channel == "fmu.Out"
            assert published["Float64_continuous_output"] == 0.0
        finally:
            participant.close()


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
        participant.on_init(init_line(**{"fmu.Out": "out"}))
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


def ball_manifest() -> Manifest:
    """One imported FMU with state, publishing its own trajectory."""
    m = Manifest(duration_ns=BALL_DURATION_NS)
    m.add_schemas(BALL_SCHEMAS)
    m.add_channel("ball.State", schema="ball.State")
    m.add_process(
        "ball",
        command=[sys.executable, "-m", "sil.fmi", str(BOUNCING_BALL)],
        step_period_ns=STEP_PERIOD_NS,
        publishes=["ball.State"],
    )
    return m


def test_a_stateful_fmu_is_really_stepped(sil_run, tmp_path):
    """`Feedthrough` computes its outputs from its inputs, so a Run over it
    would pass even if the instance were never advanced. `BouncingBall` only
    moves because `fmi3DoStep` advances it: the ball falls from its start
    height, stays above the ground, and rebounds."""
    result = run_simulation(ball_manifest(), runner=sil_run, workdir=tmp_path)
    heights = [fields["h"] for _, fields in result.messages("ball.State")]
    bounce = heights.index(min(heights))

    assert heights[:bounce] == sorted(heights[:bounce], reverse=True)
    assert min(heights) > 0.0
    assert max(heights[bounce:]) > 10 * heights[bounce]
