"""Execute the pinned CAN node FMU through an independent FMI 3.0 importer.

This is the reference exchange the acceptance fixture is built around: the
same FMU the milestone targets, driven by FMPy's FMI 3.0 bindings rather than
by SiL, and judged against payloads and event times stated in `expected.json`
before any Run produced them.

FMPy's own simulator is not what drives it. `fmpy.simulate_fmu` has no clock
or Binary handling at all — the FMI-LS-BUS lifecycle (Event Mode, a triggered
output Clock, a Binary buffer read while that Clock is active) has to be
driven explicitly. What comes from FMPy is the binding layer: the FMI 3.0
entry points, their argument marshalling, and its model description reader.
That keeps the reference exchange independent of the importer it is evidence
for, which is the whole point of having one.

    python reference_exchange.py <fmu> --trace observed.json --log fmu.log

The exit code is the verdict: 0 when every case matched the expectation, 1
when a case did not, and the mismatch is printed as the first differing event.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from fmpy import extract, read_model_description
from fmpy.fmi3 import FMU3Slave

# The decoder lives beside this file rather than on the path: the harness runs
# from wherever the proof mounts it, and the two are one unit.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from can_operations import Operation, decode

NS_PER_S = 1_000_000_000

EXPECTED = Path(__file__).resolve().parent / "expected.json"


@dataclass(frozen=True)
class Event:
    """One activation of the node's output Clock, and what it carried.

    `next_event_time_s` is what `fmi3UpdateDiscreteStates` said about the next
    event when this one ended, and `None` means it declared none. It is part
    of the event because an importer's step grid depends on it: an FMU that
    announces its next event can be stepped onto it, and one that does not
    cannot.
    """

    time_ns: int
    payload: bytes
    operations: list[Operation]
    next_event_time_s: float | None

    def as_dict(self) -> dict:
        return {
            "time_ns": self.time_ns,
            "payload_hex": self.payload.hex(),
            "operations": [
                {"name": operation.name, "fields": operation.fields}
                for operation in self.operations
            ],
            "next_event_time_s": self.next_event_time_s,
        }


class Node:
    """The FMU under one instantiation, driven in Event and Step Mode.

    Virtual time is integer nanoseconds and seconds are derived from those
    integers on every call, never accumulated — the same policy `sil.fmi`
    holds to, so a later comparison between this exchange and a SiL Run is not
    a comparison between two different ways of adding up a step size.
    """

    def __init__(self, fmu_path: Path, log: list[str]):
        description = read_model_description(fmu_path)
        variables = {
            variable.name: variable.valueReference
            for variable in description.modelVariables
        }
        self._clock = variables["CanChannel.Tx_Clock"]
        self._data = variables["CanChannel.Tx_Data"]
        self._log = log
        self._slave = FMU3Slave(
            guid=description.guid,
            unzipDirectory=extract(fmu_path),
            modelIdentifier=description.coSimulation.modelIdentifier,
            instanceName="node",
        )
        # The FMU refuses to instantiate without it, and refusing is the
        # behavior this fixture documents: Event Mode is the node's contract,
        # not an optimisation its importer may decline.
        self._slave.instantiate(
            loggingOn=True,
            eventModeUsed=True,
            earlyReturnAllowed=False,
            logMessage=self._record,
        )

    def _record(self, _instance, status, category, message) -> None:
        """Keep the FMU's own log lines: they carry its internal event times.

        The node logs the time at which it generates a frame, which is the one
        observation that separates an FMI event time from the communication
        point the frame becomes visible at.
        """
        self._log.append(
            f"{category.decode()}/{status}: {message.decode(errors='replace')}"
        )

    def initialize(self) -> Event | None:
        """Leave initialization in Event Mode, and take the initial event.

        With Event Mode in use, initialization ends in Event Mode rather than
        in Step Mode, and a CAN node publishes its bus configuration in that
        first event: its output Clock is already active when it begins. Step
        Mode is entered afterwards, which is what the first step needs.
        """
        self._slave.enterInitializationMode(startTime=0.0)
        self._slave.exitInitializationMode()
        event = self._handle_event(0)
        self._slave.enterStepMode()
        return event

    def step(self, time_ns: int, step_size_ns: int) -> Event | None:
        """One communication step, and the event it ends in, if any."""
        event_needed, terminate, early_return, _ = self._slave.doStep(
            currentCommunicationPoint=time_ns / NS_PER_S,
            communicationStepSize=step_size_ns / NS_PER_S,
        )
        if terminate:
            raise RuntimeError(
                f"fmi3DoStep requested termination at {time_ns} ns"
            )
        if early_return:
            raise RuntimeError(
                f"fmi3DoStep returned early at {time_ns} ns, and this "
                f"exchange declared earlyReturnAllowed false"
            )
        if not event_needed:
            return None
        self._slave.enterEventMode()
        event = self._handle_event(time_ns + step_size_ns)
        self._slave.enterStepMode()
        return event

    def _handle_event(self, time_ns: int) -> Event | None:
        """Read the output Clock, take its buffer, and end the event.

        The Binary value is only defined while its Clock is active, so the
        Clock decides whether the buffer is read at all. Discrete states are
        updated until the FMU stops asking, which is what ends the event: this
        node never asks twice, and iterating rather than assuming that is what
        would expose it if it did.
        """
        payload = None
        if self._slave.getClock([self._clock])[0]:
            # A Binary value the FMU answers as a null pointer is an empty
            # buffer, not a missing one.
            payload = self._slave.getBinary([self._data])[0] or b""
        while True:
            (states_need_update, terminate, _, _,
             next_event_defined, next_event_time) = (
                self._slave.updateDiscreteStates()
            )
            if terminate:
                raise RuntimeError(
                    f"fmi3UpdateDiscreteStates requested termination at "
                    f"{time_ns} ns"
                )
            if not states_need_update:
                break
        if payload is None:
            return None
        return Event(
            time_ns, payload, decode(payload),
            next_event_time if next_event_defined else None,
        )

    def close(self) -> None:
        """Terminate the instance and free it."""
        self._slave.terminate()
        self._slave.freeInstance()


def run_case(fmu_path: Path, case: dict, log: list[str]) -> list[Event]:
    """Every Clock activation one case's step grid makes observable."""
    log.append(f"--- case {case['name']} ---")
    node = Node(fmu_path, log)
    try:
        events = [event for event in [node.initialize()] if event is not None]
        step_size_ns = case["step_size_ns"]
        for time_ns in range(0, case["duration_ns"], step_size_ns):
            event = node.step(time_ns, step_size_ns)
            if event is not None:
                events.append(event)
        return events
    finally:
        node.close()


