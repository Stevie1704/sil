"""Issue 157: bounded resources and isolation under malformed traffic.

The native checks in `abi.cpp` and `capacity.cpp` cover the C entry points
under sanitizers. These tests drive the packaged archive: two instances of
one loaded library through FMPy, and malformed traffic across a SiL Run.
"""

import json
import subprocess

import pytest
from fmpy.fmi1 import FMICallException

from fmpy import extract, read_model_description
from fmpy.fmi3 import FMU3Slave

from sil import schema
from sil.manifest import Manifest, SubscriberRoute
from sil.recording import read_records

from test_exchange import (
    ARTIFACTS, BUFFER_SCHEMAS, FRAME, LAYOUT, MODEL, ROOT, THREE_REQUESTS,
    UNKNOWN_OPERATION, config, confirm, drive, drive_steps, fault_parameters,
    format_error, frame, frame_end_ns, sil_run, deliver, initialized_fmu,
)

RATE = 125_000
PAYLOAD_MISMATCH = FRAME[:14] + b"\x05\x00" + FRAME[16:]
BEYOND_STANDARD_ID = frame(0x800, b"")
TRUNCATED_HEADER = FRAME[:5]
EXTENDED = FRAME[:12] + b"\x01" + FRAME[13:]
SCENARIOS = {
    # A Format Error report and a frame on two nodes.
    "two": {
        "nodes": 2,
        "parameters": [],
        "requests": [
            (0, 0, config(RATE)), (0, 1, config(RATE)),
            (1000, 0, frame(1, b"\x01")),
            (1000, 1, UNKNOWN_OPERATION + frame(2, b"")),
        ],
        "until": 1_000_000,
    },
    # Contention, a discarding node and a scheduled transmission error.
    "three": {
        "nodes": 3,
        "parameters": [(LAYOUT["active_nodes"], 3), (LAYOUT["queue_capacity"], 2)]
        + fault_parameters([{
            "kind": 1, "sender_node": 2, "receiver_node": 0, "identifier": 0,
            "request_start_ns": 1000, "request_end_ns": 1000,
            "occurrence": 1, "attempt": 1,
        }]),
        "requests": THREE_REQUESTS,
        "until": 1_500_000,
    },
}


def test_instances_sharing_one_library_stay_isolated(tmp_path):
    path = ARTIFACTS / f"{MODEL}.fmu"
    description = read_model_description(path)
    # One extraction: every instance comes from the same loaded library.
    unpacked = extract(path, unzipdir=str(tmp_path / "bus"))

    def instantiate(name):
        fmu = FMU3Slave(
            guid=description.guid,
            unzipDirectory=unpacked,
            modelIdentifier=description.coSimulation.modelIdentifier,
            instanceName=name,
        )
        fmu.instantiate(eventModeUsed=True)
        parameters = SCENARIOS[name]["parameters"]
        if parameters:
            refs, values = zip(*parameters)
            fmu.setFloat64(list(refs), [float(value) for value in values])
        fmu.enterInitializationMode(startTime=0)
        fmu.exitInitializationMode()
        return fmu

    alone = {}
    for name, scenario in SCENARIOS.items():
        fmu = instantiate(name)
        alone[name] = drive(fmu, scenario["requests"], scenario["until"],
                            nodes=scenario["nodes"])
        fmu.freeInstance()
    assert alone["two"][0] == (1001, 1, format_error(UNKNOWN_OPERATION))

    fmus = {name: instantiate(name) for name in SCENARIOS}
    traces = {name: [] for name in SCENARIOS}
    masters = [
        drive_steps(fmus[name], scenario["requests"], scenario["until"],
                    traces[name], nodes=scenario["nodes"])
        for name, scenario in SCENARIOS.items()
    ]
    # Alternate every Step of both instances: queues, fault state, clocks
    # and counters must not couple.
    while masters:
        for master in list(masters):
            if next(master, StopIteration) is StopIteration:
                masters.remove(master)
    for fmu in fmus.values():
        fmu.freeInstance()
    assert traces == alone


def request_manifest(name, schedule):
    """A scripted source feeding raw operations into two bus terminals."""
    manifest = Manifest(duration_ns=1_000_000)
    manifest.add_schemas(BUFFER_SCHEMAS)
    requests, observed = ["in.node1", "in.node2"], ["out.node1", "out.node2"]
    for channel in requests:
        manifest.add_channel(channel, schema="can.Buffer", latency_ns=0)
    for channel in observed:
        manifest.add_channel(channel, schema="can.Buffer")
    manifest.add_process(
        "source",
        command=["python", str(ROOT / "models/can/tests/source.py"),
                 json.dumps([[channel, t, data.hex()] for channel, t, data in schedule])],
        step_period_ns=1_000_000,
        publishes=requests,
    )
    command = ["python", "-m", "sil.fmi", "--instance", "bus",
               str(ARTIFACTS / f"{MODEL}.fmu"),
               "--bus-profile", "application/org.fmi-standard.fmi-ls-bus.can"]
    for node in (1, 2):
        command += ["--bind", f"in.node{node}:data=bus.Node{node}.Rx_Data"]
        command += ["--bind", f"out.node{node}:data=bus.Node{node}.Tx_Data"]
    manifest.add_process(
        "can", command=command, step_period_ns=1_000_000,
        subscribes=[SubscriberRoute(channel, capacity=8) for channel in requests],
        publishes=observed, priority=1,
    )
    return manifest.write(ARTIFACTS / f"issue157-{name}.json").path


