"""One FMU driven on the kernel's own Slot grid.

The scheduling policy here is the kernel's: the FMU is stepped over exactly the
interval the Step declares, and an event happens at a communication point the
Manifest's step period decided. An FMU that asks to be stopped between two of
them is told that cannot be promised — this importer does not choose
communication points. A group of connected FMUs is the other policy, and it
owns the instants between two Slots.
"""

from __future__ import annotations

from pathlib import Path
from collections.abc import Sequence

from sil.participant import ParticipantFailure, StepParticipant

from sil.fmi.archive import Extraction
from sil.fmi.binding import ChannelBinding, ClockedPayload
from sil.fmi.description import ModelDescription
from sil.fmi.mapping import (
    bind_channel,
    channel_fields,
    clocked_payload,
    declared_bindings,
    derived_bindings,
    start_values,
)
from sil.fmi.runtime import NS_PER_S, CoSimulation
from sil.fmi.stepping import declared_ns, require_contiguous, run_event


class _Events:
    """The FMU's event side: the Clocks an event activates, in both directions.

    Every method here is called with the FMU already in Event Mode, because
    entering and leaving it is the participant's business — an event that
    followed a Step and one that ends initialization are the same event, and
    only the caller knows which it is holding.

    `next_event_time` is what the last discrete-state update declared, in
    seconds, and it is kept because the *next* Step is where an importer that
    cannot honour it has to say so.
    """

    def __init__(self, outputs: dict[str, ClockedPayload],
                 inputs: dict[str, ClockedPayload]):
        self._outputs = list(outputs.items())
        self._inputs = inputs
        self.next_event_time: float | None = None

    def handle(self, fmu: CoSimulation, event_time_ns: int) -> list:
        """One event: every Clock activation it carries, in Publish order."""
        published, states = run_event(
            fmu, event_time_ns,
            lambda: self._activations(fmu, event_time_ns),
        )
        self.next_event_time = states.next_event_time
        return published

    def _activations(self, fmu: CoSimulation, event_time_ns: int) -> list:
        """Every output Clock that reads active, and the buffer it gates."""
        published = []
        for channel, payload in self._outputs:
            activation = payload.activation(fmu, event_time_ns)
            if activation is not None:
                published.append((channel, activation))
        return published

    def deliver(self, fmu: CoSimulation, messages: list,
                event_time_ns: int) -> list:
        """Activate one input Clock per Message, in the order they arrived.

        Each Message is its own activation, so they are handed over one at a
        time: two frames delivered in one Slot are two activations of the same
        Clock, and merging them would lose one.
        """
        published = []
        for message in messages:
            self._inputs[message.channel].activate(fmu, message.data)
            published.extend(self.handle(fmu, event_time_ns))
        return published

    def receives(self, channel: str) -> bool:
        """Whether Messages on this Channel are Clock activations."""
        return channel in self._inputs

    def require_next_event_reachable(self, t: int, dt: int) -> None:
        """Refuse to step past an event the FMU asked to be stopped at.

        An FMU that declares a next event time is asking its importer to
        choose the next communication point. This one cannot: the kernel owns
        the Slot grid, and the Manifest's step period is what decides it. An
        FMU that declares such a time inside the interval about to be stepped
        is told so, rather than stepped past it and reported as if the
        interval had been clean.

        An event declared *on* the end of this interval is reachable: the
        Step lands on it, and `due` is what takes it.
        """
        if self.next_event_time is None:
            return
        declared = declared_ns(self.next_event_time)
        if declared < t + dt:
            raise ParticipantFailure(
                f"the FMU declared its next event at {self.next_event_time} s "
                f"({declared} ns), inside the interval [{t}, {t + dt}] ns "
                f"this Step covers; this importer does not choose "
                f"communication points, so it cannot stop there"
            )

    def due(self, communication_point_ns: int) -> bool:
        """Whether the FMU declared an event at the point just reached.

        An FMU that declares a next event time is asking to be in Event Mode
        when its own clock reaches that instant. `fmi3DoStep` reporting
        `eventHandlingNeeded` is the FMU saying so a second time, and an
        importer that waited for the second one would skip the event of an
        FMU that only said it once.
        """
        return (
            self.next_event_time is not None
            and declared_ns(self.next_event_time) == communication_point_ns
        )


