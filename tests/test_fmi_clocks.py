"""A clocked FMU through the importer: Event Mode, Clocks, and event times.

The target is the FMI-LS-BUS CAN node of `proofs/fmi-ls-bus/`: an FMU whose
whole output is a Binary buffer gated by a triggered Clock, readable only
inside an event. Its archive is built by the proof inside a pinned container
and is not vendored here, so what these tests drive is the node's own
declarations — read back from the fixture's `evidence/profile.json` — around a
stand-in built from `tests/fixtures/fmi_clock.c`, which reproduces the
behavior `proofs/fmi-ls-bus/expected.json` states.

The expectations are that file's, not this one's. It was written from
upstream's sources before any Run, and an independent FMI 3.0 importer is
recorded matching it in `evidence/reference-exchange.json`; these tests assert
that the SiL importer produces the same operations, in the same order, at the
same event times, on the same two step grids.

Three times are involved and only the first is the FMU's:

- the **FMI event time**: the communication point the activation was observed
  at, carried in each Message's `<field>_event_time_ns`;
- the **publication time**: the Slot the importer was activated in, which is
  where the Recording timestamps the Message;
- the **delivery time**: one Latency later, which is the subscriber's.

A 100 ms grid observes the node's 300 ms transmit at 300 ms and publishes it
in the Slot at 200 ms, because the Step from 200 ms to 300 ms is where it
became visible. Nothing is quantised to the Slot and no Latency is added.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from can_node import (
    CAN_BUFFER_BYTES,
    RX_CLOCK,
    RX_DATA,
    TX_CLOCK,
    TX_DATA,
    node_fmu,
)
from conftest import COMPAT_ROUTE_CAPACITY, ROOT
from sil.fmi import CoSimulation, FmuParticipant, library_suffix, platform_directory
from sil.manifest import Manifest, SubscriberRoute
from sil.participant import Input, ManifestError, ParticipantFailure
from sil.testing import run_simulation

PROOF = ROOT / "proofs" / "fmi-ls-bus"
EXPECTED = json.loads((PROOF / "expected.json").read_text())


def _load(name: str, path: Path):
    """Import a module that lives outside the importable packages.

    It is registered before it is executed because a dataclass defined in it
    resolves its own annotations through `sys.modules`.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# The fixture's own decoder, so an operation is named here the way the
# acceptance fixture names it rather than by a second reading of the headers.
decode = _load("can_operations", PROOF / "can_operations.py").decode

TX = "can.Tx"
RX = "can.Rx"
BUFFER_SCHEMA = "can.Buffer"

# The bounded clocked representation: the length used, the payload the Channel
# admits, and the FMI event time of the activation carrying it. One schema,
# carried by both Channels, as the fixture's terminal declares them.
SCHEMAS = {
    BUFFER_SCHEMA: {"fields": [
        {"name": "data_length", "type": "u16"},
        {"name": "data", "type": "u8", "count": CAN_BUFFER_BYTES},
        {"name": "data_event_time_ns", "type": "u64"},
    ]}
}
CHANNELS = {TX: (BUFFER_SCHEMA, "out"), RX: (BUFFER_SCHEMA, "in")}
BINDINGS = [f"{TX}:data={TX_DATA}", f"{RX}:data={RX_DATA}"]
TX_ONLY = [f"{TX}:data={TX_DATA}"]

MS = 1_000_000


def init_line(channels: dict[str, tuple[str, str]] = CHANNELS,
              schemas: dict = SCHEMAS) -> dict:
    """The initialization line the kernel would send for these Channels."""
    return {
        "op": "init",
        "name": "importer",
        "schemas": schemas,
        "channels": {
            channel: {"schema": schema, "direction": direction}
            for channel, (schema, direction) in channels.items()
        },
    }


def clocked_fmu(tmp_path: Path, build_dir: Path, variant: str = "CanNode",
                **packaging) -> Path:
    """The node's declarations around one built stand-in."""
    identifier = f"Clocked{variant}"
    binary = build_dir / f"{identifier}{library_suffix()}"
    assert binary.exists(), f"clocked FMU binary was not built at {binary}"
    return node_fmu(
        tmp_path / f"{identifier}.fmu",
        model_identifier=identifier,
        binary=binary,
        platform_directory=platform_directory(),
        **packaging,
    )


