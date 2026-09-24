"""Issue 156: replace one live CAN node at its stable bus terminal."""

import hashlib
import json
import subprocess
from pathlib import Path

from sil import schema
from sil.manifest import Manifest, SubscriberRoute
from sil.recording import read_records

from test_exchange import ARTIFACTS, FAULT_RULE, MODEL, fault_start_values


SCHEMAS = {"can.Buffer": {"fields": [
    {"name": "data_length", "type": "u16"},
    {"name": "data", "type": "u8", "count": 2048},
    {"name": "data_event_time_ns", "type": "u64"},
]}}
CHANNELS = {
    "node1": "node1.CanChannel", "node2": "node2.CanChannel",
    "node3": "node3.CanChannel", "bus1": "bus.Node1",
    "bus2": "bus.Node2", "bus3": "bus.Node3",
}
BOUNDARY = "node1"
PROFILE = "application/org.fmi-standard.fmi-ls-bus.can"
CASES = {
    "contention": {"step": 500_000_000, "duration": 1_500_000_000, "rules": []},
    "fault": {"step": 1_000_000, "duration": 610_000_000, "rules": [FAULT_RULE]},
}


def manifest(name, case, *, recording=None, interceptor=None):
    """Keep bus.Node1 and its rule identity when node1 is replaced."""
    spec = CASES[case]
    replay = recording is not None
    result = Manifest(duration_ns=spec["duration"])
    result.add_schemas(SCHEMAS)
    for channel in CHANNELS:
        if replay and channel == BOUNDARY:
            result.add_channel(channel, schema="can.Buffer", latency_ns=0)
        else:
            result.add_channel(channel, schema="can.Buffer")
    if interceptor:
        result.add_interceptor(BOUNDARY, **interceptor)
    if replay:
        result.add_replay("node1_replay", recording=recording, channels=[BOUNDARY])
    command = ["python", "-m", "sil.fmi"]
    for node in (1, 2, 3):
        if node == 1 and replay:
            continue
        fixture = "FaultAwareReceiver" if node == 3 else "FaultAwareSender"
        command += ["--instance", f"node{node}",
                    str(ARTIFACTS / f"{fixture}.fmu")]
    command += ["--instance", "bus", str(ARTIFACTS / f"{MODEL}.fmu"),
                "--bus-profile", PROFILE]
    for node in (1, 2, 3):
        if node != 1 or not replay:
            command += ["--connect", f"node{node}.CanChannel=bus.Node{node}"]
    if replay:
        command += ["--bind", "node1:data=bus.Node1.Rx_Data"]
    for channel, terminal in CHANNELS.items():
        if replay and channel == BOUNDARY:
            continue
        command += ["--bind", f"{channel}:data={terminal}.Tx_Data"]
    command += ["--start", "bus.activeNodeCount=3"]
    for setting in fault_start_values(spec["rules"]):
        command += ["--start", setting]
    result.add_process(
        "can", command=command, step_period_ns=spec["step"],
        subscribes=[SubscriberRoute(BOUNDARY, capacity=16)] if replay else [],
        publishes=[c for c in CHANNELS if not replay or c != BOUNDARY],
    )
    return result.write(ARTIFACTS / f"issue156-{name}.json").path


def run(path, name, *, success=True):
    recording = ARTIFACTS / f"issue156-{name}.mcap"
    process = subprocess.run(
        ["/opt/kernel/sil-run", str(path), "--participant-timeout-ms", "5000",
         "-o", str(recording)], capture_output=True, text=True, timeout=120,
    )
    (ARTIFACTS / f"issue156-{name}.log").write_text(process.stdout + process.stderr)
    assert process.returncode == (0 if success else 1), process.stdout + process.stderr
    return recording


def operations(path):
    """Per Channel, preserve Publish order, payload bytes and FMI event time."""
    codec = schema.load(SCHEMAS)["can.Buffer"]
    result = {channel: [] for channel in CHANNELS}
    for channel, _slot, data in read_records(path):
        fields = codec.unpack(data)
        result[channel].append((fields["data_event_time_ns"],
                                bytes(fields["data"][:fields["data_length"]]).hex()))
    return result


