"""Two CAN nodes through a bus FMU, in one deterministic Run.

The target is the acceptance fixture of `proofs/fmi-ls-bus/`: two instances of
the upstream CAN node attached to the two network terminals of the upstream CAN
bus simulation FMU. Neither archive is vendored here — both are built by
`proofs/fmi-ls-bus/run-proof.sh` inside a pinned container — so what these tests
drive is the two FMUs' own declarations, read back from the fixture's
`evidence/profile.json`, around stand-ins built from `tests/fixtures/fmi_clock.c`
and `tests/fixtures/fmi_bus.c`, which reproduce the behavior upstream's sources
state.

The expectations are `proofs/fmi-ls-bus/connected_expected.json`'s, written from
those sources before any Run. What it states, and what these tests hold the
importer to, is a timing case no arrangement of separately stepped participants
reproduces:

- both nodes offer a frame of CAN ID 1 at the same instant;
- the bus transmits one of them, answering its originator with a `Confirm`
  operation and handing the frame to the other node, **480 us** later — the
  exact time (44 + 4) bits take at 100 000 bit/s;
- it transmits the second frame 480 us after that, the other way round.

480 us is no Slot of any step period these tests declare. The group is what
stops there, and the Messages it publishes state that instant while being
published in the Slot the importer's activation runs in. Three times again,
kept apart: the FMI event time in the Message, the publication time the
Recording stamps, and the delivery time one Latency later.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest
from can_fixture import (
    CAN_BUFFER_BYTES,
    CAN_PROFILE,
    bus_fmu,
    node_fmu,
)
from conftest import ROOT
from sil.fmi import CoSimulation, FmuGroupParticipant, library_suffix, platform_directory
from sil.manifest import Manifest, SubscriberRoute
from sil.participant import Input, ManifestError, ParticipantFailure
from sil.testing import RunFailure, run_simulation

PROOF = ROOT / "proofs" / "fmi-ls-bus"
EXPECTED = json.loads((PROOF / "connected_expected.json").read_text())

MS = 1_000_000
US = 1_000


def _load(name: str, path: Path):
    """Import a module that lives outside the importable packages."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# The fixture's own decoder, so an operation is named here the way the
# acceptance fixture names it rather than by a second reading of the headers.
_can_operations = _load("can_operations", PROOF / "can_operations.py")
decode = _can_operations.decode
HEADER_SIZE = _can_operations.HEADER_SIZE

BUFFER_SCHEMA = "can.Buffer"
SCHEMAS = {
    BUFFER_SCHEMA: {"fields": [
        {"name": "data_length", "type": "u16"},
        {"name": "data", "type": "u8", "count": CAN_BUFFER_BYTES},
        {"name": "data_event_time_ns", "type": "u64"},
    ]}
}

# One Channel per terminal whose activations the Run observes, named after the
# terminal, so the Recording is read in the expectation's own vocabulary.
SOURCES = {
    "can.node1.Tx": "node1.CanChannel",
    "can.node2.Tx": "node2.CanChannel",
    "can.bus.Node1": "bus.Node1",
    "can.bus.Node2": "bus.Node2",
}
OBSERVED = {channel: (BUFFER_SCHEMA, "out") for channel in SOURCES}
VARIABLES = {
    "can.node1.Tx": "node1.CanChannel.Tx_Data",
    "can.node2.Tx": "node2.CanChannel.Tx_Data",
    "can.bus.Node1": "bus.Node1.Tx_Data",
    "can.bus.Node2": "bus.Node2.Tx_Data",
}
BINDINGS = [
    f"{channel}:data={variable}" for channel, variable in VARIABLES.items()
]

INSTANCES = ("node1", "node2", "bus")
CONNECTS = ["node1.CanChannel=bus.Node1", "node2.CanChannel=bus.Node2"]


