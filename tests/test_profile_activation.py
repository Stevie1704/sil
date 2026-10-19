"""The Activation join of `sil profile --activations` (#316), on synthetic
records.

`correlate` reads the runner's observation stream, the CPU capture members
and the normalized samples, all as saved evidence. The records here are
written by hand, independently of the runner and of perf, so each expected
class comes from the rules of docs/profile-report.md, not from the code:

- a runner-thread sample inside a Native activation or a Process Step is in
  that Activation's execution context;
- a sample of a Process participant's process tree inside one of its Steps
  is associated with that Step window only;
- every other sample is lifecycle, Replay or unattributed, with a reason.

Intervals are half open: [begin, end).
"""

from __future__ import annotations

import json

import pytest

from sil.profile_activation import correlate

ORIGIN = 1_000_000_000_000
NS = {"device": 4, "inode": 4026531836}


def at(offset: int) -> str:
    return str(ORIGIN + offset)


def header(**overrides) -> dict:
    return {"sil_activation_observations": 1, "clock": "host_monotonic",
            "runner_pid": 100, "runner_tid": 100, "limit_bytes": 1 << 20,
            "pid_namespace": NS, **overrides}


def activation(participant, task, kind, index, virtual_ns, begin, end,
               outcome="completed") -> dict:
    return {"kind": "activation", "participant": participant, "task": task,
            "task_kind": kind, "index": index, "virtual_time_ns": virtual_ns,
            "begin_ns": at(begin), "end_ns": at(end), "outcome": outcome,
            "tid": 100}


def phase(name, begin, end) -> dict:
    return {"kind": "phase", "phase": name, "begin_ns": at(begin),
            "end_ns": at(end)}


def spawn(participant, pid, offset) -> dict:
    return {"kind": "spawn", "participant": participant, "pid": pid,
            "at_ns": at(offset)}


def reap(participant, pid, offset) -> dict:
    return {"kind": "reap", "participant": participant, "pid": pid,
            "reaped": True, "at_ns": at(offset)}


def end(records: int, **overrides) -> dict:
    return {"kind": "end", "records": records, "dropped": 0,
            "truncated": False, "write_failed": False, "bytes": 1000,
            **overrides}


def stream(rows: list[dict], *, terminated: bool = True,
           head: dict | None = None, **end_overrides) -> str:
    lines = [head or header(), *rows]
    if terminated:
        lines.append(end(len(rows), **end_overrides))
    return "".join(json.dumps(line) + "\n" for line in lines)


RECORDS = [
    phase("preflight", 400, 500),
    spawn("proc_a", 200, 900),
    spawn("proc_b", 201, 1300),
    phase("setup", 500, 1900),
    activation("nat", "tick", "native_task", 0, 0, 2000, 2100),
    # The same Virtual time, another Task: told apart by host time only.
    activation("nat2", "tick", "native_task", 0, 0, 2100, 2200),
    activation("proc_a", "step", "process_step", 0, 0, 2200, 2400),
    activation("nat", "tick", "native_task", 1, 10_000_000, 3000, 3100),
    activation("nat", "tick", "native_task", 2, 20_000_000, 3500, 3600,
               "failed"),
    phase("scheduled_execution", 1900, 9500),
    reap("proc_a", 200, 9600),
    reap("proc_b", 201, 9700),
    phase("shutdown", 9500, 9800),
]


def process(pid_id, pid, parent, role, start, end_offset, *, forked=True):
    return {"id": pid_id, "pid": pid, "parent": parent, "role": role,
            "commands": ["x"], "execs": 1, "start_offset_ns": start,
            "end_offset_ns": end_offset, "forked": forked, "exited": True,
            "samples": 0, "weight_ns": 0, "threads": 1}


