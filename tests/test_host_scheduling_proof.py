"""Independent examples at the #362 proof's offline report boundary."""
import json
import hashlib
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest


REPORT = Path(__file__).resolve().parents[1] / "proofs/host-scheduling/report.py"


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
    source = tmp_path / "input.json"
    source.write_text(json.dumps({"clock": "host_monotonic", "namespace_verified": True,
        "loss": False, "truncated": False, "events": [], "windows": [
            {"participant": "slow", "index": 0, "virtual_ns": 0, "tid": 7,
             "begin_ns": 10, "end_ns": 60, "outcome": "completed"}]}))
    perf = tmp_path / "perf.txt"
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
    perf.write_text("\n".join(rows) + "\n")
    output = tmp_path / "report.json"
    result = subprocess.run([sys.executable, str(REPORT), str(source), str(output),
                             "--perf-text", str(perf)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text())["windows"][0]["states_ns"] == {
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


def test_retained_qualification_replays_offline(tmp_path):
    evidence = REPORT.parent / "evidence"
    archive = evidence / "portable-evidence.tar.xz"
    with archive.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == (
            evidence / "archive.sha256").read_text().split()[0]
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
