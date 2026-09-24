"""Issue 156: replace one live CAN node at its stable bus terminal."""

import hashlib
import json
import re
import subprocess

from sil import schema
from sil.manifest import Manifest, SubscriberRoute
from sil.recording import read_records

from test_exchange import (
    ARTIFACTS, BIT_TIME_NS, BUFFER_SCHEMAS, CONFIRM, FIRST_ERROR_END_NS,
    FRAME, LATER_END_NS, MODEL, RETRY_END_NS, ROOT, UPSTREAM_PAYLOAD,
    bus_error, confirm, fault_start_values, frame, frame_end_ns, sil_run,
)
import wire


CHANNELS = {
    "node1": "node1.CanChannel", "node2": "node2.CanChannel",
    "bus1": "bus.Node1", "bus2": "bus.Node2",
}
BOUNDARY = "node1"
RETAINED = tuple(channel for channel in CHANNELS if channel != BOUNDARY)
CAN_PROFILE_MIME = "application/org.fmi-standard.fmi-ls-bus.can"
GAP_NS = wire.INTERMISSION * BIT_TIME_NS
FAULT_RULE = {
    "kind": 1, "sender_node": 1, "receiver_node": 0, "identifier": 1,
    "request_start_ns": 300_000_000, "request_end_ns": 600_000_000,
    "occurrence": 1, "attempt": 1,
}
CASES = {
    "contention": {"step": 500_000_000, "duration": 1_500_000_000, "rules": []},
    "scheduled-error": {"step": 1_000_000, "duration": 610_000_000,
                        "rules": [FAULT_RULE]},
}


def write_can_manifest(name, case, *, recording=None, interceptor=None):
    """Keep bus.Node1 and its rule identity when node1 is replaced."""
    spec = CASES[case]
    replay = recording is not None
    result = Manifest(duration_ns=spec["duration"])
    result.add_schemas(BUFFER_SCHEMAS)
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
    for node in (1, 2):
        if node == 1 and replay:
            continue
        fixture = "FaultAwareSender" if node == 1 else "ContendingSender"
        command += ["--instance", f"node{node}",
                    str(ARTIFACTS / f"{fixture}.fmu")]
    command += ["--instance", "bus", str(ARTIFACTS / f"{MODEL}.fmu"),
                "--bus-profile", CAN_PROFILE_MIME]
    for node in (1, 2):
        if node != 1 or not replay:
            command += ["--connect", f"node{node}.CanChannel=bus.Node{node}"]
    if replay:
        command += ["--bind", "node1:data=bus.Node1.Rx_Data"]
    for channel, terminal in CHANNELS.items():
        if replay and channel == BOUNDARY:
            continue
        command += ["--bind", f"{channel}:data={terminal}.Tx_Data"]
    for setting in fault_start_values(spec["rules"]):
        command += ["--start", setting]
    result.add_process(
        "can", command=command, step_period_ns=spec["step"],
        subscribes=[SubscriberRoute(BOUNDARY, capacity=16)] if replay else [],
        publishes=[c for c in CHANNELS if not replay or c != BOUNDARY],
    )
    return result.write(ARTIFACTS / f"issue156-{name}.json").path


def run_expected_failure(path, name):
    recording = ARTIFACTS / f"issue156-{name}.mcap"
    process = subprocess.run(
        ["/opt/kernel/sil-run", str(path), "--participant-timeout-ms", "5000",
         "-o", str(recording)], capture_output=True, text=True, timeout=120,
    )
    (ARTIFACTS / f"issue156-{name}.log").write_text(process.stdout + process.stderr)
    assert process.returncode == 1, process.stdout + process.stderr
    return process.stderr.splitlines()[-1]


def operations(path):
    """Per Channel, preserve Publish order, payload bytes and FMI event time."""
    codec = schema.load(BUFFER_SCHEMAS)["can.Buffer"]
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