@pytest.fixture
def importer(tmp_path, build_dir):
    """Build an initialized importer over a clocked FMU, torn down after."""
    built = []

    def _build(*, variant="CanNode", binds=BINDINGS, starts=(),
               channels=CHANNELS, schemas=SCHEMAS, fmu=None, **packaging):
        participant = FmuParticipant(
            fmu or clocked_fmu(tmp_path, build_dir, variant, **packaging),
            binds=list(binds), starts=list(starts),
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


def frame(payload: bytes, event_time_ns: int = 0) -> dict:
    """One Message carrying a payload on a Channel of the node's bound."""
    return {
        "data_length": len(payload),
        "data": payload.ljust(CAN_BUFFER_BYTES, b"\x00"),
        "data_event_time_ns": event_time_ns,
    }


def observed(participant, step_size_ns: int, duration_ns: int) -> list[dict]:
    """Drive one step grid, and state every Message the way the fixture does.

    The shape is `expected.json`'s own event shape, so the comparison is
    against the fixture's record rather than against a restatement of it.
    """
    events = []
    for t in range(0, duration_ns, step_size_ns):
        for channel, fields in participant.on_step(t, step_size_ns, []):
            assert channel == TX
            payload = fields["data"][:fields["data_length"]]
            events.append({
                "published_ns": t,
                "time_ns": fields["data_event_time_ns"],
                "payload_hex": payload.hex(),
                "operations": [
                    {"name": operation.name, "fields": operation.fields}
                    for operation in decode(payload)
                ],
            })
    return events


def case(name: str) -> dict:
    """One step grid of the acceptance fixture's expected exchange."""
    for declared in EXPECTED["cases"]:
        if declared["name"] == name:
            return declared
    raise AssertionError(f"the fixture states no case {name!r}")


def expected_events(name: str) -> list[dict]:
    """The expected events, less the one field a Message does not carry.

    `next_event_time_s` is what `fmi3UpdateDiscreteStates` declared when the
    event ended, and no Channel carries it. What the importer does with a
    declared next event time is asserted where it belongs — it refuses to
    step past one — so the expectation is required to declare none rather
    than compared against a null these tests wrote themselves.
    """
    events = []
    for event in case(name)["events"]:
        assert event["next_event_time_s"] is None, (
            f"the event at {event['time_ns']} ns expects a next event time, "
            f"which no Channel carries"
        )
        events.append({
            key: value for key, value in event.items()
            if key != "next_event_time_s"
        })
    return events


class TestTheExpectedExchange:
    """Every operation the fixture expects, at the event time it expects it.

    Payloads, counts, ordering and event times are compared whole: a frame
    that arrives with the right bytes at the wrong communication point is a
    mismatch, and so is one that arrives twice.
    """

    @pytest.mark.parametrize("name", ["aligned", "quantised"])
    def test_the_exchange_matches_the_fixture(self, importer, name):
        declared = case(name)
        events = observed(
            importer(binds=TX_ONLY, channels={TX: (BUFFER_SCHEMA, "out")}),
            declared["step_size_ns"], declared["duration_ns"],
        )
        assert [
            {key: value for key, value in event.items() if key != "published_ns"}
            for event in events
        ] == expected_events(declared["name"])

    def test_an_event_is_published_in_the_slot_it_became_visible_in(
        self, importer
    ):
        """The Slot is not the event time, and neither is quantised.

        On the aligned grid the node's 300 ms frame is observed at the
        communication point 300 ms and published in the Slot that ends there,
        which is the Step that started at 200 ms.
        """
        declared = case("aligned")
        events = observed(
            importer(binds=TX_ONLY, channels={TX: (BUFFER_SCHEMA, "out")}),
            declared["step_size_ns"], declared["duration_ns"],
        )
        assert [(e["published_ns"], e["time_ns"]) for e in events] == [
            (0, 0), (200 * MS, 300 * MS), (500 * MS, 600 * MS),
            (800 * MS, 900 * MS),
        ]

    def test_the_initialization_event_is_published_in_the_first_slot(
        self, importer
    ):
        """The FMU's initial time is virtual time zero, and the event that
        ends initialization belongs to it — there is no Slot before it."""
        participant = importer(
            binds=TX_ONLY, channels={TX: (BUFFER_SCHEMA, "out")}
        )
        (channel, fields), = participant.on_step(0, 100 * MS, [])
        assert channel == TX
        assert fields["data_event_time_ns"] == 0
        assert [operation.name for operation in decode(
            fields["data"][:fields["data_length"]]
        )] == ["Configuration", "Configuration"]

    def test_several_operations_share_one_activation(self, importer):
        """A Step that crosses three transmit boundaries carries three
        operations in the one buffer its Clock activation gates."""
        participant = importer(
            binds=TX_ONLY, channels={TX: (BUFFER_SCHEMA, "out")}
        )
        published = participant.on_step(0, 1000 * MS, [])
        # The initialization event first, then the one this Step ended in.
        assert [fields["data_event_time_ns"] for _, fields in published] == [
            0, 1000 * MS
        ]
        transmits = published[-1][1]
        assert transmits["data_event_time_ns"] == 1000 * MS
        assert [operation.name for operation in decode(
            transmits["data"][:transmits["data_length"]]
        )] == ["CanTransmit"] * 3

    def test_every_step_that_ends_on_a_boundary_activates_once(self, importer):
        """A grid whose every Step ends on a transmit boundary: one
        activation per Step, and the initialization event beside the first.

        A missed activation makes this list short and a duplicated one makes
        it long, which is what a Clock cleared on the read costs an importer
        that reads it twice or not at all.
        """
        events = observed(
            importer(binds=TX_ONLY, channels={TX: (BUFFER_SCHEMA, "out")}),
            300 * MS, 1200 * MS,
        )
        assert [(e["published_ns"], e["time_ns"]) for e in events] == [
            (0, 0), (0, 300 * MS), (300 * MS, 600 * MS),
            (600 * MS, 900 * MS), (900 * MS, 1200 * MS),
        ]

    def test_an_activation_raised_by_the_last_update_is_not_lost(
        self, importer
    ):
        """The Clock can go up in the discrete-state update that ends the
        event, and an importer that stops reading there loses it for good —
        the buffer it gates is defined only while it is up.

        This build raises its transmit Clock exactly there, and the exchange
        it produces is the one the fixture expects either way.
        """
        declared = case("aligned")
        events = observed(
            importer(variant="LateActivation", binds=TX_ONLY,
                     channels={TX: (BUFFER_SCHEMA, "out")}),
            declared["step_size_ns"], declared["duration_ns"],
        )
        assert [
            {key: value for key, value in event.items() if key != "published_ns"}
            for event in events
        ] == expected_events(declared["name"])

    def test_two_events_can_share_one_slot(self, importer):
        """The initialization event and the first Step's event are published
        together, at the same publication time and with different event
        times: a Burst of two Messages on one Channel in one Slot."""
        participant = importer(
            binds=TX_ONLY, channels={TX: (BUFFER_SCHEMA, "out")}
        )
        published = participant.on_step(0, 300 * MS, [])
        assert [channel for channel, _ in published] == [TX, TX]
        assert [fields["data_event_time_ns"] for _, fields in published] == [
            0, 300 * MS
        ]


class TestTheClockProfile:
    """The lifecycle itself: which calls, in which order, how many times."""

    def test_initialization_ends_in_event_mode_and_then_step_mode(
        self, importer, monkeypatch, tmp_path, build_dir
    ):
        calls = record_calls(monkeypatch)
        importer(binds=TX_ONLY, channels={TX: (BUFFER_SCHEMA, "out")})
        assert calls == [
            "fmi3EnterInitializationMode",
            "fmi3ExitInitializationMode",
            "fmi3GetClock",
            "fmi3GetBinary",
            "fmi3UpdateDiscreteStates",
            # Once more, because the update that ends an event can raise a
            # Clock of its own.
            "fmi3GetClock",
            "fmi3EnterStepMode",
        ]

    def test_a_clock_is_read_once_per_discrete_state_iteration(
        self, importer, monkeypatch
    ):
        """An FMU clears its output Clock on the read, so reading it twice
        would lose the activation and reading it none would invent one."""
        participant = importer(
            binds=TX_ONLY, channels={TX: (BUFFER_SCHEMA, "out")}
        )
        calls = record_calls(monkeypatch)
        participant.on_step(0, 300 * MS, [])
        assert calls == [
            # The Step that ends on the node's first transmit.
            "fmi3DoStep", "fmi3EnterEventMode",
            "fmi3GetClock", "fmi3GetBinary", "fmi3UpdateDiscreteStates",
            "fmi3GetClock",
            "fmi3EnterStepMode",
        ]

    def test_an_inactive_clock_leaves_its_buffer_unread(
        self, importer, monkeypatch
    ):
        """The buffer is defined only while the Clock is active, so a Step
        without an event reads neither."""
        participant = importer(
            binds=TX_ONLY, channels={TX: (BUFFER_SCHEMA, "out")}
        )
        participant.on_step(0, 100 * MS, [])  # the initialization event
        calls = record_calls(monkeypatch)
        assert participant.on_step(100 * MS, 100 * MS, []) == []
        assert calls == ["fmi3DoStep"]

    def test_an_activation_arriving_on_a_channel_is_handed_over_before_the_step(
        self, importer, monkeypatch
    ):
        """An input Clock is raised at the communication point the FMU stands
        on, which is the Slot's own time rather than the end of the Step."""
        participant = importer()
        participant.on_step(0, 100 * MS, [])
        calls = record_calls(monkeypatch)
        published = participant.on_step(
            100 * MS, 100 * MS, [Input(RX, 100 * MS, frame(b"\x01\x02"))]
        )
        assert calls[:6] == [
            "fmi3EnterEventMode", "fmi3SetBinary", "fmi3SetClock",
            # The echo the FMU prepares is visible on the next iteration,
            # which is why the Clock is read again rather than once.
            "fmi3GetClock", "fmi3UpdateDiscreteStates", "fmi3GetClock",
        ]
        assert [fields["data_event_time_ns"] for _, fields in published] == [
            100 * MS
        ]
        assert [fields["data_length"] for _, fields in published] == [2]

    def test_each_message_of_a_burst_is_its_own_activation(self, importer):
        """Two frames delivered in one Slot are two activations of one Clock,
        and merging them would lose one."""
        participant = importer()
        participant.on_step(0, 100 * MS, [])
        published = participant.on_step(100 * MS, 100 * MS, [
            Input(RX, 100 * MS, frame(b"\x01")),
            Input(RX, 100 * MS, frame(b"\x02\x03")),
        ])
        assert [
            fields["data"][:fields["data_length"]] for _, fields in published
        ] == [b"\x01", b"\x02\x03"]

    def test_an_incoming_event_time_is_not_the_one_the_clock_is_raised_at(
        self, importer
    ):
        """A Message states the event time it was produced at; this importer
        activates the Clock where the FMU stands and says so."""
        participant = importer()
        participant.on_step(0, 100 * MS, [])
        (_, fields), = participant.on_step(
            100 * MS, 100 * MS,
            [Input(RX, 100 * MS, frame(b"\x07", event_time_ns=42))],
        )
        assert fields["data_event_time_ns"] == 100 * MS


def record_calls(monkeypatch) -> list[str]:
    """Every co-simulation entry point the importer calls, in order."""
    calls: list[str] = []
    called = CoSimulation._call

    def record(self, name, *arguments):
        calls.append(name)
        return called(self, name, *arguments)

    monkeypatch.setattr(CoSimulation, "_call", record)
    return calls


class TestUndrivableBehavior:
    """What the importer refuses to claim it handled."""

    def test_an_event_that_never_converges_fails_at_a_declared_bound(
        self, importer
    ):
        """A bounded failure rather than a Run held until its deadline."""
        with pytest.raises(ParticipantFailure, match="bounds the iteration"):
            importer(
                variant="NeverConverges", binds=TX_ONLY,
                channels={TX: (BUFFER_SCHEMA, "out")},
            )

    def test_a_declared_next_event_time_inside_the_step_fails(self, importer):
        """The kernel owns the Slot grid, so an FMU that asks to be stepped
        onto its own event is told that cannot be promised."""
        participant = importer(
            variant="NextEvent", binds=TX_ONLY,
            channels={TX: (BUFFER_SCHEMA, "out")},
        )
        with pytest.raises(
            ParticipantFailure, match="does not choose communication points"
        ):
            participant.on_step(0, 100 * MS, [])

    def test_a_next_event_time_on_the_communication_point_is_not_refused(
        self, importer
    ):
        """An FMU that declares its next event exactly where the Step ends is
        asking for nothing the grid does not already do.

        The declared time is a double of seconds and the interval is integer
        nanoseconds; comparing them in nanoseconds is what keeps the last bit
        of one representation from aborting a Run the other admits.
        """
        participant = importer(
            variant="NextEventAtBoundary", binds=TX_ONLY,
            channels={TX: (BUFFER_SCHEMA, "out")},
        )
        participant.on_step(0, 100 * MS, [])

    def test_a_next_event_time_in_a_later_step_fails_at_that_step(
        self, importer
    ):
        """It is the Step that would pass the declared event that fails, not
        the first Step after it was declared."""
        participant = importer(
            variant="NextEventLater", binds=TX_ONLY,
            channels={TX: (BUFFER_SCHEMA, "out")},
        )
        for t in range(0, 500 * MS, 100 * MS):
            participant.on_step(t, 100 * MS, [])
        with pytest.raises(
            ParticipantFailure, match="does not choose communication points"
        ):
            participant.on_step(500 * MS, 100 * MS, [])

    def test_an_early_return_is_not_a_completed_interval(self, importer):
        participant = importer(
            variant="EarlyReturn", binds=TX_ONLY,
            channels={TX: (BUFFER_SCHEMA, "out")},
        )
        with pytest.raises(ParticipantFailure, match="returned early"):
            participant.on_step(0, 100 * MS, [])

    def test_a_termination_request_inside_an_event_aborts(self, importer):
        with pytest.raises(
            ParticipantFailure, match="fmi3UpdateDiscreteStates requested"
        ):
            importer(
                variant="TerminateInEvent", binds=TX_ONLY,
                channels={TX: (BUFFER_SCHEMA, "out")},
            )

    def test_a_gap_between_activations_is_refused(self, importer):
        """An interval the FMU never covered would date every event after it
        at an instant the FMU never reached."""
        participant = importer(
            binds=TX_ONLY, channels={TX: (BUFFER_SCHEMA, "out")}
        )
        participant.on_step(0, 100 * MS, [])
        with pytest.raises(
            ParticipantFailure, match="contiguous intervals"
        ):
            participant.on_step(300 * MS, 100 * MS, [])

    def test_an_fmu_that_requires_event_mode_refuses_an_unclocked_run(
        self, tmp_path, build_dir
    ):
        """With no Clock in the Manifest the importer uses no Event Mode, and
        this FMU — like the fixture's node — refuses to instantiate.

        That is the failure `proofs/fmi-ls-bus/evidence/sil-no-channels.txt`
        records against the released importer, reproduced here as the cost of
        declaring nothing clocked.
        """
        participant = FmuParticipant(clocked_fmu(tmp_path, build_dir))
        try:
            with pytest.raises(
                ParticipantFailure, match="returned no instance"
            ):
                participant.on_init(init_line({}, {}))
        finally:
            participant.close()


class TestMappingsRejectedBeforeStepping:
    """A Channel that cannot carry an activation is a Manifest error."""

    def reject(self, tmp_path, build_dir, **arguments) -> str:
        participant = FmuParticipant(
            clocked_fmu(tmp_path, build_dir, **arguments.pop("packaging", {})),
            binds=list(arguments.pop("binds", TX_ONLY)),
        )
        try:
            with pytest.raises(ManifestError) as rejected:
                participant.on_init(init_line(**arguments))
        finally:
            participant.close()
        return str(rejected.value)

    def test_a_channel_without_an_event_time_field_is_rejected(
        self, tmp_path, build_dir
    ):
        schemas = {BUFFER_SCHEMA: {"fields": [
            {"name": "data_length", "type": "u16"},
            {"name": "data", "type": "u8", "count": CAN_BUFFER_BYTES},
        ]}}
        assert "data_event_time_ns" in self.reject(
            tmp_path, build_dir,
            channels={TX: (BUFFER_SCHEMA, "out")}, schemas=schemas,
        )

    def test_an_event_time_field_of_the_wrong_type_is_rejected(
        self, tmp_path, build_dir
    ):
        schemas = {BUFFER_SCHEMA: {"fields": [
            {"name": "data_length", "type": "u16"},
            {"name": "data", "type": "u8", "count": CAN_BUFFER_BYTES},
            {"name": "data_event_time_ns", "type": "f64"},
        ]}}
        assert "nanoseconds" in self.reject(
            tmp_path, build_dir,
            channels={TX: (BUFFER_SCHEMA, "out")}, schemas=schemas,
        )

    def test_a_field_beside_the_activation_is_rejected(
        self, tmp_path, build_dir
    ):
        """One Channel carries one activation and nothing else: a Step's
        value published on an event's Message would be dated by it."""
        schemas = {BUFFER_SCHEMA: {"fields": [
            {"name": "data_length", "type": "u16"},
            {"name": "data", "type": "u8", "count": CAN_BUFFER_BYTES},
            {"name": "data_event_time_ns", "type": "u64"},
            {"name": "spare", "type": "f64"},
        ]}}
        assert "a clocked payload does not carry" in self.reject(
            tmp_path, build_dir,
            channels={TX: (BUFFER_SCHEMA, "out")}, schemas=schemas,
        )

    def test_a_clock_of_another_interval_variability_is_rejected(
        self, tmp_path, build_dir
    ):
        """The bus simulation FMU's Clocks are `countdown`, which asks an
        importer to read an interval and schedule the next activation."""
        stopped = self.reject(
            tmp_path, build_dir,
            packaging={"rewrite": lambda text: text.replace(
                'intervalVariability="triggered"',
                'intervalVariability="countdown"',
            )},
            channels={TX: (BUFFER_SCHEMA, "out")},
        )
        assert "'countdown'" in stopped
        assert "'triggered'" in stopped

    def test_an_fmu_without_event_mode_is_rejected(self, tmp_path, build_dir):
        stopped = self.reject(
            tmp_path, build_dir,
            packaging={"rewrite": lambda text: text.replace(
                'hasEventMode="true"', 'hasEventMode="false"'
            )},
            channels={TX: (BUFFER_SCHEMA, "out")},
        )
        assert "hasEventMode=false" in stopped

    def test_a_clock_gating_the_other_causality_is_rejected(
        self, tmp_path, build_dir
    ):
        """A `clocks` attribute naming the input Clock on an output variable
        describes an FMU neither end of the mapping can drive."""
        stopped = self.reject(
            tmp_path, build_dir,
            packaging={"rewrite": lambda text: text.replace(
                f'name="{TX_DATA}"', f'name="{TX_DATA}" clocks="2"'
            ).replace(' clocks="3"', "")},
            channels={TX: (BUFFER_SCHEMA, "out")},
        )
        assert RX_CLOCK in stopped
        assert "causality" in stopped

    def test_a_clocked_channel_binding_a_second_variable_is_rejected(
        self, tmp_path, build_dir
    ):
        schemas = {BUFFER_SCHEMA: {"fields": [
            {"name": "data_length", "type": "u16"},
            {"name": "data", "type": "u8", "count": CAN_BUFFER_BYTES},
            {"name": "data_event_time_ns", "type": "u64"},
            {"name": "notify", "type": "u8"},
        ]}}
        stopped = self.reject(
            tmp_path, build_dir,
            binds=[*TX_ONLY,
                   f"{TX}:notify=org.fmi_standard.fmi_ls_bus.Can_BusNotifications"],
            channels={TX: (BUFFER_SCHEMA, "out")}, schemas=schemas,
        )
        assert "and nothing else" in stopped

    def test_a_clock_bound_to_a_field_of_its_own_is_rejected(
        self, tmp_path, build_dir
    ):
        stopped = self.reject(
            tmp_path, build_dir,
            binds=[f"{TX}:data={TX_CLOCK}"],
            channels={TX: (BUFFER_SCHEMA, "out")},
        )
        assert "Clock is driven through the variable it gates" in stopped


OBSERVER = ROOT / "tests" / "participants" / "can_observer.py"
STIMULUS = ROOT / "tests" / "participants" / "can_stimulus.py"

# One activation per Step on a route drained every Step, the Latency's own,
# and the two the first Slot carries when a Step ends on the initial event.
ROUTE_CAPACITY = 8


def can_manifest(fmu: Path, *, step_period_ns: int, duration_ns: int,
                 stimulus: bool = False) -> Manifest:
    """The node published on a bounded CAN Channel, and read back."""
    m = Manifest(duration_ns=duration_ns)
    m.add_schemas(SCHEMAS)
    m.add_channel(TX, schema=BUFFER_SCHEMA)
    binds = TX_ONLY
    if stimulus:
        binds = BINDINGS
        m.add_channel(RX, schema=BUFFER_SCHEMA)
        m.add_process(
            "stimulus",
            command=[sys.executable, str(STIMULUS), RX],
            step_period_ns=step_period_ns,
            publishes=[RX],
        )
    m.add_process(
        "importer",
        command=[
            sys.executable, "-m", "sil.fmi", str(fmu),
            *(argument for bind in binds for argument in ("--bind", bind)),
        ],
        step_period_ns=step_period_ns,
        subscribes=(
            [SubscriberRoute(RX, capacity=ROUTE_CAPACITY)] if stimulus else []
        ),
        publishes=[TX],
        priority=1,
    )
    # A published Channel with no subscriber describes a Run nobody would
    # execute; the observer is what a consumer of the node's frames is.
    m.add_process(
        "observer",
        command=[sys.executable, str(OBSERVER)],
        step_period_ns=step_period_ns,
        subscribes=[SubscriberRoute(TX, capacity=ROUTE_CAPACITY)],
        priority=2,
    )
    return m


@pytest.fixture(scope="module")
def aligned_result(sil_run, tmp_path_factory, build_dir):
    """One Run on the fixture's aligned grid, shared by every assertion."""
    workdir = tmp_path_factory.mktemp("fmi-clocks")
    declared = case("aligned")
    return run_simulation(
        can_manifest(
            clocked_fmu(workdir, build_dir),
            step_period_ns=declared["step_size_ns"],
            duration_ns=declared["duration_ns"],
        ),
        runner=sil_run, workdir=workdir,
    )


class TestRunBoundary:
    """The clocked importer as a process participant in a complete Run."""

    def test_the_recording_carries_the_expected_exchange(self, aligned_result):
        published = aligned_result.messages(TX)
        assert [
            {
                "time_ns": fields["data_event_time_ns"],
                "payload_hex": fields["data"][:fields["data_length"]].hex(),
                "operations": [
                    {"name": operation.name, "fields": operation.fields}
                    for operation in decode(
                        fields["data"][:fields["data_length"]]
                    )
                ],
            }
            for _, fields in published
        ] == expected_events("aligned")

    def test_the_recording_timestamps_the_slot_and_states_the_event_time(
        self, aligned_result
    ):
        """The Recording's own timestamp is the publication time; the event
        time is in the Message, and the two differ by the Step the activation
        became visible in."""
        assert [
            (published_ns, fields["data_event_time_ns"])
            for published_ns, fields in aligned_result.messages(TX)
        ] == [(0, 0), (200 * MS, 300 * MS), (500 * MS, 600 * MS),
              (800 * MS, 900 * MS)]

    def test_the_determinism_check_passes_for_a_clocked_run(
        self, sil_run, tmp_path, build_dir
    ):
        """Two Runs of one Manifest, bit-compared."""
        declared = case("aligned")
        ref = can_manifest(
            clocked_fmu(tmp_path, build_dir),
            step_period_ns=declared["step_size_ns"],
            duration_ns=declared["duration_ns"],
        ).write(tmp_path / "clocked.json")
        proc = subprocess.run(
            [sys.executable, "-m", "sil.check", str(ref.path),
             "--runner", str(sil_run)],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.startswith("deterministic: ")

    def test_a_frame_published_into_the_node_comes_back_out(
        self, sil_run, tmp_path, build_dir
    ):
        """The receiving half: a Message on the subscribed Channel is one
        activation of the node's input Clock, and the stand-in echoes it.

        The grid stays inside the node's first 300 ms transmit interval, so
        every activation on the way out is an echo of one on the way in.
        """
        period = 50 * MS
        result = run_simulation(
            can_manifest(
                clocked_fmu(tmp_path, build_dir),
                step_period_ns=period, duration_ns=250 * MS, stimulus=True,
            ),
            runner=sil_run, workdir=tmp_path,
        )
        echoed = [
            (published_ns, fields["data"][:fields["data_length"]],
             fields["data_event_time_ns"])
            for published_ns, fields in result.messages(TX)
        ]
        stimulus = [
            (published_ns, fields["data"][:fields["data_length"]])
            for published_ns, fields in result.messages(RX)
        ]
        # The Channel's default Latency holds each frame back one Slot, and
        # the echo is published in the Slot it was delivered in.
        assert [
            (published_ns, payload) for published_ns, payload, _ in echoed
        ][1:] == [
            (published_ns + period, payload)
            for published_ns, payload in stimulus[:-1]
        ]
        assert all(
            event_time_ns == published_ns
            for published_ns, payload, event_time_ns in echoed[1:]
        )

    def test_an_unhandled_clock_profile_is_a_manifest_error(
        self, run_sil, tmp_path, build_dir
    ):
        """The exit-code taxonomy: a Clock this importer does not drive is a
        rejected Manifest, not a failed Run."""
        fmu = clocked_fmu(
            tmp_path, build_dir,
            rewrite=lambda text: text.replace(
                'intervalVariability="triggered"',
                'intervalVariability="countdown"',
            ),
        )
        manifest = can_manifest(
            fmu, step_period_ns=100 * MS, duration_ns=300 * MS
        ).write(tmp_path / "countdown.json")
        proc = run_sil(manifest.path)
        assert proc.returncode == 2, proc.stderr
        assert "countdown" in proc.stderr

    def test_an_event_that_never_converges_is_a_run_failure(
        self, run_sil, tmp_path, build_dir
    ):
        fmu = clocked_fmu(tmp_path, build_dir, "NeverConverges")
        manifest = can_manifest(
            fmu, step_period_ns=100 * MS, duration_ns=300 * MS
        ).write(tmp_path / "never-converges.json")
        proc = run_sil(manifest.path)
        assert proc.returncode == 1, proc.stderr
        assert "bounds the iteration" in proc.stderr


class TestLegacyLifecycle:
    """An FMU with no Clock keeps the Step-only lifecycle it always had."""

    def test_an_unclocked_run_never_enters_event_mode(self, monkeypatch):
        from test_fmi import FEEDTHROUGH, FMI_SCHEMAS

        calls = record_calls(monkeypatch)
        participant = FmuParticipant(FEEDTHROUGH)
        participant.on_init({
            "op": "init", "name": "fmu", "schemas": FMI_SCHEMAS,
            "channels": {
                "fmu.In": {"schema": "fmu.In", "direction": "in"},
                "fmu.Out": {"schema": "fmu.Out", "direction": "out"},
            },
        })
        try:
            participant.on_step(0, 10 * MS, [])
        finally:
            participant.close()
        assert "fmi3EnterEventMode" not in calls
        assert "fmi3GetClock" not in calls