class FmuParticipant(StepParticipant):
    """A process participant whose behavior is an imported FMU's."""

    def __init__(self, fmu_path: Path, *, binds: Sequence[str] = (),
                 starts: Sequence[str] = ()):
        self.name = ""  # the init line's, for the one diagnostic the kernel misses
        self._fmu_path = fmu_path
        self._binds = list(binds)
        self._starts = list(starts)
        self._extraction = None
        self._fmu = None
        self._inputs: dict[str, ChannelBinding] = {}
        self._outputs: dict[str, ChannelBinding] = {}
        # Set when a Channel carries a Clock-gated payload; None is the
        # Step-only lifecycle, where the FMU never leaves Step Mode.
        self._events: _Events | None = None
        # What the event that ended initialization produced, held until the
        # first activation: the kernel has no Slot before it.
        self._pending_activations: list = []
        # Where the FMU stands, in kernel nanoseconds.
        self._communication_point = 0

    def on_init(self, init: dict) -> None:
        self.name = init["name"]
        self._extraction = Extraction()
        extracted = self._extraction.unpack(self._fmu_path)
        description = ModelDescription.read(extracted)
        # Everything the Manifest got wrong is rejected before the FMU is
        # instantiated: a mapping that cannot hold is a fact about the Run's
        # configuration, and no FMU has to be loaded to see it.
        self._bind_channels(init, description)
        starts = start_values(self._starts, description)
        self._fmu = CoSimulation(
            description.binary(extracted), description,
            event_mode=self._events is not None,
            resource_path=(extracted / "resources")
            if (extracted / "resources").is_dir() else None,
        )
        self._fmu.apply_start_values(starts)
        self._fmu.initialize()
        if self._events is not None:
            # With Event Mode in use, initialization ends in Event Mode, and
            # a bus node has its configuration waiting in that first event.
            # Its activations belong to the initial time, and they are
            # published in the first Slot the kernel activates this
            # participant in, which is that same instant.
            self._pending_activations = self._events.handle(self._fmu, 0)
            self._fmu.enter_step_mode()

    def _bind_channels(self, init: dict, description: ModelDescription) -> None:
        """Bind the declared Channels to FMU variables, in both directions.

        An input-direction Channel is written into the FMU before its step; an
        output-direction Channel is published from it after. Declaring one
        binding declares them all — the bindings are the whole mapping, and a
        Channel with none derives its own from the Float64 variable names.

        A Channel whose variable declares a Clock is neither: its Messages are
        activations of that Clock, handled in Event Mode.
        """
        fields_by_channel = channel_fields(init)
        bound = (
            declared_bindings(self._binds, fields_by_channel, description)
            if self._binds
            else derived_bindings(init, fields_by_channel, description)
        )
        clocked: dict[str, dict[str, ClockedPayload]] = {"in": {}, "out": {}}
        for channel, declaration in init["channels"].items():
            direction = declaration["direction"]
            fields = fields_by_channel[channel]
            payload = clocked_payload(
                channel, direction, fields, bound[channel], description
            )
            if payload is not None:
                clocked[direction][channel] = payload
                continue
            bindings = self._inputs if direction == "in" else self._outputs
            bindings[channel] = bind_channel(
                channel, direction, fields, bound[channel]
            )
        if clocked["in"] or clocked["out"]:
            self._events = _Events(clocked["out"], clocked["in"])

    def on_step(self, t: int, dt: int, inputs: list):
        """One Step of the FMU, and every Message the interval produced.

        The kernel's Slot is `t` and the FMU is advanced over `[t, t+dt]`, so
        what this publishes at `t` is what the FMU reached at `t+dt` — the
        Step-mapped outputs as the values it holds there, and each Clock
        activation as its own Message stating the FMI event time it belongs
        to. The two are different quantities on purpose: an operation
        observed at a communication point is published in the Slot the
        importer was activated in, and reaches a subscriber one Latency after
        that.
        """
        if self._events is None:
            return self._step(t, dt, inputs)
        self._require_communication_point(t)
        published, self._pending_activations = self._pending_activations, []
        events, plain = self._split_activations(inputs)
        published.extend(self._deliver(events, t))
        published.extend(self._step(t, dt, plain))
        self._communication_point = t + dt
        return published

    def _step(self, t: int, dt: int, inputs: list) -> list:
        """The Step-mapped half: write, advance, read, and take any event."""
        # Inputs arrive in publish order, so writing each in turn leaves the
        # newest Message on a Channel as the value the step sees.
        for message in inputs:
            self._inputs[message.channel].write(self._fmu, message.data)
        if self._events is not None:
            self._events.require_next_event_reachable(t, dt)
        event_needed = self._fmu.do_step(t / NS_PER_S, dt / NS_PER_S)
        published = []
        if event_needed and self._events is None:
            raise ParticipantFailure(
                f"fmi3DoStep reported eventHandlingNeeded at "
                f"{(t + dt) / NS_PER_S} s; no Channel of this Run carries "
                f"a Clock, so the FMU was instantiated with eventModeUsed "
                f"false and the event cannot be handled"
            )
        # The FMU asks for an event either by reporting one at the end of the
        # Step or by having declared its time beforehand. A Step that lands
        # on a declared event time is that event's communication point, and
        # waiting for the flag as well would skip it.
        if self._events is not None and (event_needed or self._events.due(t + dt)):
            self._fmu.enter_event_mode()
            published = self._events.handle(self._fmu, t + dt)
            self._fmu.enter_step_mode()
        return published + [
            (channel, binding.read(self._fmu))
            for channel, binding in self._outputs.items()
        ]

    def _split_activations(self, inputs: list) -> tuple[list, list]:
        """Messages that are Clock activations, and Messages that are values."""
        events: list = []
        plain: list = []
        for message in inputs:
            target = events if self._events.receives(message.channel) else plain
            target.append(message)
        return events, plain

    def _deliver(self, messages: list, t: int) -> list:
        """Hand the FMU the activations that arrived, at the time it stands on.

        They are delivered before the Step rather than inside it: the FMU is
        standing on this communication point, and an event happens at the
        point the FMU stands on.
        """
        if not messages:
            return []
        self._fmu.enter_event_mode()
        published = self._events.deliver(self._fmu, messages, t)
        self._fmu.enter_step_mode()
        return published

    def _require_communication_point(self, t: int) -> None:
        require_contiguous(self._communication_point, t)

    def close(self) -> None:
        """Terminate and free the instance, and drop the extracted FMU.

        The diagnostic stays unframed: on every path the kernel can see, the
        kernel is what names the participant.
        """
        fmu, extraction = self._fmu, self._extraction
        self._fmu = None
        self._extraction = None
        try:
            if fmu is not None:
                fmu.close()
        finally:
            if extraction is not None:
                extraction.cleanup()
