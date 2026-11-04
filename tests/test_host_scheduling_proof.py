"""Independent examples at the #362 proof's offline report boundary."""
import json
import hashlib
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest


REPORT = Path(__file__).resolve().parents[1] / "proofs/host-scheduling/report.py"


def capture_report(tmp_path, event_text, *, marker_begin=10, marker_end=55,
                   stop_reason=None, activation_begin=10):
    capture = tmp_path / "capture"
    capture.mkdir()
    (tmp_path / "identity.json").write_text(json.dumps({"pid_namespace": [5, 11]}))
    (capture / "events.txt").write_text(event_text)
    (capture / "events-errors.txt").write_text("")
    (capture / "stats.txt").write_text("")
    events = ("sched_switch", "sched_wakeup", "sched_wakeup_new", "sched_process_fork",
              "sched_process_exec", "sched_process_exit")
    (capture / "attributes.txt").write_text("\n".join(
        f"sched:{event}: use_clockid: 1, clockid: 1" for event in events))
    observations = [
        {"sil_activation_observations": 1, "clock": "host_monotonic", "runner_pid": 1,
         "pid_namespace": {"device": 5, "inode": 11}},
        {"kind": "phase", "begin_ns": "2", "end_ns": "3"},
        {"kind": "spawn", "participant": "slow", "pid": 7, "at_ns": "4"},
        {"kind": "activation", "task_kind": "process_step", "participant": "slow",
         "index": 0, "virtual_time_ns": 0, "begin_ns": str(activation_begin), "end_ns": "60",
         "outcome": "completed"}, {"kind": "end"}]
    (capture / "activations.jsonl").write_text("\n".join(map(json.dumps, observations)))
    marker = {"namespace": [5, 11], "participant": "slow", "phase": "step",
              "index": 0, "virtual_ns": 0, "pid": 7, "tid": 7}
    (capture / "markers.jsonl").write_text("\n".join(map(json.dumps, [
        {**marker, "edge": "begin", "host_ns": marker_begin},
        {**marker, "edge": "end", "host_ns": marker_end}])))
    result = {"initial_pid_namespace": True, "run": {"exit_code": 0},
              "exports": {key: {"exit_code": 0} for key in ("events", "attributes", "stats")}}
    if stop_reason:
        result["collector_stop_reason"] = stop_reason
    (capture / "result.json").write_text(json.dumps(result))
    output = tmp_path / "bound.json"
    completed = subprocess.run([sys.executable, str(REPORT.with_name("decode.py")),
                                str(capture), str(output)], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    return json.loads(output.read_text())


ROOT_AND_CHILD = "\n".join([
    "[000] 0.000000001: sched:sched_process_fork: comm=supervisor pid=99 child_comm=runner child_pid=1",
    "[000] 0.000000005: sched:sched_process_fork: comm=runner pid=1 child_comm=child child_pid=7",
]) + "\n"


def test_begin_marker_at_exclusive_activation_end_does_not_verify_clock(tmp_path):
    bound = capture_report(tmp_path, ROOT_AND_CHILD, marker_begin=60, marker_end=60)
    assert bound["analysis"]["clock_verified"] is False


@pytest.mark.parametrize("parent", [88, 1])
def test_reused_descendant_tid_cannot_inherit_old_spawn(tmp_path, parent):
    events = ROOT_AND_CHILD + "\n".join([
        "[000] 0.000000010: sched:sched_switch: idle:0 [120] R ==> child:7 [120]",
        "[000] 0.000000020: sched:sched_switch: child:7 [120] S ==> idle:0 [120]",
        "[000] 0.000000025: sched:sched_process_exit: comm=child pid=7 prio=120",
        f"[000] 0.000000035: sched:sched_process_fork: comm=other pid={parent} child_comm=replacement child_pid=7",
        "[000] 0.000000036: sched:sched_wakeup: unrelated:7 [120] CPU:000",
        "[000] 0.000000040: sched:sched_switch: idle:0 [120] R ==> unrelated:7 [120]",
        "[000] 0.000000060: sched:sched_switch: unrelated:7 [120] S ==> idle:0 [120]",
    ])
    bound = capture_report(tmp_path, events, marker_begin=40, activation_begin=40)
    assert bound["analysis"]["namespace_verified"] is False
    assert bound["analysis"]["windows"][0]["states_ns"]["unknown"] == 20
    assert len([life for life in bound["analysis"]["lifetimes"] if life["tid"] == 7]) == (2 if parent == 1 else 1)


@pytest.mark.parametrize("stop_reason, code", [("interrupted", "collector_interrupted"),
                                             ("size_guard", "collector_size_limit")])
def test_collector_stop_reason_survives_capture_binding(tmp_path, stop_reason, code):
    events = ROOT_AND_CHILD + "\n".join([
        "[000] 0.000000010: sched:sched_switch: idle:0 [120] R ==> child:7 [120]",
        "[000] 0.000000055: sched:sched_switch: child:7 [120] S ==> idle:0 [120]",
    ])
    bound = capture_report(tmp_path, events, stop_reason=stop_reason)
    assert code in bound["analysis"]["reasons"]
    assert bound["analysis"]["collector_stop_reason"] == stop_reason
    assert bound["analysis"]["windows"][0]["states_ns"] == {
        "running": 45, "runnable": 0, "sleeping": 0, "unknown": 5}


def test_exclusive_fixture_end_may_coincide_with_activation_end(tmp_path):
    bound = capture_report(tmp_path, ROOT_AND_CHILD, marker_begin=10, marker_end=60)
    assert bound["analysis"]["clock_verified"] is True


def report(tmp_path, events, **changes):
    document = {"clock": "host_monotonic", "namespace_verified": True,
        "loss": False, "truncated": False,
        "events": events,
        "windows": [{"participant": "slow", "index": 0, "virtual_ns": 0,
                     "tid": 7, "begin_ns": 10, "end_ns": 60,
                     "outcome": "completed"}], **changes}
    source = tmp_path / "input.json"
    target = tmp_path / "report.json"
    source.write_text(json.dumps(document))
    result = subprocess.run([sys.executable, str(REPORT), str(source), str(target)],
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return json.loads(target.read_text())


def fork(t, child=7):
    return {"time_ns": t, "cpu": 0, "event": "fork", "parent_pid": 1,
            "child_pid": child}


def switch(t, prev, nxt, state="S"):
    return {"time_ns": t, "cpu": 0, "event": "switch", "prev_pid": prev,
            "next_pid": nxt, "prev_state": state}


def wake(t, pid=7):
    return {"time_ns": t, "cpu": 0, "event": "wakeup", "pid": pid}


def test_direct_events_partition_half_open_step_with_unknown_edges(tmp_path):
    result = report(tmp_path, [fork(5), wake(10), switch(15, 0, 7),
        switch(25, 7, 0), wake(40), switch(45, 0, 7), switch(55, 7, 0)])
    assert result["windows"][0]["states_ns"] == {
        "running": 20, "runnable": 10, "sleeping": 15, "unknown": 5}
    assert result["windows"][0]["intervals"] == [
        [10, 15, "runnable"], [15, 25, "running"], [25, 40, "sleeping"],
        [40, 45, "runnable"], [45, 55, "running"], [55, 60, "unknown"]]


def test_initial_and_final_unobserved_state_stays_unknown(tmp_path):
    window = {"participant": "slow", "index": 0, "virtual_ns": 0, "tid": 7,
              "begin_ns": 10, "end_ns": 70, "outcome": "completed"}
    result = report(tmp_path, [fork(5), wake(20), switch(30, 0, 7),
        switch(40, 7, 0), wake(50), switch(60, 0, 7)], windows=[window])
    assert result["windows"][0]["intervals"] == [[10, 20, "unknown"],
        [20, 30, "runnable"], [30, 40, "running"], [40, 50, "sleeping"],
        [50, 60, "runnable"], [60, 70, "unknown"]]


def test_transition_on_window_edge_belongs_to_following_window(tmp_path):
    windows = [{"participant": "slow", "index": i, "virtual_ns": 0, "tid": 7,
                "begin_ns": a, "end_ns": b, "outcome": "completed"}
               for i, a, b in ((0, 10, 60), (1, 60, 70))]
    result = report(tmp_path, [fork(5), switch(10, 0, 7), switch(30, 7, 0, "R+"),
        switch(40, 0, 7), switch(60, 7, 0), wake(70)], windows=windows)
    assert result["windows"][0]["states_ns"] == {
        "running": 40, "runnable": 10, "sleeping": 0, "unknown": 0}
    assert result["windows"][1]["states_ns"] == {
        "running": 0, "runnable": 0, "sleeping": 10, "unknown": 0}


@pytest.mark.parametrize("switch_state, observed", [("R+", 1), ("S", 0)])
def test_contended_phase_requires_observed_runnable_state(tmp_path, switch_state, observed):
    window = {"kind": "fixture_phase", "phase": "contended", "participant": "slow",
              "index": 0, "virtual_ns": 0, "tid": 7, "begin_ns": 10,
              "end_ns": 50, "outcome": "fixture_closed"}
    result = report(tmp_path, [fork(5), switch(10, 0, 7),
        switch(20, 7, 0, switch_state), wake(50), switch(60, 0, 7)], windows=[window])
    assert result["phase_evidence"]["contended"] == {
        "expected_state": "runnable", "windows": 1, "observed_windows": observed}


def test_missing_wakeup_does_not_turn_entire_gap_into_sleep(tmp_path):
    result = report(tmp_path, [fork(5), switch(10, 0, 7),
        switch(20, 7, 0), switch(50, 0, 7), switch(60, 7, 0)])
    assert result["windows"][0]["states_ns"] == {
        "running": 20, "runnable": 0, "sleeping": 0, "unknown": 30}
    assert "missing_wakeup" in result["reasons"]


def test_pid_reuse_and_equal_virtual_times_keep_lifetimes_separate(tmp_path):
    windows = [{"participant": name, "index": 0, "virtual_ns": 0, "tid": 7,
                "begin_ns": a, "end_ns": b, "outcome": "completed"}
               for name, a, b in (("first", 10, 20), ("second", 40, 50))]
    result = report(tmp_path, [fork(5), switch(10, 0, 7), switch(20, 7, 0),
        {"time_ns": 21, "cpu": 0, "event": "exit", "pid": 7},
        fork(35), switch(40, 0, 7), switch(50, 7, 0)], windows=windows)
    assert [w["states_ns"]["running"] for w in result["windows"]] == [10, 10]
    assert result["windows"][0]["lifetime"] != result["windows"][1]["lifetime"]


def test_preemption_is_runnable_and_exec_preserves_lifetime(tmp_path):
    result = report(tmp_path, [fork(5), switch(10, 0, 7),
        switch(20, 7, 0, "R+"), switch(30, 0, 7),
        {"time_ns": 35, "cpu": 0, "event": "exec", "pid": 7, "old_pid": 7},
        switch(60, 7, 0)])
    assert result["windows"][0]["states_ns"]["runnable"] == 10
    assert len(result["lifetimes"]) == 1
    assert result["lifetimes"][0]["exec_ns"] == [35]


@pytest.mark.parametrize("change", [{"loss": True}, {"truncated": True},
    {"clock": "unknown"}, {"namespace_verified": False}])
def test_loss_truncation_clock_or_namespace_failure_keeps_time_unknown(tmp_path, change):
    events = [fork(5), switch(10, 0, 7), switch(60, 7, 0)]
    result = report(tmp_path, events, **change)
    assert result["windows"][0]["states_ns"]["unknown"] == 50


def test_failed_and_unclosed_steps_never_enter_completed_statistics(tmp_path):
    windows = [{"participant": "slow", "index": i, "virtual_ns": 0,
                "tid": 7, "begin_ns": 10, "end_ns": end, "outcome": outcome}
               for i, end, outcome in ((0, 60, "failed"), (1, None, "unclosed"))]
    result = report(tmp_path, [fork(5), switch(10, 0, 7), switch(60, 7, 0)],
                    windows=windows)
    assert result["completed_steps"] == 0
    assert result["windows"][1]["states_ns"] is None


def test_child_cpu_during_parent_sleep_is_kept_as_window_association(tmp_path):
    windows = [{"participant": "slow", "index": 0, "virtual_ns": 0, "tid": tid,
                "begin_ns": 10, "end_ns": 60, "outcome": "completed", "kind": kind}
               for tid, kind in ((7, "step"), (8, "worker_window_association"))]
    events = [fork(1), fork(2, 8), switch(10, 0, 7),
              switch(20, 7, 8), wake(50), switch(60, 8, 7)]
    result = report(tmp_path, events, windows=windows)
    assert result["completed_steps"] == 1
    assert result["windows"][0]["states_ns"]["sleeping"] == 30
    assert result["windows"][1]["states_ns"]["running"] == 40


@pytest.mark.parametrize("raw_fields", [False, True])
def test_pinned_perf_text_preserves_direct_wakeup_and_switch_states(tmp_path, raw_fields):
    rows = [
        "[000] 0.000000005: sched:sched_process_fork: comm=parent pid=1 child_comm=child child_pid=7",
        "[000] 0.000000010: sched:sched_wakeup_new: child:7 [120] CPU:000",
        "[000] 0.000000015: sched:sched_switch: swapper/0:0 [120] R ==> child:7 [120]",
        "[000] 0.000000025: sched:sched_switch: child:7 [120] S ==> swapper/0:0 [120]",
        "[000] 0.000000040: sched:sched_wakeup: child:7 [120] CPU:000",
        "[000] 0.000000045: sched:sched_switch: swapper/0:0 [120] R ==> child:7 [120]",
        "[000] 0.000000055: sched:sched_switch: child:7 [120] S ==> swapper/0:0 [120]",
    ]
    if raw_fields:
        rows = [row.replace("child:7 [120] CPU:000", "comm=child pid=7 prio=120 target_cpu=000")
            .replace("swapper/0:0 [120] R ==> child:7 [120]",
                     "prev_comm=swapper/0 prev_pid=0 prev_prio=120 prev_state=R ==> next_comm=child next_pid=7 next_prio=120")
            .replace("child:7 [120] S ==> swapper/0:0 [120]",
                     "prev_comm=child prev_pid=7 prev_prio=120 prev_state=S ==> next_comm=swapper/0 next_pid=0 next_prio=120")
                for row in rows]
    bound = capture_report(tmp_path, ROOT_AND_CHILD.splitlines()[0] + "\n" + "\n".join(rows))
    assert bound["analysis"]["windows"][0]["states_ns"] == {
        "running": 20, "runnable": 10, "sleeping": 15, "unknown": 5}


def test_nonleader_exec_does_not_retain_old_tid_state(tmp_path):
    result = report(tmp_path, [fork(5), switch(10, 0, 7),
        {"time_ns": 30, "cpu": 0, "event": "exec", "pid": 8, "old_pid": 7},
        switch(60, 7, 0)])
    assert "nonleader_exec_unqualified" in result["reasons"]
    assert result["windows"][0]["states_ns"]["unknown"] == 50


def test_unrelated_host_exec_does_not_contaminate_participant_coverage(tmp_path):
    result = report(tmp_path, [fork(5), switch(10, 0, 7),
        {"time_ns": 30, "cpu": 1, "event": "exec", "pid": 90, "old_pid": 91},
        switch(60, 7, 0)])
    assert result["reasons"] == []
    assert result["windows"][0]["states_ns"]["running"] == 50


@pytest.mark.parametrize("archive_name", ["portable-evidence.tar.xz", "review-fixes.tar.xz"])
def test_retained_qualification_replays_offline(tmp_path, archive_name):
    evidence = REPORT.parent / "evidence"
    archive = evidence / archive_name
    checksums = {name: digest for digest, name in (
        line.split() for line in (evidence / "archive.sha256").read_text().splitlines())}
    with archive.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == checksums[archive_name]
    with tarfile.open(archive) as saved:
        members = saved.getmembers()
        assert len(members) < 2000
        assert sum(m.size for m in members) <= 512 << 20
        saved.extractall(tmp_path, filter="data")
    root = tmp_path / "scheduler-evidence"
    validator = root / "proof-sources/validate.py"
    environments = {"native-unprivileged": (1, 1), "container-default": (1, 1),
                    "native-root-existing": (20, 12), "container-perfmon-hostpid": (20, 12)}
    for name, (runs, attempts) in environments.items():
        result = subprocess.run([sys.executable, str(validator), str(root / name)],
                                 capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["validated_runs"] == runs
        assert json.loads(result.stdout)["capture_attempts"] == attempts