def run_success(path, name, report):
    """Check one successful Manifest against itself, including Interceptors."""
    recording = ARTIFACTS / f"issue156-{name}.mcap"
    sil_run(path, recording)
    repeat = ARTIFACTS / f"issue156-{name}-repeat.mcap"
    sil_run(path, repeat)
    assert sha256(recording) == sha256(repeat)
    report["determinism"][name] = {
        "manifest": path.name,
        "first_recording_sha256": sha256(recording),
        "repeat_recording_sha256": sha256(repeat),
    }
    return recording


def assert_contention(observed):
    # Both senders have multiple operations at 1000 ms. ID 1 wins twice;
    # ID 2 loses and remains queued, then transmits twice afterward.
    assert any(t == 1_000_000_000 and bytes.fromhex(data) == FRAME * 2
               for t, data in observed[BOUNDARY])
    assert [t for t, _ in observed["node2"] if t] == [
        500_000_000, 1_000_000_000, 1_500_000_000,
    ]
    first = frame_end_ns(500_000_000)
    second = frame_end_ns(first + GAP_NS, identifier=2)
    third = frame_end_ns(1_000_000_000)
    fourth = frame_end_ns(third + GAP_NS)
    fifth = frame_end_ns(fourth + GAP_NS, identifier=2)
    sixth = frame_end_ns(fifth + GAP_NS, identifier=2)
    assert observed["bus1"] == [
        (first, CONFIRM.hex()), (second, frame(2, UPSTREAM_PAYLOAD).hex()),
        (third, CONFIRM.hex()), (fourth, CONFIRM.hex()),
        (fifth, frame(2, UPSTREAM_PAYLOAD).hex()),
        (sixth, frame(2, UPSTREAM_PAYLOAD).hex()),
    ]
    assert observed["bus2"] == [
        (first, FRAME.hex()), (second, confirm(2).hex()),
        (third, FRAME.hex()), (fourth, FRAME.hex()),
        (fifth, confirm(2).hex()), (sixth, confirm(2).hex()),
    ]


def assert_scheduled_error(observed):
    # The rule matches Node1's first ID 1 request; Node2's ID 2 request
    # loses arbitration and waits through the error and retry.
    assert observed["bus1"][0] == (
        FIRST_ERROR_END_NS, bus_error(1, 1, 1).hex(),
    )
    assert observed["bus2"][0] == (
        FIRST_ERROR_END_NS, bus_error(1, 2, 0).hex(),
    )
    assert (RETRY_END_NS, CONFIRM.hex()) in observed["bus1"]
    assert (RETRY_END_NS, FRAME.hex()) in observed["bus2"]
    assert (LATER_END_NS, CONFIRM.hex()) in observed["bus1"]
    assert (LATER_END_NS, FRAME.hex()) in observed["bus2"]
    assert [t for t, payload in observed["bus1"]
            if bytes.fromhex(payload) == bus_error(1, 1, 1)] == [
                FIRST_ERROR_END_NS,
            ]


def qualify_case(case, report):
    live_manifest = write_can_manifest(f"{case}-live", case)
    live = run_success(live_manifest, f"{case}-live", report)
    replay_manifest = write_can_manifest(f"{case}-replay", case, recording=live)
    replay = run_success(replay_manifest, f"{case}-replay", report)
    result = compare(live, replay)
    assert all(row["equal"] for row in result.values()), result
    if case == "contention":
        assert_contention(operations(replay))
    else:
        assert_scheduled_error(operations(replay))
    report["cases"][case] = result
    return live


