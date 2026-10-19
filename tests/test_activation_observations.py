"""`sil-run-diagnostics --activation-observations PATH` (#316).

The diagnostics runner can stream one record per Native Task activation,
inclusive Process Step, Replay publication Slot, lifecycle phase and Process
participant spawn and reap to a JSON Lines sidecar. A profiler joins these
host-time intervals with its samples. The ordinary runner has no such option.
The records are observations only: they never change the Run's outcome, its
Recording or its stdout, also when the storage limit is reached.
"""

from __future__ import annotations

import json
import subprocess
import sys

from conftest import ROOT, run_manifest
from sil.manifest import SubscriberRoute
from toys import add_producer, toy_manifest

ECHO = ROOT / "tests" / "participants" / "echo.py"
FAIL_AT_20MS = ROOT / "tests" / "participants" / "fail_at_20ms.py"


def pipeline(tmp_path, *, duration_ns=50_000_000):
    """Two Native Tasks with the same Virtual times and one Process Step."""
    m = toy_manifest(duration_ns=duration_ns)
    m.add_channel("ticks", schema="toy.Counter")
    m.add_channel("other", schema="toy.Counter")
    m.add_channel("echo", schema="toy.Counter")
    add_producer(m, "first", channel="ticks", period_ns=10_000_000)
    add_producer(m, "second", channel="other", period_ns=10_000_000)
    m.add_process("pyecho", command=[sys.executable, str(ECHO)],
                  step_period_ns=10_000_000,
                  subscribes=[SubscriberRoute("ticks", capacity=64)],
                  publishes=["echo"])
    return m.write(tmp_path / "pipeline.json").path


def observe(runner, manifest, cwd, observations, *extra):
    return subprocess.run(
        [str(runner), str(manifest), "--no-recording",
         "--activation-observations", str(observations), *extra],
        cwd=cwd, capture_output=True, text=True)


def records(path):
    lines = path.read_text().splitlines()
    return json.loads(lines[0]), [json.loads(line) for line in lines[1:]]


def of_kind(rows, kind):
    return [row for row in rows if row["kind"] == kind]


def test_every_activation_is_one_closed_host_interval(
        sil_run_diagnostics, tmp_path):
    observations = tmp_path / "activations.jsonl"

    proc = observe(sil_run_diagnostics, pipeline(tmp_path), tmp_path,
                   observations)

    assert proc.returncode == 0, proc.stderr
    header, rows = records(observations)
    assert header["sil_activation_observations"] == 1
    assert header["clock"] == "host_monotonic"
    assert type(header["runner_pid"]) is int
    activations = of_kind(rows, "activation")
    by_task = {}
    for row in activations:
        by_task.setdefault((row["participant"], row["task"]), []).append(row)
    # 50 ms in 10 ms periods: activations at 0, 10, 20, 30 and 40 ms.
    times = [0, 10_000_000, 20_000_000, 30_000_000, 40_000_000]
    assert sorted(by_task) == [("first", "tick"), ("pyecho", "step"),
                               ("second", "tick")]
    for (participant, _), rows_of_task in by_task.items():
        assert [r["index"] for r in rows_of_task] == [0, 1, 2, 3, 4]
        assert [r["virtual_time_ns"] for r in rows_of_task] == times
        assert {r["outcome"] for r in rows_of_task} == {"completed"}
        assert {r["task_kind"] for r in rows_of_task} == {
            "process_step" if participant == "pyecho" else "native_task"}
    # Host times are decimal strings; each interval is closed and they follow
    # one another on the runner thread in Slot order.
    previous_end = 0
    for row in activations:
        begin, end = int(row["begin_ns"]), int(row["end_ns"])
        assert previous_end <= begin <= end
        previous_end = end
        assert row["tid"] == header["runner_tid"]
    end = rows[-1]
    assert end == {"kind": "end", "records": len(rows) - 1, "dropped": 0,
                   "truncated": False, "write_failed": False,
                   "bytes": end["bytes"]}