def capture(samples: list[tuple], processes=None, threads=None) -> tuple:
    """The capture members and the samples document, with sums that match."""
    processes = processes or [
        process("p1", 99, None, "launcher", 0, 9990),
        process("p2", 100, "p1", "runner", 50, 9950),
        process("p3", 200, "p2", "descendant", 1000, 9600),
        # A short-lived helper of proc_a: PID 201, then reused for proc_b.
        process("p4", 201, "p3", "descendant", 1100, 1200),
        process("p5", 201, "p2", "descendant", 1350, 9700),
        # A process the runner forked for no spawn record: a Native
        # participant's own child.
        process("p6", 300, "p2", "descendant", 2000, 2600),
    ]
    threads = threads or [
        {"id": "t1", "process": "p1", "tid": 99},
        {"id": "t2", "process": "p2", "tid": 100},
        {"id": "t3", "process": "p2", "tid": 101},
        {"id": "t4", "process": "p3", "tid": 200},
        {"id": "t5", "process": "p4", "tid": 201},
        {"id": "t6", "process": "p5", "tid": 201},
        {"id": "t7", "process": "p6", "tid": 300},
    ]
    rows = [[offset, p, t, weight, 0] for offset, p, t, weight in samples]
    members = {
        "sampling": {"clock": "host_monotonic"},
        "time_origin_ns": str(ORIGIN),
        "samples_file": "cpu/samples.json",
        "totals": {"samples": len(rows),
                   "weight_ns": sum(row[3] for row in rows)},
        "processes": processes,
        "threads": threads,
    }
    document = {"sil_profile_cpu_samples": 1, "time_origin_ns": str(ORIGIN),
                "frames": [], "stacks": [[]], "samples": rows}
    return members, document


SAMPLES = [
    (2050, "p2", "t2", 1),   # nat/tick#0
    (2099, "p2", "t2", 1),   # nat/tick#0: the last ns inside
    (2100, "p2", "t2", 1),   # nat2/tick#0: begin is inside, end is not
    (2250, "p2", "t2", 1),   # proc_a/step#0, the runner's side of the Step
    (2300, "p3", "t4", 7),   # proc_a's process inside its Step window
    (2400, "p3", "t4", 1),   # the Step's end: outside the window
    (2500, "p3", "t4", 1),   # proc_a between Steps
    (1150, "p4", "t5", 1),   # proc_a's helper, before any Step
    (2300, "p2", "t3", 1),   # a runner thread that runs no Activation
    (1500, "p2", "t2", 1),   # setup
    (2900, "p2", "t2", 1),   # scheduled execution between Activations
    (3550, "p2", "t2", 1),   # the failed activation nat/tick#2
    (2300, "p6", "t7", 1),   # a runner descendant without a spawn record
    (100, "p1", "t1", 1),    # the launcher
    (60, "p2", "t2", 1),     # the runner before its first phase
]


def run(observations=None, samples=SAMPLES, **kwargs):
    members, document = capture(samples)
    if observations is None:
        observations = stream(RECORDS)
    return correlate(observations, members, document,
                     pid_namespace=kwargs.pop("pid_namespace", NS), **kwargs)


def bucket(summary, name):
    entry = summary["samples"][name]
    return entry["samples"], entry["weight_ns"]


def reasons(summary) -> dict[str, int]:
    return {row["reason"]: row["samples"]
            for row in summary["samples"]["unattributed"]["reasons"]}


def test_a_complete_stream_gives_a_complete_correlation():
    result = run()

    assert result.record["kind"] == "activation"
    assert result.record["status"] == "complete"
    assert result.record["reasons"] == []
    assert result.record["clock"] == "host_monotonic"


def test_runner_thread_samples_in_an_activation_carry_its_context():
    summary = run().record["summary"]

    tasks = {(t["participant"], t["task"]): t for t in summary["tasks"]}
    assert tasks["nat", "tick"]["execution_context"]["samples"] == 3
    assert tasks["nat2", "tick"]["execution_context"]["samples"] == 1
    assert tasks["proc_a", "step"]["execution_context"]["samples"] == 1
    assert bucket(summary, "execution_context") == (5, 5)