def qualify_interceptors(source_recording, report):
    changes = {
        "intercept-dropped": {"kind": "drop", "start_ns": 299_000_000,
                              "end_ns": 300_000_000},
        "intercept-retimed": {"kind": "override", "field": "data_event_time_ns",
                              "value": 299_500_000, "start_ns": 299_000_000,
                              "end_ns": 300_000_000},
    }
    for name, interceptor in changes.items():
        path = write_can_manifest(name, "scheduled-error",
                                  recording=source_recording,
                                  interceptor=interceptor)
        altered = run_success(path, name, report)
        result = compare(source_recording, altered)
        assert not result[BOUNDARY]["equal"]
        assert any(not result[channel]["equal"] for channel in RETAINED)
        report["negative"][name] = result
    path = write_can_manifest(
        "intercept-unreachable", "scheduled-error", recording=source_recording,
        interceptor={"kind": "override", "field": "data_event_time_ns",
                     "value": 200_000_000, "start_ns": 299_000_000,
                     "end_ns": 300_000_000},
    )
    diagnostic = run_expected_failure(path, "intercept-unreachable")
    assert "200000000 ns" in diagnostic and "299000000 ns" in diagnostic
    report["negative"]["intercept-unreachable"] = {
        "exit_code": 1, "diagnostic": diagnostic,
    }


def check_upstream_evidence(report):
    """Recheck retained upstream Recording digests and three replay verdicts."""
    fixture = ROOT / "proofs/fmi-ls-bus"
    evidence = fixture / "evidence"
    provenance = (evidence / "replay-provenance.txt").read_text()
    found = re.findall(
        r"(?m)^  recording\s+(\S+\.mcap) sha256 ([0-9a-f]{64})",
        provenance,
    )
    assert len(found) == 8
    assert all(sha256(evidence / name) == expected for name, expected in found)
    report["upstream_evidence"] = {name: expected for name, expected in found}
    for case, live in (("aligned", "connected-aligned"),
                       ("quantised", "connected-quantised"),
                       ("coarse", "live-coarse")):
        verdict = subprocess.run(
            ["python", str(fixture / "replay_equivalence.py"), case,
             str(evidence / f"{live}.mcap"),
             str(evidence / f"replay-{case}.mcap")],
            capture_output=True, text=True, timeout=30,
        )
        assert verdict.returncode == 0, verdict.stdout + verdict.stderr


def test_live_replay_equivalence_and_deliberate_failures():
    report = {"cases": {}, "negative": {}, "determinism": {}, "sha256": {}}
    qualify_case("contention", report)
    source_recording = qualify_case("scheduled-error", report)
    qualify_interceptors(source_recording, report)
    check_upstream_evidence(report)
    for archive in ("FaultAwareSender", "ContendingSender", MODEL):
        path = ARTIFACTS / f"{archive}.fmu"
        report["sha256"][path.name] = sha256(path)
    names = (
        "contention-live", "contention-replay", "scheduled-error-live",
        "scheduled-error-replay", "intercept-dropped", "intercept-retimed",
    )
    retained = [ARTIFACTS / f"issue156-{name}.json" for name in names]
    retained.append(ARTIFACTS / "issue156-intercept-unreachable.json")
    recordings = [ARTIFACTS / f"issue156-{name}.mcap" for name in names]
    retained.extend(recordings)
    retained.extend(path.with_suffix(".mcap.provenance.json")
                    for path in recordings)
    report["sha256"].update({path.name: sha256(path) for path in retained})
    revision = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True,
    ).strip()
    sidecar = json.loads(recordings[0].with_suffix(
        ".mcap.provenance.json"
    ).read_text())
    assert sidecar["sil"]["source_revision"] == revision
    report["qualification"] = {
        "source_revision": revision,
        "source_dirty": bool(subprocess.check_output(
            ["git", "-C", str(ROOT), "status", "--porcelain",
             "--untracked-files=no"], text=True,
        ).strip()),
        "image_id": (ARTIFACTS / "image.txt").read_text().strip(),
        "test_sha256": sha256(ROOT / "models/can/tests/test_replay_equivalence.py"),
        "contending_patch_sha256": sha256(
            ROOT / "models/can/qualification/contending-node.patch"
        ),
    }
    report["fixed_input_boundary"] = (
        "Equivalence holds for identical physical inputs and a fixed source "
        "Recording; changing a closed-loop receiver can change the removed "
        "node's future output and is outside this claim."
    )
    (ARTIFACTS / "issue156-report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