def init_line(channels: dict[str, tuple[str, str]] = OBSERVED,
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


def built(build_dir: Path, tmp_path: Path, name: str, variant: str) -> Path:
    """One FMU of the fixture, packaged around a stand-in this build made."""
    binary = build_dir / f"Clocked{variant}{library_suffix()}"
    assert binary.exists(), f"stand-in FMU was not built at {binary}"
    package = bus_fmu if variant.startswith("CanBus") else node_fmu
    return package(
        tmp_path / f"{name}.fmu",
        model_identifier=f"Clocked{variant}",
        binary=binary,
        platform_directory=platform_directory(),
    )


@pytest.fixture
def group(tmp_path, build_dir, monkeypatch):
    """Build an initialized group of connected FMUs, torn down after.

    The working directory is the test's own, because the kernel starts a
    process participant in its Run working directory and the importer extracts
    every archive beneath wherever it was started.
    """
    monkeypatch.chdir(tmp_path)
    built_participants = []

    def _build(*, node="CanNodeOnABus", bus="CanBus", instances=INSTANCES,
               connects=CONNECTS, binds=BINDINGS, starts=(),
               channels=OBSERVED, schemas=SCHEMAS, profile=CAN_PROFILE,
               paths=None):
        archives = paths or {
            "node1": built(build_dir, tmp_path, "node1", node),
            "node2": built(build_dir, tmp_path, "node2", node),
            "bus": built(build_dir, tmp_path, "bus", bus),
        }
        participant = FmuGroupParticipant(
            [(name, str(archives[name])) for name in instances],
            connects=list(connects), binds=list(binds), starts=list(starts),
            profile=profile,
        )
        built_participants.append(participant)
        participant.on_init(init_line(channels, schemas))
        return participant

    _build.participants = built_participants
    yield _build
    for participant in built_participants:
        try:
            participant.close()
        except ParticipantFailure:
            pass


def observed(participant, step_size_ns: int, duration_ns: int) -> list[dict]:
    """Drive one step grid, and state every Message the way the fixture does."""
    events = []
    for t in range(0, duration_ns, step_size_ns):
        for channel, fields in participant.on_step(t, step_size_ns, []):
            payload = fields["data"][:fields["data_length"]]
            events.append({
                "time_ns": fields["data_event_time_ns"],
                "published_ns": t,
                "source": SOURCES[channel],
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


def calls(monkeypatch) -> list[tuple]:
    """Every co-simulation entry point the importer calls, with its arguments."""
    recorded: list[tuple] = []
    called = CoSimulation._call

    def record(self, name, *arguments):
        recorded.append((id(self), name, arguments))
        return called(self, name, *arguments)

    monkeypatch.setattr(CoSimulation, "_call", record)
    return recorded


class TestTheConnectedExchange:
    """Every operation the fixture expects, at the event time it expects it."""

    @pytest.mark.parametrize("name", ["aligned", "quantised"])
    def test_the_exchange_matches_what_was_stated_before_the_run(
        self, group, name
    ):
        declared = case(name)
        assert observed(
            group(), declared["step_size_ns"], declared["duration_ns"]
        ) == declared["events"]

    def test_a_frame_reaches_the_other_node_a_transmission_time_later(
        self, group
    ):
        """The case that fails under accidental next-activation delivery.

        Both nodes offer a frame at 300 ms and the bus transmits them 480 us
        apart, which is the exact time (44 + 4) bits take at 100 000 bit/s. A
        Channel between separately stepped participants would deliver both at
        the subscriber's next activation — one instant, 100 ms after the frames
        were offered, with nothing left to tell the two transmissions apart.
        """
        events = observed(group(), 100 * MS, 400 * MS)
        delivered = [
            (event["time_ns"], event["source"],
             event["operations"][0]["name"])
            for event in events if event["source"].startswith("bus.")
        ]
        assert delivered == [
            (300 * MS + 480 * US, "bus.Node1", "Confirm"),
            (300 * MS + 480 * US, "bus.Node2", "CanTransmit"),
            (300 * MS + 960 * US, "bus.Node1", "CanTransmit"),
            (300 * MS + 960 * US, "bus.Node2", "Confirm"),
        ]

    def test_the_event_time_is_not_the_publication_time(self, group):
        """The group's own instants reach the Recording as a stated time.

        Every Message of one Step is published in the Slot the importer's
        activation runs in; what it states is the instant the FMUs stood on,
        which is between two Slots for everything the bus transmits.
        """
        events = observed(group(), 100 * MS, 400 * MS)
        assert [
            (event["published_ns"], event["time_ns"]) for event in events
        ] == [
            (0, 0), (0, 0),
            (200 * MS, 300 * MS), (200 * MS, 300 * MS),
            (300 * MS, 300 * MS + 480 * US), (300 * MS, 300 * MS + 480 * US),
            (300 * MS, 300 * MS + 960 * US), (300 * MS, 300 * MS + 960 * US),
        ]

    def test_the_bus_transmits_nothing_before_both_nodes_configured_it(
        self, group
    ):
        """The initial event is the nodes' configuration, and nothing else."""
        events = observed(group(), 100 * MS, 100 * MS)
        assert [event["source"] for event in events] == [
            "node1.CanChannel", "node2.CanChannel"
        ]

    def test_a_run_of_the_same_group_produces_the_same_exchange(
        self, group, tmp_path, build_dir
    ):
        """Repeatability at the participant's own boundary."""
        first = observed(group(), 100 * MS, 400 * MS)
        second = observed(group(), 100 * MS, 400 * MS)
        assert first == second


class TestCoordination:
    """Where the group stops, and what it refuses to step past."""

    def test_every_instance_is_stepped_over_the_same_interval(
        self, group, monkeypatch
    ):
        """A Step nobody asked to be stopped inside is one interval for all."""
        recorded = calls(monkeypatch)
        participant = group()
        recorded.clear()
        participant.on_step(0, 100 * MS, [])
        steps = [
            (arguments[0], arguments[1])
            for _, name, arguments in recorded if name == "fmi3DoStep"
        ]
        assert steps == [(0.0, 0.1)] * 3

    def test_no_peer_is_advanced_beyond_an_event_one_instance_asked_for(
        self, group, monkeypatch
    ):
        """What makes rollback unnecessary, and none of these FMUs offers it.

        They declare `canGetAndSetFMUState` false, so an importer that stepped
        a peer past an event could not take it back. One node declares its next
        event at 50 ms, inside a 100 ms Step, and every instance stops there
        before any of them goes on.
        """
        recorded = calls(monkeypatch)
        participant = group(node="NextEvent")
        recorded.clear()
        participant.on_step(0, 100 * MS, [])
        steps = [
            (round(arguments[0], 9), round(arguments[1], 9))
            for _, name, arguments in recorded if name == "fmi3DoStep"
        ]
        # Every instance to 50 ms, and only then every instance to 100 ms.
        assert steps == [(0.0, 0.05)] * 3 + [(0.05, 0.05)] * 3

    def test_the_group_stops_at_the_transmission_time_it_was_told(
        self, group, monkeypatch
    ):
        recorded = calls(monkeypatch)
        participant = group()
        participant.on_step(0, 100 * MS, [])
        participant.on_step(100 * MS, 100 * MS, [])
        participant.on_step(200 * MS, 100 * MS, [])
        recorded.clear()
        participant.on_step(300 * MS, 100 * MS, [])
        steps = sorted({
            (round(arguments[0], 9), round(arguments[1], 9))
            for _, name, arguments in recorded if name == "fmi3DoStep"
        })
        assert steps == [(0.3, 0.00048), (0.30048, 0.00048), (0.30096, 0.09904)]

    def test_an_interval_that_is_no_whole_nanosecond_is_refused(self, group):
        """Nothing is quantised onto an instant the FMU did not ask for."""
        with pytest.raises(ParticipantFailure, match="no whole number of"):
            participant = group(bus="CanBusRaggedInterval")
            for t in range(0, 400 * MS, 100 * MS):
                participant.on_step(t, 100 * MS, [])

    def test_an_activation_asked_for_in_the_instant_it_names_is_refused(
        self, group
    ):
        """A countdown interval is counted from the event it was stated in."""
        with pytest.raises(ParticipantFailure, match="the group stands at"):
            participant = group(bus="CanBusInstantInterval")
            for t in range(0, 400 * MS, 100 * MS):
                participant.on_step(t, 100 * MS, [])

    def test_two_fmus_answering_each_other_at_one_instant_fail_at_a_bound(
        self, group, tmp_path, build_dir
    ):
        """A bounded failure rather than an instant that never ends.

        The `CanNode` build echoes a frame handed to its input Clock back out
        on its output Clock, one discrete-state update later. Two of them
        connected to each other answer each other forever at one instant, so
        the group's own bound on propagation is what ends it — the same kind
        of declaration as the bound on iterating one event.
        """
        echoing = built(build_dir, tmp_path, "echo1", "CanNode")
        # The same FMU, declaring itself the bus simulation end, because a
        # connection has one of each and what is under test is the loop.
        as_bus = node_fmu(
            tmp_path / "echo2.fmu",
            model_identifier="ClockedCanNode",
            binary=build_dir / f"ClockedCanNode{library_suffix()}",
            platform_directory=platform_directory(),
            rewrite_manifest=lambda text: text.replace(
                'isBusSimulationFMU="false"', 'isBusSimulationFMU="true"'
            ),
        )
        with pytest.raises(ParticipantFailure, match="bounds the propagation"):
            group(
                instances=("node1", "bus"),
                connects=["node1.CanChannel=bus.CanChannel"],
                binds=["can.node1.Tx:data=node1.CanChannel.Tx_Data"],
                channels={"can.node1.Tx": (BUFFER_SCHEMA, "out")},
                paths={"node1": echoing, "bus": as_bus},
            )

    def test_a_step_that_does_not_continue_where_the_last_one_ended_is_refused(
        self, group
    ):
        participant = group()
        participant.on_step(0, 100 * MS, [])
        with pytest.raises(ParticipantFailure, match="stands at 100000000 ns"):
            participant.on_step(300 * MS, 100 * MS, [])


class TestTheReplayBoundary:
    """One live source replaced by the Channel its frames were recorded on.

    A Message on an in-direction Channel is an activation of the terminal's
    input Clock, and it is raised at the instant the Message states rather than
    wherever the group happens to stand. That is what makes a replayed source
    equivalent to the live one it stands in for: the receiver is handed the
    same operation at the same instant, so what it does next is the same too.

    The instant has to be reachable, and a Channel is what decides that. A
    Message published in the Slot the observing activation ran in states an
    instant inside the Step that observed it, so a zero Latency — delivery in
    the Slot it was published in — puts the Message in the Step its instant
    belongs to. Anything else arrives after the instant it names, and no FMU of
    this profile can be taken back to it.
    """

    # What the fixture's node publishes in the event initialization ends in,
    # and what it transmits every 300 ms, read from the same expectation the
    # connected exchange is judged against.
    CONFIGURATION = bytes.fromhex(
        EXPECTED["cases"][0]["events"][0]["payload_hex"]
    )
    FRAME = bytes.fromhex(EXPECTED["cases"][0]["events"][2]["payload_hex"])

    @pytest.fixture
    def replayed(self, group, tmp_path, build_dir):
        """The connected group with one node replaced by a Channel.

        `node1` is still live and connected to `bus.Node1`. `bus.Node2` is
        connected to nothing and fed by `can.replay` instead, which is the
        boundary a Recording of the removed node's Channel stands in at.
        """
        return group(
            instances=("node1", "bus"),
            connects=["node1.CanChannel=bus.Node1"],
            binds=[
                "can.node1.Tx:data=node1.CanChannel.Tx_Data",
                "can.bus.Node1:data=bus.Node1.Tx_Data",
                "can.bus.Node2:data=bus.Node2.Tx_Data",
                "can.replay:data=bus.Node2.Rx_Data",
            ],
            channels={
                "can.node1.Tx": (BUFFER_SCHEMA, "out"),
                "can.bus.Node1": (BUFFER_SCHEMA, "out"),
                "can.bus.Node2": (BUFFER_SCHEMA, "out"),
                "can.replay": (BUFFER_SCHEMA, "in"),
            },
            paths={
                "node1": built(build_dir, tmp_path, "node1", "CanNodeOnABus"),
                "bus": built(build_dir, tmp_path, "bus", "CanBus"),
            },
        )

    @staticmethod
    def message(payload: bytes, event_time_ns: int, publish_ns: int = 0):
        """One recorded activation, as the kernel delivers it back."""
        return Input("can.replay", publish_ns, {
            "data": payload.ljust(CAN_BUFFER_BYTES, b"\x00"),
            "data_length": len(payload),
            "data_event_time_ns": event_time_ns,
        })

    @staticmethod
    def frame(identifier: int, payload: bytes) -> bytes:
        """One `CanTransmit` operation, in the layered standard's own bytes.

        `fmi3LsBusCanOperationCanTransmit`: the 8-byte header states the
        operation's own total length, and the body is the CAN ID, the IDE and
        RTR flags, the data length, and the data.
        """
        body = (identifier.to_bytes(4, "little") + b"\x00\x00"
                + len(payload).to_bytes(2, "little") + payload)
        return (int(0x10).to_bytes(4, "little")
                + (HEADER_SIZE + len(body)).to_bytes(4, "little") + body)

    @staticmethod
    def stated(published: list) -> list[tuple[str, int, str]]:
        """Every published Message, as channel, event time and payload."""
        return [
            (channel, fields["data_event_time_ns"],
             fields["data"][:fields["data_length"]].hex())
            for channel, fields in published
        ]

    def configured(self, participant) -> None:
        """The first Step: the configuration of the node that was replaced.

        The bus enables communication only once both terminals agreed on one
        baud rate, so the replayed source has to carry its own configuration
        for anything after it to be transmitted at all.
        """
        participant.on_step(0, 100 * MS, [self.message(self.CONFIGURATION, 0)])

    def test_an_activation_is_raised_at_the_instant_the_message_states(
        self, replayed
    ):
        """The instant the Message names, not the one the group stands on.

        The frame is handed to the terminal at 150 ms, inside a Step that
        began at 100 ms, and the bus's transmission time is counted from there.
        """
        self.configured(replayed)
        published = replayed.on_step(
            100 * MS, 100 * MS, [self.message(self.FRAME, 150 * MS)]
        )
        assert self.stated(published) == [
            ("can.bus.Node1", 150 * MS + 480 * US, self.FRAME.hex()),
            ("can.bus.Node2", 150 * MS + 480 * US,
             "200000000c00000001000000"),
        ]

    def test_an_activation_at_the_start_of_a_step_is_raised_there(
        self, replayed
    ):
        """The instant a Step begins on is the instant the group stands on."""
        self.configured(replayed)
        published = replayed.on_step(
            100 * MS, 100 * MS, [self.message(self.FRAME, 100 * MS)]
        )
        assert self.stated(published) == [
            ("can.bus.Node1", 100 * MS + 480 * US, self.FRAME.hex()),
            ("can.bus.Node2", 100 * MS + 480 * US,
             "200000000c00000001000000"),
        ]

    def test_an_activation_at_the_end_of_a_step_is_raised_there(
        self, replayed
    ):
        """The Step's own end is reachable, and is where a recorded Message
        published in the Slot before it states its instant."""
        self.configured(replayed)
        assert replayed.on_step(
            100 * MS, 100 * MS, [self.message(self.FRAME, 200 * MS)]
        ) == []
        # The transmission time is counted from the instant the frame arrived,
        # which lands it in the next Step — where the live node's own transmit
        # at 300 ms is too.
        assert self.stated(replayed.on_step(200 * MS, 100 * MS, [])) == [
            ("can.bus.Node1", 200 * MS + 480 * US, self.FRAME.hex()),
            ("can.bus.Node2", 200 * MS + 480 * US,
             "200000000c00000001000000"),
            ("can.node1.Tx", 300 * MS, self.FRAME.hex()),
        ]

    def test_two_activations_at_one_instant_keep_the_order_they_arrived_in(
        self, replayed
    ):
        """Same-time ordering at the boundary is the Recording's Publish order.

        Two frames of one CAN ID leave the bus in the order they were offered
        to it, so the order the Messages arrived in is observable rather than
        assumed.
        """
        self.configured(replayed)
        first, second = self.frame(1, b"\x0a"), self.frame(1, b"\x0b")
        published = replayed.on_step(100 * MS, 100 * MS, [
            self.message(first, 150 * MS), self.message(second, 150 * MS),
        ])
        assert [
            (channel, event, payload)
            for channel, event, payload in self.stated(published)
            if channel == "can.bus.Node1"
        ] == [
            ("can.bus.Node1", 150 * MS + 450 * US, first.hex()),
            ("can.bus.Node1", 150 * MS + 900 * US, second.hex()),
        ]

    def test_two_activations_in_one_step_are_each_raised_at_their_own_instant(
        self, replayed
    ):
        """One Step carries every activation the Step that recorded it did."""
        self.configured(replayed)
        early, late = self.frame(1, b"\x0a"), self.frame(1, b"\x0b")
        published = replayed.on_step(100 * MS, 100 * MS, [
            self.message(early, 120 * MS), self.message(late, 160 * MS),
        ])
        assert [
            (event, payload)
            for channel, event, payload in self.stated(published)
            if channel == "can.bus.Node1"
        ] == [
            (120 * MS + 450 * US, early.hex()),
            (160 * MS + 450 * US, late.hex()),
        ]

    def test_an_activation_behind_the_group_is_refused(self, replayed):
        """No FMU of this profile can be taken back to an instant it passed."""
        self.configured(replayed)
        with pytest.raises(ParticipantFailure, match="stands at 100000000 ns"):
            replayed.on_step(
                100 * MS, 100 * MS, [self.message(self.FRAME, 42)]
            )

    def test_an_activation_beyond_the_step_is_refused(self, replayed):
        """A Step is not stepped past to reach an instant after its end."""
        self.configured(replayed)
        with pytest.raises(ParticipantFailure, match="ends at 200000000 ns"):
            replayed.on_step(
                100 * MS, 100 * MS, [self.message(self.FRAME, 250 * MS)]
            )


class TestCleanup:
    """Every instance is closed, on every path the group can fail on."""

    def test_every_instance_is_terminated_and_freed_on_close(
        self, group, monkeypatch
    ):
        recorded = calls(monkeypatch)
        participant = group()
        participant.close()
        assert sum(
            1 for _, name, _ in recorded if name == "fmi3Terminate"
        ) == len(INSTANCES)

    def test_an_initialization_failure_still_closes_the_instances_loaded(
        self, group, monkeypatch
    ):
        recorded = calls(monkeypatch)
        with pytest.raises(ParticipantFailure, match="terminateSimulation"):
            group(node="TerminateInEvent")
        # What the importer does on the way out of a failure it has reported.
        group.participants[-1].close()
        # The failure is in the first event, after every instance was loaded.
        instantiated = {
            instance for instance, name, _ in recorded
            if name == "fmi3EnterInitializationMode"
        }
        terminated = {
            instance for instance, name, _ in recorded
            if name == "fmi3Terminate"
        }
        assert len(instantiated) == len(INSTANCES)
        assert terminated == instantiated

    def test_a_step_failure_still_closes_every_instance(
        self, group, monkeypatch
    ):
        recorded = calls(monkeypatch)
        participant = group(node="EarlyReturn")
        with pytest.raises(ParticipantFailure, match="returned early"):
            participant.on_step(0, 100 * MS, [])
        recorded.clear()
        participant.close()
        assert sum(
            1 for _, name, _ in recorded if name == "fmi3Terminate"
        ) == len(INSTANCES)


class TestGroupsRejectedBeforeStepping:
    """What a group refuses to start, and the diagnostic it refuses with."""

    def test_an_instance_declaration_without_a_path(self):
        participant = FmuGroupParticipant([("node1", "")], profile=CAN_PROFILE)
        with pytest.raises(ManifestError, match="names no FMU"):
            participant.on_init(init_line({}, SCHEMAS))

    def test_a_group_that_declares_no_instance(self):
        participant = FmuGroupParticipant([], profile=CAN_PROFILE)
        with pytest.raises(ManifestError, match="at least one --instance"):
            participant.on_init(init_line({}, SCHEMAS))

    @pytest.mark.parametrize("name", ["absent.fmu", "not-an-archive.fmu"])
    def test_an_unreadable_archive_names_the_instance_that_declared_it(
        self, group, tmp_path, build_dir, name
    ):
        """A group extracts several archives, so the path is not the whole
        diagnostic: which instance declared it is what a Manifest is fixed
        by."""
        unreadable = tmp_path / name
        if name != "absent.fmu":
            unreadable.write_text("this is not a zip archive")
        with pytest.raises(
            ManifestError,
            match=f"cannot read FMU '{unreadable}' of instance 'node2'",
        ):
            group(paths={
                "node1": built(build_dir, tmp_path, "node1", "CanNodeOnABus"),
                "node2": unreadable,
                "bus": built(build_dir, tmp_path, "bus", "CanBus"),
            })

    def test_an_unknown_instance_in_a_connection(self, group):
        with pytest.raises(ManifestError, match="names instance 'node3'"):
            group(connects=["node3.CanChannel=bus.Node1"])

    def test_an_unknown_terminal_in_a_connection(self, group):
        with pytest.raises(ManifestError, match="names terminal 'Node9'"):
            group(connects=["node1.CanChannel=bus.Node9"])

    def test_a_terminal_connected_twice(self, group):
        with pytest.raises(ManifestError, match="already connected"):
            group(connects=CONNECTS + ["node2.CanChannel=bus.Node1"])

    def test_a_terminal_connected_to_itself(self, group):
        with pytest.raises(ManifestError, match="to itself"):
            group(connects=["bus.Node1=bus.Node1"])

    def test_two_nodes_connected_without_a_bus_between_them(self, group):
        with pytest.raises(ManifestError, match="isBusSimulationFMU=false"):
            group(connects=["node1.CanChannel=node2.CanChannel"])

    def test_a_connection_between_two_bus_simulation_fmus(
        self, group, tmp_path, build_dir
    ):
        with pytest.raises(ManifestError, match="isBusSimulationFMU=true"):
            group(
                instances=("bus", "other"),
                connects=["bus.Node1=other.Node1"],
                binds=["can.bus.Node1:data=bus.Node1.Tx_Data"],
                channels={"can.bus.Node1": (BUFFER_SCHEMA, "out")},
                paths={
                    "bus": built(build_dir, tmp_path, "bus", "CanBus"),
                    "other": built(build_dir, tmp_path, "other", "CanBus"),
                },
            )

    def test_a_bus_profile_the_terminals_do_not_carry(self, group):
        with pytest.raises(ManifestError, match="this Run declares the BUS"):
            group(profile="application/org.fmi-standard.fmi-ls-bus.flexray")

    def test_a_group_that_declares_no_bus_profile(self, group):
        with pytest.raises(ManifestError, match="declares the BUS profile"):
            group(profile="")

    def test_two_ends_declaring_different_layered_standard_versions(
        self, group, tmp_path, build_dir
    ):
        node = node_fmu(
            tmp_path / "other.fmu",
            model_identifier="ClockedCanNodeOnABus",
            binary=build_dir / f"ClockedCanNodeOnABus{library_suffix()}",
            platform_directory=platform_directory(),
            rewrite_manifest=lambda text: text.replace(
                "1.0.0-beta.1", "2.0.0"
            ),
        )
        with pytest.raises(ManifestError, match="one version of the layered"):
            group(paths={
                "node1": node,
                "node2": built(build_dir, tmp_path, "node2", "CanNodeOnABus"),
                "bus": built(build_dir, tmp_path, "bus", "CanBus"),
            })

    def test_two_ends_declaring_no_layered_standard_version(
        self, group, tmp_path, build_dir
    ):
        """An absent version is not a version two ends agree on.

        Comparing two absent ones would let a pair of FMUs that say nothing
        about the layered standard pass the check that exists to make them say
        something.
        """
        def unversioned(text: str) -> str:
            return re.sub(r' fmi-ls:fmi-ls-version="[^"]*"', "", text)

        with pytest.raises(ManifestError, match="declares no .* version in"):
            group(paths={
                "node1": node_fmu(
                    tmp_path / "node1.fmu",
                    model_identifier="ClockedCanNodeOnABus",
                    binary=build_dir
                    / f"ClockedCanNodeOnABus{library_suffix()}",
                    platform_directory=platform_directory(),
                    rewrite_manifest=unversioned,
                ),
                "node2": built(build_dir, tmp_path, "node2", "CanNodeOnABus"),
                "bus": bus_fmu(
                    tmp_path / "bus.fmu",
                    model_identifier="ClockedCanBus",
                    binary=build_dir / f"ClockedCanBus{library_suffix()}",
                    platform_directory=platform_directory(),
                    rewrite_manifest=unversioned,
                ),
            })

    def test_a_terminal_that_is_not_a_network_terminal(
        self, group, tmp_path, build_dir
    ):
        node = node_fmu(
            tmp_path / "other.fmu",
            model_identifier="ClockedCanNodeOnABus",
            binary=build_dir / f"ClockedCanNodeOnABus{library_suffix()}",
            platform_directory=platform_directory(),
            rewrite_terminals=lambda text: text.replace(
                "org.fmi-ls-bus.network-terminal", "org.example.other"
            ),
        )
        with pytest.raises(ManifestError, match="declares terminalKind"):
            group(paths={
                "node1": node,
                "node2": built(build_dir, tmp_path, "node2", "CanNodeOnABus"),
                "bus": built(build_dir, tmp_path, "bus", "CanBus"),
            })

    def test_an_fmu_that_declares_no_terminal(self, group, tmp_path, build_dir):
        node = node_fmu(
            tmp_path / "other.fmu",
            model_identifier="ClockedCanNodeOnABus",
            binary=build_dir / f"ClockedCanNodeOnABus{library_suffix()}",
            platform_directory=platform_directory(),
            rewrite_terminals=lambda text: (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<fmiTerminalsAndIcons fmiVersion="3.0"><Terminals/>'
                '</fmiTerminalsAndIcons>'
            ),
        )
        with pytest.raises(ManifestError, match="does not declare \\(declared: none"):
            group(paths={
                "node1": node,
                "node2": built(build_dir, tmp_path, "node2", "CanNodeOnABus"),
                "bus": built(build_dir, tmp_path, "bus", "CanBus"),
            })

    def test_an_instance_no_connection_and_no_channel_names(self, group):
        """An FMU is stepped on every Step, so an unreached one is a mistake."""
        with pytest.raises(ManifestError, match="named by no --connect"):
            group(
                connects=["node1.CanChannel=bus.Node1"],
                binds=BINDINGS[:1] + BINDINGS[2:3],
                channels={
                    "can.node1.Tx": (BUFFER_SCHEMA, "out"),
                    "can.bus.Node1": (BUFFER_SCHEMA, "out"),
                },
            )

    def test_a_terminal_of_another_profile_the_run_does_not_use_is_left_alone(
        self, group, tmp_path, build_dir
    ):
        """Only what the Run drives is checked against the declared profile.

        An FMU may carry a terminal of another layered standard beside the one
        it is connected through, and that is its own business.
        """
        node = node_fmu(
            tmp_path / "two-terminals.fmu",
            model_identifier="ClockedCanNodeOnABus",
            binary=build_dir / f"ClockedCanNodeOnABus{library_suffix()}",
            platform_directory=platform_directory(),
            rewrite_terminals=lambda text: text.replace(
                "</Terminals>",
                '<Terminal name="FlexRayChannel" '
                'terminalKind="org.fmi-ls-bus.network-terminal" '
                'matchingRule="org.fmi-ls-bus.flexray"/></Terminals>',
            ),
        )
        participant = group(paths={
            "node1": node,
            "node2": built(build_dir, tmp_path, "node2", "CanNodeOnABus"),
            "bus": built(build_dir, tmp_path, "bus", "CanBus"),
        })
        assert observed(participant, 100 * MS, 400 * MS) == [
            event for event in case("aligned")["events"]
            if event["published_ns"] < 400 * MS
        ]

    def test_an_fmu_without_the_layered_standard_manifest(
        self, group, tmp_path, build_dir
    ):
        node = node_fmu(
            tmp_path / "other.fmu",
            model_identifier="ClockedCanNodeOnABus",
            binary=build_dir / f"ClockedCanNodeOnABus{library_suffix()}",
            platform_directory=platform_directory(),
            rewrite_manifest=lambda text: None,
        )
        with pytest.raises(ManifestError, match="carries no extra/"):
            group(paths={
                "node1": node,
                "node2": built(build_dir, tmp_path, "node2", "CanNodeOnABus"),
                "bus": built(build_dir, tmp_path, "bus", "CanBus"),
            })

    def test_a_channel_bound_to_a_variable_of_no_terminal(self, group):
        with pytest.raises(ManifestError, match="is no 'Tx_Data' or 'Rx_Data'"):
            group(binds=BINDINGS[:3] + ["can.bus.Node2:data=bus.time"])

    def test_an_out_direction_channel_on_a_receiving_member(self, group):
        with pytest.raises(ManifestError, match="direction is 'out'"):
            group(binds=BINDINGS[:3] + [
                "can.bus.Node2:data=bus.Node2.Rx_Data"
            ])

    def test_two_channels_observing_one_terminal(self, group):
        with pytest.raises(ManifestError, match="already observes"):
            group(binds=BINDINGS[:3] + [
                "can.bus.Node2:data=bus.Node1.Tx_Data"
            ])

    def test_a_channel_handed_to_a_connected_terminal(self, group):
        with pytest.raises(ManifestError, match="not from both"):
            group(
                binds=BINDINGS + ["can.replay:data=bus.Node1.Rx_Data"],
                channels={**OBSERVED, "can.replay": (BUFFER_SCHEMA, "in")},
            )

    def test_a_channel_that_binds_no_variable(self, group):
        with pytest.raises(ManifestError, match="binds 0 FMU variables"):
            group(binds=BINDINGS[:3])

    def test_a_channel_that_binds_two_variables(self, group):
        with pytest.raises(ManifestError, match="binds 2 FMU variables"):
            group(binds=BINDINGS + [
                "can.bus.Node2:data_length=bus.Node1.Tx_Data"
            ])

    def test_an_instance_name_carrying_the_separator(self, group, tmp_path,
                                                     build_dir):
        with pytest.raises(ManifestError, match="carries a '\\.'"):
            group(instances=("node.1",), connects=[], binds=[], channels={},
                  paths={"node.1": built(build_dir, tmp_path, "a",
                                         "CanNodeOnABus")})

    def test_a_duplicate_instance_name(self, group, tmp_path, build_dir):
        with pytest.raises(ManifestError, match="twice"):
            group(instances=("node1", "node1"), connects=[], binds=[],
                  channels={},
                  paths={"node1": built(build_dir, tmp_path, "a",
                                        "CanNodeOnABus")})

    def test_a_start_value_for_a_variable_of_no_instance(self, group):
        with pytest.raises(ManifestError, match="names instance 'node9'"):
            group(starts=["node9.CanChannel.Tx_Data=00"])

    def test_a_parameter_is_declared_per_instance(self, group):
        """A declared parameter is part of the Manifest the Run is hashed by."""
        participant = group(starts=["bus.BusErrorProbability=0.0"])
        assert observed(participant, 100 * MS, 100 * MS)

    def test_a_declared_parameter_reaches_the_instance_that_declared_it(
        self, group
    ):
        """The stand-in refuses a bus error probability above zero, because
        upstream draws that path from `rand()` and nothing here models it. A
        Run that reached the FMU with the declared value is what that refusal
        reports; a parameter quietly dropped would start instead."""
        with pytest.raises(ParticipantFailure, match="fmi3SetFloat64 returned"):
            group(starts=["bus.BusErrorProbability=0.5"])


OBSERVER = ROOT / "tests" / "participants" / "can_observer.py"

# Four activations of one Channel in one Slot is the most any Step of these
# grids publishes, and the route is drained every Step.
ROUTE_CAPACITY = 8


def group_manifest(node: Path, bus: Path, *, step_period_ns: int,
                   duration_ns: int) -> Manifest:
    """The two nodes and the bus, driven by one importer, and read back."""
    m = Manifest(duration_ns=duration_ns)
    m.add_schemas(SCHEMAS)
    for channel in SOURCES:
        m.add_channel(channel, schema=BUFFER_SCHEMA)
    m.add_process(
        "importer",
        command=[
            sys.executable, "-m", "sil.fmi",
            "--instance", "node1", str(node),
            "--instance", "node2", str(node),
            "--instance", "bus", str(bus),
            "--bus-profile", CAN_PROFILE,
            *(argument for connect in CONNECTS
              for argument in ("--connect", connect)),
            *(argument for bind in BINDINGS
              for argument in ("--bind", bind)),
            "--start", "bus.BusErrorProbability=0.0",
        ],
        step_period_ns=step_period_ns,
        publishes=list(SOURCES),
        priority=1,
    )
    # A published Channel with no subscriber describes a Run nobody would
    # execute; the observer is what a consumer of the bus traffic is.
    m.add_process(
        "observer",
        command=[sys.executable, str(OBSERVER)],
        step_period_ns=step_period_ns,
        subscribes=[
            SubscriberRoute(channel, capacity=ROUTE_CAPACITY)
            for channel in SOURCES
        ],
        priority=2,
    )
    return m


@pytest.fixture(scope="module")
def aligned_result(sil_run, tmp_path_factory, build_dir):
    """One Run on the fixture's aligned grid, shared by every assertion."""
    workdir = tmp_path_factory.mktemp("fmi-group")
    declared = case("aligned")
    return run_simulation(
        group_manifest(
            built(build_dir, workdir, "node", "CanNodeOnABus"),
            built(build_dir, workdir, "bus", "CanBus"),
            step_period_ns=declared["step_size_ns"],
            duration_ns=declared["duration_ns"],
        ),
        runner=sil_run, workdir=workdir,
    )


class TestRunBoundary:
    """The group as one process participant in a complete Run."""

    def test_the_recording_carries_the_expected_exchange(self, aligned_result):
        """Every Channel of the Recording, against what was stated first.

        The comparison is per Channel and in the Recording's own order, which
        is the order the group produced the activations in: a Channel's
        Messages are one terminal's activations, and nothing else may appear
        on it.
        """
        expected = case("aligned")["events"]
        for channel, source in SOURCES.items():
            assert [
                {
                    "time_ns": fields["data_event_time_ns"],
                    "published_ns": published_ns,
                    "source": source,
                    "payload_hex":
                        fields["data"][:fields["data_length"]].hex(),
                    "operations": [
                        {"name": operation.name, "fields": operation.fields}
                        for operation in decode(
                            fields["data"][:fields["data_length"]]
                        )
                    ],
                }
                for published_ns, fields in aligned_result.messages(channel)
            ] == [
                event for event in expected if event["source"] == source
            ], channel

    def test_an_fmu_path_resolves_against_the_manifest_and_is_digested(
        self, sil_run, tmp_path, build_dir
    ):
        """Each FMU is an artifact of the Run, named the way every other is.

        The kernel resolves a command argument that names a file against the
        Manifest's directory and digests it into the Run's provenance. It does
        that for arguments, not for text inside one, so a group's FMU paths are
        arguments of their own: a Manifest may name them relatively, and the
        Recording is attributable to the archives that produced it.
        """
        models = tmp_path / "models"
        models.mkdir()
        built(build_dir, models, "node", "CanNodeOnABus")
        built(build_dir, models, "bus", "CanBus")
        manifest = group_manifest(
            Path("models/node.fmu"), Path("models/bus.fmu"),
            step_period_ns=100 * MS, duration_ns=400 * MS,
        )
        result = run_simulation(manifest, runner=sil_run, workdir=tmp_path)
        provenance = json.loads(
            result.mcap_path.with_name(
                result.mcap_path.name + ".provenance.json"
            ).read_text()
        )
        digested = {
            entry["file"]["path"]: entry["file"]["sha256"]
            for entry in provenance["artifacts"]["command_files"]
            if entry["file"]
        }
        for archive in (models / "node.fmu", models / "bus.fmu"):
            assert str(archive.resolve()) in digested, digested
            assert digested[str(archive.resolve())] == hashlib.sha256(
                archive.read_bytes()
            ).hexdigest()

    def test_the_determinism_check_passes_for_a_connected_run(
        self, sil_run, tmp_path, build_dir
    ):
        declared = case("aligned")
        manifest = group_manifest(
            built(build_dir, tmp_path, "node", "CanNodeOnABus"),
            built(build_dir, tmp_path, "bus", "CanBus"),
            step_period_ns=declared["step_size_ns"],
            duration_ns=declared["duration_ns"],
        )
        first = run_simulation(manifest, runner=sil_run, workdir=tmp_path)
        recorded = first.mcap_path.read_bytes()
        second = run_simulation(manifest, runner=sil_run, workdir=tmp_path)
        assert second.mcap_path.read_bytes() == recorded


# The boundary the live source is replaced at: the Channel its activations
# were observed on, replayed into the terminal it used to be connected to.
BOUNDARY = "can.node1.Tx"
RECEIVER_SOURCES = {
    channel: source for channel, source in SOURCES.items()
    if channel != BOUNDARY
}


def replay_manifest(node: Path, bus: Path, recording: Path, *,
                    step_period_ns: int, duration_ns: int,
                    latency_ns: int | None = 0) -> Manifest:
    """The same composition with the live source replaced by its Recording.

    `node1` is gone. `bus.Node1` is connected to nothing and handed the
    Messages the removed node published, by a Replay participant reading the
    live Run's Recording. Everything else — the receiving node, the bus, the
    bus configuration, the step grid — is what the live Run declared.

    The boundary Channel declares `latency_ns` 0. An activation observed
    during a Step is published in the Slot that Step began in, so a Message
    delivered in the Slot it was published in arrives in the Step its instant
    belongs to; the default next-activation delivery would put every one of
    them one Step behind the instant it states.
    """
    m = Manifest(duration_ns=duration_ns)
    m.add_schemas(SCHEMAS)
    m.add_channel(BOUNDARY, schema=BUFFER_SCHEMA, latency_ns=latency_ns)
    for channel in RECEIVER_SOURCES:
        m.add_channel(channel, schema=BUFFER_SCHEMA)
    m.add_replay("node1", recording=recording, channels=[BOUNDARY])
    m.add_process(
        "importer",
        command=[
            sys.executable, "-m", "sil.fmi",
            "--instance", "node2", str(node),
            "--instance", "bus", str(bus),
            "--bus-profile", CAN_PROFILE,
            "--connect", "node2.CanChannel=bus.Node2",
            "--bind", f"{BOUNDARY}:data=bus.Node1.Rx_Data",
            *(argument for channel, variable in VARIABLES.items()
              if channel != BOUNDARY
              for argument in ("--bind", f"{channel}:data={variable}")),
            "--start", "bus.BusErrorProbability=0.0",
        ],
        step_period_ns=step_period_ns,
        subscribes=[SubscriberRoute(BOUNDARY, capacity=ROUTE_CAPACITY)],
        publishes=list(RECEIVER_SOURCES),
        priority=1,
    )
    m.add_process(
        "observer",
        command=[sys.executable, str(OBSERVER)],
        step_period_ns=step_period_ns,
        subscribes=[
            SubscriberRoute(channel, capacity=ROUTE_CAPACITY)
            for channel in SOURCES
        ],
        priority=2,
    )
    return m


def messages_by_channel(
    result, channels=SOURCES
) -> dict[str, list[tuple[int, str]]]:
    """Every Message of the named Channels, as event time and payload.

    Held in the Recording's Publish order, so comparing two of these compares
    the count, that order, the event times and the payloads at once. The
    publication Slot is left out on purpose: what a replaced source has to
    reproduce is the operation and the instant it crossed the terminal at, and
    comparing whole Recordings across two Manifest hashes would compare the
    Manifests instead.
    """
    return {
        channel: [
            (fields["data_event_time_ns"],
             fields["data"][:fields["data_length"]].hex())
            for _, fields in result.messages(channel)
        ]
        for channel in channels
    }


@pytest.fixture(scope="module")
def runs(sil_run, tmp_path_factory, build_dir):
    """The live Run, and the replay Run built out of its Recording."""
    workdir = tmp_path_factory.mktemp("fmi-replay")
    declared = case("aligned")
    node = built(build_dir, workdir, "node", "CanNodeOnABus")
    bus = built(build_dir, workdir, "bus", "CanBus")
    live = run_simulation(
        group_manifest(
            node, bus, step_period_ns=declared["step_size_ns"],
            duration_ns=declared["duration_ns"],
        ),
        runner=sil_run, workdir=workdir,
    )
    replayed = tmp_path_factory.mktemp("fmi-replay-run")
    recording = replayed / "live.mcap"
    recording.write_bytes(live.mcap_path.read_bytes())
    replay = run_simulation(
        replay_manifest(
            node, bus, recording,
            step_period_ns=declared["step_size_ns"],
            duration_ns=declared["duration_ns"],
        ),
        runner=sil_run, workdir=replayed,
    )
    return live, replay


class TestReplayEquivalence:
    """One live source replaced by its Recording, in a complete Run."""

    def test_the_receiver_sees_the_same_operations_at_the_same_instants(
        self, runs
    ):
        """What the proof compares: the unchanged receiver's own Channels.

        The removed node is not in the replay Run at all, so the only thing
        that could reproduce the receiving node's transmissions and the bus's
        two arbitrated deliveries is the replayed source landing on the same
        instants the live one produced.
        """
        live, replay = runs
        assert messages_by_channel(
            replay, RECEIVER_SOURCES
        ) == messages_by_channel(live, RECEIVER_SOURCES)

    def test_the_replayed_boundary_carries_the_recorded_stream(self, runs):
        """The stimulus is the Recording's, unchanged and complete."""
        live, replay = runs
        assert messages_by_channel(
            replay, [BOUNDARY]
        ) == messages_by_channel(live, [BOUNDARY])

    def test_the_replay_run_reproduces(self, sil_run, tmp_path, build_dir,
                                       runs):
        """The determinism check, run against the replay Manifest's own hash.

        A replay Run is a Run: its stimulus is a file the Manifest hashes, and
        two Runs of it record the same bytes.
        """
        live, _ = runs
        declared = case("aligned")
        recording = tmp_path / "live.mcap"
        recording.write_bytes(live.mcap_path.read_bytes())
        manifest = replay_manifest(
            built(build_dir, tmp_path, "node", "CanNodeOnABus"),
            built(build_dir, tmp_path, "bus", "CanBus"),
            recording,
            step_period_ns=declared["step_size_ns"],
            duration_ns=declared["duration_ns"],
        )
        first = run_simulation(manifest, runner=sil_run, workdir=tmp_path)
        recorded = first.mcap_path.read_bytes()
        second = run_simulation(manifest, runner=sil_run, workdir=tmp_path)
        assert second.mcap_path.read_bytes() == recorded

    def test_the_default_latency_puts_every_message_behind_the_group(
        self, sil_run, tmp_path, build_dir, runs
    ):
        """Why the boundary Channel declares a Latency of zero.

        With the default next-activation delivery a Message arrives one Step
        after the Slot it was published in, which is one Step after the
        instant it states. The importer refuses it rather than raising the
        activation at an instant the FMUs cannot be taken back to.
        """
        live, _ = runs
        declared = case("aligned")
        recording = tmp_path / "live.mcap"
        recording.write_bytes(live.mcap_path.read_bytes())
        manifest = replay_manifest(
            built(build_dir, tmp_path, "node", "CanNodeOnABus"),
            built(build_dir, tmp_path, "bus", "CanBus"),
            recording,
            step_period_ns=declared["step_size_ns"],
            duration_ns=declared["duration_ns"],
            latency_ns=None,
        )
        with pytest.raises(RunFailure, match="an activation is raised at the"):
            run_simulation(manifest, runner=sil_run, workdir=tmp_path)