def test_child_samples_in_a_step_are_window_associated_only():
    summary = run().record["summary"]

    tasks = {(t["participant"], t["task"]): t for t in summary["tasks"]}
    assert tasks["proc_a", "step"]["window_associated"] == {
        "samples": 1, "weight_ns": 7}
    assert tasks["nat", "tick"]["window_associated"] == {
        "samples": 0, "weight_ns": 0}
    assert bucket(summary, "window_associated") == (1, 7)


def test_other_work_is_lifecycle_or_unattributed_with_a_reason():
    summary = run().record["summary"]

    assert {row["phase"]: row["samples"] for row in summary["lifecycle"]} \
        == {"preflight": 0, "setup": 1, "scheduled_execution": 1,
            "shutdown": 0}
    assert bucket(summary, "lifecycle") == (2, 2)
    assert reasons(summary) == {
        "process_outside_step": 3, "runner_other_thread": 1,
        "unmapped_process": 1, "launcher": 1,
        "runner_outside_intervals": 1}
    # Every sample is in exactly one class.
    total = summary["samples"]["total"]
    assert total == {"samples": len(SAMPLES),
                     "weight_ns": sum(s[3] for s in SAMPLES)}
    assert sum(summary["samples"][name]["samples"] for name in (
        "execution_context", "window_associated", "lifecycle", "replay",
        "unattributed")) == total["samples"]


def test_activations_without_samples_stay_visible():
    result = run()

    nat = next(t for t in result.record["summary"]["tasks"]
               if t["participant"] == "nat")
    assert nat["activations"] == 3
    assert nat["failed"] == 1
    assert nat["with_samples"] == 2
    assert nat["without_samples"] == 1
    rows = result.document["activations"]
    assert [(r["index"], r["samples"], r["outcome"]) for r in rows
            if r["participant"] == "nat"] == [
        (0, 2, "completed"), (1, 0, "completed"), (2, 1, "failed")]
    assert rows[0]["begin_ns"] == at(2000) and rows[0]["end_ns"] == at(2100)


def test_each_sample_names_its_class_in_capture_order():
    contexts = run().document["samples"]

    assert contexts[0] == ["execution_context", 0]
    assert contexts[2] == ["execution_context", 1]
    assert contexts[4] == ["window_associated", 2]
    assert contexts[9] == ["lifecycle", "setup"]
    assert contexts[13] == ["unattributed", "launcher"]
    assert len(contexts) == len(SAMPLES)


def test_a_reused_pid_maps_only_the_process_the_runner_forked():
    processes = run().record["summary"]["processes"]

    assert processes == [
        {"participant": "proc_a", "pid": 200, "process": "p3",
         "mapping": "verified"},
        {"participant": "proc_b", "pid": 201, "process": "p5",
         "mapping": "verified"},
    ]


def test_a_spawn_that_perf_did_not_see_is_unmapped_and_partial():
    rows = [r for r in RECORDS if r.get("participant") != "proc_b"]
    rows.append(spawn("proc_b", 999, 1300))

    result = run(stream(rows))

    assert result.record["status"] == "partial"
    assert result.record["reasons"] == ["process_unmapped"]
    mapping = {p["participant"]: p for p in result.record["summary"][
        "processes"]}
    assert mapping["proc_b"]["process"] is None
    assert mapping["proc_b"]["mapping"] == "not_observed"


def test_shared_code_gets_no_identity_from_its_symbol():
    # Two Native participants run the same function: only the interval of
    # the runner thread decides, never the frame. Same stack index 0 here.
    summary = run(samples=[(2050, "p2", "t2", 1),
                           (2150, "p2", "t2", 1)]).record["summary"]

    tasks = {(t["participant"], t["task"]): t for t in summary["tasks"]}
    assert tasks["nat", "tick"]["execution_context"]["samples"] == 1
    assert tasks["nat2", "tick"]["execution_context"]["samples"] == 1