def compare(live, replay):
    left, right = operations(live), operations(replay)
    return {channel: {"equal": left[channel] == right[channel],
                      "live": left[channel], "replay": right[channel]}
            for channel in CHANNELS}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_live_replay_equivalence_and_deliberate_failures():
    report = {"cases": {}, "negative": {}, "sha256": {}}
    for case in CASES:
        live_manifest = manifest(f"{case}-live", case)
        live = run(live_manifest, f"{case}-live")
        replay_manifest = manifest(f"{case}-replay", case, recording=live)
        replay = run(replay_manifest, f"{case}-replay")
        comparison = compare(live, replay)
        assert all(row["equal"] for row in comparison.values()), comparison
        observed = operations(replay)
        if case == "contention":
            # Two operations in one activation at the Step end. Their two
            # bus completions remain distinct instants inside the next Step.
            assert any(t == 1_000_000_000 and len(bytes.fromhex(data)) == 40
                       for t, data in observed[BOUNDARY])
            assert [t for t, _ in observed["bus3"]] == [
                500_830_000, 1_000_830_000, 1_001_690_000,
            ]
            assert [t for t, _ in observed["node2"] if t] == [
                500_000_000, 1_000_000_000, 1_500_000_000,
            ]
        else:
            # Rule 1 selects bus.Node1 despite node1 being replaced. It is
            # consumed once: the 600 ms request succeeds without another
            # Bus Error. Node2 co-transmits in both Runs.
            assert [t for t, payload in observed["bus1"]
                    if payload.startswith("31000000")] == [300_830_000]
            assert [t for t, payload in observed["bus3"]
                    if payload.startswith("10000000")] == [
                        301_690_000, 600_830_000,
                    ]
        # The determinism check is within each Manifest, never across them.
        for label, path, source in (("live", live_manifest, live),
                                     ("replay", replay_manifest, replay)):
            repeated = run(path, f"{case}-{label}-repeat")
            assert sha256(repeated) == sha256(source)
            report["sha256"][source.name] = sha256(source)
            report["sha256"][repeated.name] = sha256(repeated)
            report["sha256"][path.name] = sha256(path)
        report["cases"][case] = comparison
        if case != "fault":
            continue
        for variant, change in {
            "dropped": {"kind": "drop", "start_ns": 299_000_000,
                        "end_ns": 300_000_000},
            "retimed": {"kind": "override", "field": "data_event_time_ns",
                        "value": 299_500_000, "start_ns": 299_000_000,
                        "end_ns": 300_000_000},
        }.items():
            name = f"fault-{variant}"
            altered_manifest = manifest(name, case, recording=live, interceptor=change)
            altered = run(altered_manifest, name)
            differences = compare(live, altered)
            assert not differences[BOUNDARY]["equal"]
            assert any(not row["equal"] for channel, row in differences.items()
                       if channel != BOUNDARY)
            report["negative"][variant] = differences
            report["sha256"][altered_manifest.name] = sha256(altered_manifest)
            report["sha256"][altered.name] = sha256(altered)
        unreachable = manifest(
            "fault-unreachable", case, recording=live,
            interceptor={"kind": "override", "field": "data_event_time_ns",
                         "value": 200_000_000, "start_ns": 299_000_000,
                         "end_ns": 300_000_000},
        )
        run(unreachable, "fault-unreachable", success=False)
        failure = (ARTIFACTS / "issue156-fault-unreachable.log").read_text()
        assert "participant 'can' failed" in failure
        assert "200000000 ns" in failure and "299000000 ns" in failure
        report["negative"]["unreachable"] = {
            "exit_code": 1,
            "stated_instant_ns": 200_000_000,
            "arrival_step_start_ns": 299_000_000,
            "arrival_step_end_ns": 300_000_000,
        }
        report["sha256"][unreachable.name] = sha256(unreachable)
    for archive in ("FaultAwareSender", "FaultAwareReceiver", MODEL):
        path = ARTIFACTS / f"{archive}.fmu"
        report["sha256"][path.name] = sha256(path)
    for artifact in ARTIFACTS.glob("issue156-*"):
        if (artifact.is_file() and artifact.name != "issue156-report.json"
                and (artifact.suffix != ".log"
                     or artifact.name == "issue156-fault-unreachable.log")):
            report["sha256"][artifact.name] = sha256(artifact)
    report["fixed_input_boundary"] = (
        "Equivalence holds for identical physical inputs and a fixed source "
        "Recording; changing a closed-loop receiver can change the removed "
        "node's future output and is outside this claim."
    )
    (ARTIFACTS / "issue156-report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