def test_sil_run_reports_corrupt_operations_and_fails_unsupported_ones(tmp_path):
    configured = [("in.node1", 0, config(RATE)), ("in.node2", 0, config(RATE))]
    # Two Messages of one terminal at one instant reach the bus as one event;
    # a truncated header makes the rest of that buffer one corrupt operation.
    path = request_manifest("format-error", configured + [
        ("in.node1", 1000, UNKNOWN_OPERATION),
        ("in.node1", 1000, frame(1, b"")),
        ("in.node2", 1000, PAYLOAD_MISMATCH),
        ("in.node2", 1000, BEYOND_STANDARD_ID),
        ("in.node2", 1000, TRUNCATED_HEADER),
    ])
    recordings = [ARTIFACTS / f"issue157-format-error-{run}.mcap"
                  for run in ("first", "second")]
    for recording in recordings:
        sil_run(path, recording)
    assert recordings[0].read_bytes() == recordings[1].read_bytes()
    codec = schema.load(BUFFER_SCHEMAS)["can.Buffer"]
    trace = []
    for channel, _, raw in read_records(recordings[0]):
        if channel.startswith("out."):
            fields = codec.unpack(raw)
            trace.append((fields["data_event_time_ns"], int(channel[-1]) - 1,
                          bytes(fields["data"][:fields["data_length"]])))
    end = frame_end_ns(1000, 1, b"", RATE)
    expected = [
        (1001, 0, format_error(UNKNOWN_OPERATION)),
        (1001, 1, format_error(PAYLOAD_MISMATCH) + format_error(BEYOND_STANDARD_ID)
         + format_error(TRUNCATED_HEADER)),
        (end, 0, confirm(1)),
        (end, 1, frame(1, b"")),
    ]
    assert sorted(trace) == expected

    failures = {
        "unsupported": (EXTENDED, "only 11-bit Classical CAN data frames"),
        "queue-exhausted": (b"".join(frame(k, b"") for k in range(1, 7)),
                            "per-node CAN queue is full"),
    }
    evidence = {"format-error": [(t, node, data.hex()) for t, node, data in expected]}
    for name, (operation, diagnostic) in failures.items():
        path = request_manifest(name, configured + [("in.node1", 1000, operation)])
        # A fresh invocation directory shows what the failed Run left behind.
        invocation = tmp_path / name
        invocation.mkdir()
        result = subprocess.run(
            ["/opt/kernel/sil-run", str(path), "--no-recording",
             "--provenance", str(ARTIFACTS / f"issue157-{name}.provenance.json"),
             "--participant-timeout-ms", "5000"],
            cwd=invocation, capture_output=True, text=True, timeout=60,
        )
        (ARTIFACTS / f"issue157-{name}.log").write_text(result.stdout + result.stderr)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "fmi3UpdateDiscreteStates" in result.stderr
        assert diagnostic in result.stderr
        # The Run working directory, participants' directories and the
        # extracted FMU are gone after the failure.
        assert list(invocation.iterdir()) == []
        evidence[name] = {"exit_code": result.returncode, "diagnostic": diagnostic}
    (ARTIFACTS / "issue157-sil.json").write_text(json.dumps(evidence, indent=2) + "\n")


def test_run_selects_a_smaller_operation_buffer_below_the_packaged_maxsize():
    description = read_model_description(ARTIFACTS / f"{MODEL}.fmu")
    packaged = {v.name: v for v in description.modelVariables}
    assert packaged["Node1.Rx_Data"].maxSize == 2048
    assert packaged["perTerminalBufferCapacity"].start == "2048"
    within = config(RATE) + frame(1, b"") * 3   # 23 + 48 = 71 bytes
    capacity = [(LAYOUT["buffer_capacity"], len(within))]
    with initialized_fmu(parameters=capacity) as bus:
        assert bus.getFloat64([LAYOUT["buffer_capacity"]]) == [len(within)]
        deliver(bus, config(RATE), 1)
        deliver(bus, within, 0)
        bus.updateDiscreteStates()
    with initialized_fmu(parameters=capacity) as bus:
        deliver(bus, config(RATE), 1)
        deliver(bus, within + frame(2, b""), 0)
        with pytest.raises(FMICallException):
            bus.updateDiscreteStates()


@pytest.mark.parametrize("capacity", [63, 2049, 64.5])
def test_buffer_capacity_outside_its_range_fails_at_set_float64(capacity):
    with pytest.raises(FMICallException):
        with initialized_fmu(parameters=[(LAYOUT["buffer_capacity"], capacity)],
                             exit_initialization=False):
            pass