@pytest.mark.parametrize("kwargs, reason", [
    ({"head": header(clock="realtime")}, "clock_mismatch"),
    # The intervals lie after the runner's exit: not the perf clock.
    ({"shift": 1_000_000}, "clock_mismatch"),
    ({"head": header(runner_pid=4242)}, "runner_not_observed"),
    ({"head": header(pid_namespace={"device": 4, "inode": 1})},
     "pid_namespace_mismatch"),
    ({"head": header(pid_namespace=None)}, "pid_namespace_unverified"),
])
def test_an_unproven_mapping_gives_no_correlation(kwargs, reason):
    shift = kwargs.pop("shift", 0)
    rows = RECORDS
    if shift:
        rows = [{key: (str(int(value) + shift) if key.endswith("_ns")
                       and key != "virtual_time_ns" else value)
                 for key, value in row.items()} for row in RECORDS]

    result = run(stream(rows, **kwargs))

    assert result.record["status"] == "unavailable"
    assert result.record["reasons"] == [reason]
    assert result.record["summary"] is None
    assert result.document is None


def test_a_truncated_stream_is_partial_and_its_gap_is_unattributed():
    # The limit kept the records up to nat2/tick#0; the rest were dropped.
    kept = RECORDS[:6]

    result = run(stream(kept, dropped=7, truncated=True))

    assert result.record["status"] == "partial"
    assert result.record["reasons"] == ["observations_truncated"]
    summary = result.record["summary"]
    assert summary["stream"]["dropped"] == 7
    assert summary["stream"]["truncated"] is True
    # After the last kept record (2200), no runner-thread or Run-descendant
    # sample is attributed, also not to the proc_a Step that was dropped.
    # The other runner thread and the launcher do not depend on the stream.
    assert reasons(summary)["observations_incomplete"] == 7
    assert reasons(summary)["runner_other_thread"] == 1
    assert bucket(summary, "window_associated") == (0, 0)


def test_a_stream_without_its_end_line_is_partial():
    result = run(stream(RECORDS, terminated=False))

    assert result.record["status"] == "partial"
    assert result.record["reasons"] == ["observations_unterminated"]
    assert result.record["summary"]["stream"]["terminated"] is False


def test_a_write_failure_is_partial_and_a_cut_line_is_tolerated():
    text = stream(RECORDS[:6], terminated=False) + '{"kind": "activ\n' \
        + json.dumps(end(6, write_failed=True, dropped=7)) + "\n"

    result = run(text)

    assert result.record["status"] == "partial"
    assert result.record["reasons"] == ["observations_write_failed"]
    assert result.record["summary"]["stream"]["undecodable_lines"] == 1


def test_an_undecodable_line_in_a_clean_stream_fails_the_correlation():
    lines = stream(RECORDS).splitlines(keepends=True)
    lines.insert(3, "not json\n")

    result = run("".join(lines))

    assert result.record["status"] == "failed"
    assert result.record["reasons"] == ["observations_corrupt"]
    assert result.record["summary"] is None


def test_missing_observations_are_unavailable():
    members, document = capture(SAMPLES)

    result = correlate(None, members, document, pid_namespace=NS)

    assert result.record["status"] == "unavailable"
    assert result.record["reasons"] == ["observations_missing"]
    assert result.record["observations"] is None


def test_zero_samples_still_list_every_activation():
    result = run(samples=[])

    summary = result.record["summary"]
    assert result.record["status"] == "complete"
    assert summary["samples"]["total"] == {"samples": 0, "weight_ns": 0}
    assert all(t["with_samples"] == 0 for t in summary["tasks"])
    assert len(result.document["activations"]) == 5


def test_replay_slots_are_their_own_class():
    rows = [*RECORDS, {"kind": "replay", "participant": "rec", "index": 0,
                       "virtual_time_ns": 0, "begin_ns": at(1950),
                       "end_ns": at(1990), "outcome": "completed",
                       "tid": 100}]

    summary = run(stream(rows), samples=[(1960, "p2", "t2", 1)]).record[
        "summary"]

    assert bucket(summary, "replay") == (1, 1)
    assert summary["intervals"]["replay"] == 1