def compare(case: str, observed: list[Event], expected: list[dict]) -> bool:
    """Report the first event that differs, and whether any did.

    Events are compared whole — time, bytes, and decoded operations — so a
    frame that arrives with the right payload at the wrong communication point
    is a mismatch rather than a pass.
    """
    for index, expectation in enumerate(expected):
        if index >= len(observed):
            print(f"{case}: expected event {index} at "
                  f"{expectation['time_ns']} ns, and the exchange ended")
            return False
        actual = observed[index].as_dict()
        if actual != expectation:
            print(f"{case}: event {index} differs")
            print(f"  expected {json.dumps(expectation, sort_keys=True)}")
            print(f"  observed {json.dumps(actual, sort_keys=True)}")
            return False
    if len(observed) > len(expected):
        extra = observed[len(expected)].as_dict()
        print(f"{case}: unexpected event {len(expected)}: "
              f"{json.dumps(extra, sort_keys=True)}")
        return False
    print(f"{case}: {len(observed)} events, all as expected")
    return True


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fmu", type=Path)
    parser.add_argument("--trace", type=Path, required=True,
                        help="where the observed exchange is written")
    parser.add_argument("--log", type=Path, required=True,
                        help="where the FMU's own log lines are written")
    arguments = parser.parse_args(argv)

    expected = json.loads(EXPECTED.read_text())
    log: list[str] = []
    trace = {"fmu": arguments.fmu.name, "cases": []}
    matched = True
    try:
        for case in expected["cases"]:
            events = run_case(arguments.fmu, case, log)
            trace["cases"].append({
                "name": case["name"],
                "step_size_ns": case["step_size_ns"],
                "duration_ns": case["duration_ns"],
                "events": [event.as_dict() for event in events],
            })
            matched = compare(
                case["name"], events, case["events"]
            ) and matched
    finally:
        # An FMU that refuses a call says why in its own log, and that is the
        # half of the diagnostic the traceback does not carry. It is written
        # on the failing path for that reason, not only on the passing one.
        arguments.trace.write_text(
            json.dumps(trace, indent=2, sort_keys=True) + "\n"
        )
        arguments.log.write_text("\n".join(log) + "\n")
    return 0 if matched else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