def test_lifecycle_and_process_lifetime_are_separate_records(
        sil_run_diagnostics, tmp_path):
    observations = tmp_path / "activations.jsonl"

    proc = observe(sil_run_diagnostics, pipeline(tmp_path), tmp_path,
                   observations)

    assert proc.returncode == 0, proc.stderr
    _, rows = records(observations)
    phases = [row["phase"] for row in of_kind(rows, "phase")]
    assert phases == ["preflight", "setup", "scheduled_execution",
                      "shutdown"]
    spawn, = of_kind(rows, "spawn")
    reap, = of_kind(rows, "reap")
    assert spawn["participant"] == reap["participant"] == "pyecho"
    assert spawn["pid"] == reap["pid"] > 0
    steps = [row for row in of_kind(rows, "activation")
             if row["participant"] == "pyecho"]
    assert int(spawn["at_ns"]) <= int(steps[0]["begin_ns"])
    assert int(steps[-1]["end_ns"]) <= int(reap["at_ns"])


def test_a_failed_step_is_recorded_as_failed_and_the_log_is_closed(
        sil_run_diagnostics, tmp_path):
    m = toy_manifest(duration_ns=50_000_000)
    m.add_process("test", command=[sys.executable, str(FAIL_AT_20MS)],
                  step_period_ns=10_000_000)
    manifest = m.write(tmp_path / "failing.json").path
    observations = tmp_path / "activations.jsonl"

    proc = observe(sil_run_diagnostics, manifest, tmp_path, observations)

    assert proc.returncode == 1
    _, rows = records(observations)
    outcomes = [(r["index"], r["outcome"])
                for r in of_kind(rows, "activation")]
    assert outcomes == [(0, "completed"), (1, "completed"), (2, "failed")]
    assert rows[-1]["kind"] == "end"


def test_the_storage_limit_drops_records_and_keeps_the_run(
        sil_run, sil_run_diagnostics, tmp_path):
    manifest = pipeline(tmp_path, duration_ns=2_000_000_000)
    observations = tmp_path / "activations.jsonl"
    limit = 4096

    proc = subprocess.run(
        [str(sil_run_diagnostics), str(manifest), "-o",
         str(tmp_path / "observed.mcap"), "--activation-observations",
         str(observations), "--activation-observations-limit-bytes",
         str(limit)], cwd=tmp_path, capture_output=True, text=True)
    ordinary = run_manifest(sil_run, manifest, tmp_path / "ordinary.mcap")

    assert proc.returncode == ordinary.returncode == 0, proc.stderr
    assert (tmp_path / "observed.mcap").read_bytes() == (
        tmp_path / "ordinary.mcap").read_bytes()
    assert proc.stdout == ordinary.stdout
    _, rows = records(observations)
    end = rows[-1]
    assert end["kind"] == "end"
    assert end["truncated"] is True and end["write_failed"] is False
    # 200 Slots of three activations, phases and process records: most of
    # them did not fit and are counted, not silently lost.
    assert end["records"] == len(rows) - 1
    assert end["records"] + end["dropped"] >= 600
    assert end["bytes"] <= limit
    body = observations.read_text().splitlines()
    assert sum(len(line) + 1 for line in body[:-1]) == end["bytes"]


def test_replay_publication_slots_are_replay_records(
        sil_run_diagnostics, tmp_path):
    from test_determinism import replay_into_process_manifest
    manifest = replay_into_process_manifest(tmp_path).path
    observations = tmp_path / "activations.jsonl"

    proc = observe(sil_run_diagnostics, manifest, tmp_path, observations)

    assert proc.returncode == 0, proc.stderr
    _, rows = records(observations)
    replays = of_kind(rows, "replay")
    assert replays and "task" not in replays[0]
    assert [r["index"] for r in replays] == list(range(len(replays)))
    assert {r["outcome"] for r in replays} == {"completed"}


def test_an_existing_observations_path_is_a_usage_error(
        sil_run_diagnostics, tmp_path):
    observations = tmp_path / "activations.jsonl"
    observations.write_text("earlier evidence\n")

    proc = observe(sil_run_diagnostics, pipeline(tmp_path), tmp_path,
                   observations)

    assert proc.returncode == 2
    assert "already exists" in proc.stderr
    assert observations.read_text() == "earlier evidence\n"


def test_the_ordinary_runner_has_no_observation_option(sil_run, tmp_path):
    observations = tmp_path / "activations.jsonl"

    proc = observe(sil_run, pipeline(tmp_path), tmp_path, observations)

    assert proc.returncode == 2
    assert not observations.exists()
