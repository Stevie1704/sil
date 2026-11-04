"""`sil profile --collector scopes`: the capture lifecycle (#372).

docs/proposals/native-source-scopes.md is the contract. The command runs as
its own process with a PATH that holds the built diagnostics runner and
launcher, so these tests see what a user sees: the exit code, stdout,
stderr and the files of the directory.

The participant tests/fixtures/participants/scope_scenarios.c is built with
SIL_SOURCE_SCOPES=1. Each of its scenarios runs one Task "tick" every 1 ms;
the expected counts below come from that source, not from the decoder. A
capture needs Linux x86-64/glibc; the option and preflight checks run on
every host.
"""

from __future__ import annotations

import errno
import json
import os
import platform
import resource
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from conftest import BUILD_DIR, ROOT, file_sha256
from sil import profile_scopes
from sil.profile_scopes import create_raw_file, session_value
from sil.profile_storage import RESERVE_BYTES
from sil.source_scope_decode import CaptureFailed, decode
from sil.source_scope_map import read_library
from sil.source_scope_report import map_document
from test_source_scope_contract import header
from test_source_scope_decoding import library as fake_library
from toys import toy_manifest

MS = 1_000_000
CAPTURE_HOST = (platform.system(), platform.machine(),
                platform.libc_ver()[0]) == ("Linux", "x86_64", "glibc")
needs_capture = pytest.mark.skipif(not CAPTURE_HOST,
                                   reason="needs Linux x86-64/glibc")
SCENARIOS = BUILD_DIR / "scope_scenarios.silp"
EXCEPTION = BUILD_DIR / "scope_exception.silp"
SCOPE_PROCESS = ROOT / "tests" / "participants" / "scope_process.py"
PARTICIPANT = "scoped"
FINALIZED, ACTIVE, CREATED, UNAVAILABLE = 2, 1, 0, 3


@pytest.fixture
def tools(build_dir, tmp_path) -> Path:
    """A PATH directory with the built diagnostics runner and launcher."""
    directory = tmp_path / "tools"
    directory.mkdir()
    for name in ("sil-run-diagnostics", "sil-diagnostics-launcher"):
        (directory / name).symlink_to(build_dir / name)
    return directory


@pytest.fixture
def scenarios(build_dir) -> Path:
    if not SCENARIOS.exists():
        pytest.skip("the instrumented fixtures are built on Linux only")
    return SCENARIOS


def scenario_manifest(tmp_path: Path, scenario: str, *, duration_ms=10,
                      library: Path = SCENARIOS, name="scenario",
                      **config) -> Path:
    m = toy_manifest(duration_ns=duration_ms * MS)
    m.add_channel("ticks", schema="toy.Counter")
    m.add_native(PARTICIPANT, library=str(library),
                 config={"scenario": scenario, **config}, publishes=["ticks"])
    return m.write(tmp_path / f"{name}.json").path


def sil(argv, tools: Path, cwd: Path, *, wait=True, preexec_fn=None):
    environment = {**os.environ,
                   "PATH": os.pathsep.join([str(tools), "/usr/bin:/bin"])}
    command = [sys.executable, "-m", "sil.cli", "profile", *map(str, argv)]
    if not wait:
        return subprocess.Popen(command, cwd=cwd, env=environment,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
    return subprocess.run(command, capture_output=True, text=True, cwd=cwd,
                          env=environment, preexec_fn=preexec_fn)


def capture(manifest: Path, tools: Path, cwd: Path, *options,
            out="scopes", participant=PARTICIPANT, **kwargs):
    return sil([manifest, "--out", out, "--collector", "scopes",
                "--participant", participant, *options], tools, cwd,
               **kwargs)


def saved(directory: Path) -> tuple[dict, dict | None]:
    """scopes.json, and calls.json when it was written."""
    report = json.loads((directory / "scopes.json").read_text())
    calls = directory / "scopes" / "calls.json"
    return report, json.loads(calls.read_text()) if calls.exists() else None


def raw_header(directory: Path) -> dict:
    return header((directory / "scopes" / "events.bin").read_bytes())


def ordinary(sil_run: Path, manifest: Path, tmp_path: Path, name="ordinary"):
    """The exit code and Recording of an ordinary sil-run of `manifest`."""
    recording = tmp_path / f"{name}.mcap"
    done = subprocess.run([str(sil_run), str(manifest), "-o", str(recording)],
                          capture_output=True, cwd=tmp_path)
    return done.returncode, recording.read_bytes() if recording.exists() \
        else None


def counts(calls: dict) -> list:
    return [a["counts"] for a in calls["activations"]]


# --- options and preflight: every host ----------------------------------------


@pytest.mark.parametrize("options, message", [
    ([], "needs --participant"),
    (["--participant", "p", "--frequency-hz", "9"], "--frequency-hz"),
    (["--participant", "p", "--stack-bytes", "9"], "--stack-bytes"),
    (["--participant", "p", "--modules-from", "x"], "--modules-from"),
    (["--participant", "p", "--activations"], "--activations"),
    (["--participant", "p", "--runner"], "--runner"),
    (["--participant", "p", "--max-capture-bytes", "9"],
     "--max-capture-bytes"),
    (["--participant", "p", "--max-scope-records", "0"], "positive"),
    (["--participant", "p", "--max-scope-records", "262145"], "262144"),
], ids=["participant", "frequency", "stack", "modules", "activations",
        "runner", "capture-bytes", "zero-records", "records"])
def test_invalid_options_are_usage_errors(tmp_path, options, message):
    proc = sil(["m.json", "--out", "d", "--collector", "scopes", *options],
               tmp_path, tmp_path)
    assert proc.returncode == 2
    assert message in proc.stderr
    assert not (tmp_path / "d").exists()


@pytest.mark.parametrize("option", [["--original-library", "x"],
                                    ["--max-scope-records", "7"]])
def test_scope_options_with_another_collector_are_usage_errors(tmp_path,
                                                              option):
    proc = sil(["m.json", "--out", "d", "--collector", "cpu", *option],
               tmp_path, tmp_path)
    assert proc.returncode == 2
    assert "scopes collector" in proc.stderr


def fake_manifest(tmp_path: Path, library: Path, *, kind="native") -> Path:
    manifest = tmp_path / "fake.json"
    participant = ({"type": "native", "library": library.name}
                   if kind == "native" else
                   {"type": "process", "command": ["true"]})
    manifest.write_text(json.dumps({"participants": {PARTICIPANT:
                                                     participant}}))
    return manifest


@pytest.mark.parametrize("case", ["missing", "process", "not-instrumented",
                                  "no-needed", "original-unreadable"])
def test_preflight_failures_exit_2_before_the_directory(tmp_path, case):
    library = fake_library(
        tmp_path, section=case != "not-instrumented",
        **({"needed": (b"libc.so.6",)} if case == "no-needed" else {}))
    manifest = fake_manifest(tmp_path, library,
                             kind="process" if case == "process" else
                             "native")
    options = ["--original-library", "missing.so"] \
        if case == "original-unreadable" else []
    proc = capture(manifest, tmp_path, tmp_path, *options,
                   participant="other" if case == "missing" else PARTICIPANT)
    assert proc.returncode == 2, proc.stderr
    assert not (tmp_path / "scopes").exists()


def test_an_existing_output_directory_is_never_replaced(tmp_path):
    (tmp_path / "scopes").mkdir()
    (tmp_path / "scopes" / "kept.txt").write_text("evidence")
    library = fake_library(tmp_path)
    proc = capture(fake_manifest(tmp_path, library), tmp_path, tmp_path)
    assert proc.returncode == 2
    assert "already exists" in proc.stderr
    assert [p.name for p in (tmp_path / "scopes").iterdir()] == ["kept.txt"]


@pytest.mark.skipif(CAPTURE_HOST, reason="checks another host")
def test_another_host_is_a_host_preflight_failure(tmp_path):
    library = fake_library(tmp_path)
    proc = capture(fake_manifest(tmp_path, library), tmp_path, tmp_path)
    assert proc.returncode == 1, proc.stderr
    assert "Linux x86-64/glibc" in proc.stderr
    assert not (tmp_path / "scopes").exists()


# --- the capture files before the Run: every host ----------------------------

CAPACITY, OBSERVATION_LIMIT = 7, 1 << 20
RAW_BYTES = 256 + 16 * CAPACITY  # the contract's raw file size


@pytest.fixture
def allocation(monkeypatch):
    """posix_fallocate, also where the host has none (macOS)."""
    monkeypatch.setattr(os, "posix_fallocate",
                        lambda fd, offset, size: os.ftruncate(fd, size),
                        raising=False)


def create_files(out: Path) -> int:
    return profile_scopes.create_capture_files(
        profile_scopes.ScopesLayout(out), b"{}", uuid.uuid4(), CAPACITY,
        OBSERVATION_LIMIT)


def test_the_space_warning_does_not_count_the_allocated_raw_file_twice(
        tmp_path, monkeypatch, allocation, capsys):
    """The free space is exactly what the capture needs before the raw file
    is allocated: the raw file, the observation limit and the reserve."""
    out = tmp_path / "scopes"
    before = RAW_BYTES + OBSERVATION_LIMIT + RESERVE_BYTES

    def free(directory):  # the disk: the allocated raw file uses space
        events = out / "scopes" / "events.bin"
        return before - (events.stat().st_size if events.exists() else 0)

    monkeypatch.setattr(profile_scopes, "free_bytes", free)
    os.close(create_files(out))
    assert "warning" not in capsys.readouterr().err
    monkeypatch.setattr(profile_scopes, "free_bytes",
                        lambda directory: free(directory) - 1)
    os.close(create_files(tmp_path / "second"))
    assert "warning" in capsys.readouterr().err


def test_a_failed_map_write_leaves_no_directory(tmp_path, monkeypatch,
                                                allocation):
    def full_disk(path, text):
        Path(path).write_text(text[:1])
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(profile_scopes, "write_new_file", full_disk)
    with pytest.raises(profile_scopes.Refusal) as refusal:
        create_files(tmp_path / "scopes")
    assert refusal.value.exit_code == 1
    assert "map.json" in str(refusal.value)
    assert list(tmp_path.iterdir()) == []


# --- the capture: Linux x86-64/glibc -------------------------------------------


@needs_capture
def test_a_complete_capture_writes_the_scopes_directory(
        tools, scenarios, tmp_path, sil_run):
    manifest = scenario_manifest(tmp_path, "plain")
    proc = capture(manifest, tools, tmp_path, "--original-library",
                   SCENARIOS)
    assert proc.returncode == 0, proc.stderr
    out = tmp_path / "scopes"
    report, calls = saved(out)
    assert sorted(p.name for p in out.iterdir()) == [
        "activation", "run.diagnostics.json", "run.mcap",
        "run.mcap.provenance.json", "scopes", "scopes.json"]
    assert sorted(p.name for p in (out / "scopes").iterdir()) == [
        "calls.json", "events.bin", "map.json"]
    assert report["sil_source_scope_report"] == 1
    assert report["run"]["status"] == "succeeded"
    assert (report["capture"]["status"], report["capture"]["reasons"]) == \
        ("complete", [])
    # Ten Activations of 1 ms, one call of scope 1 each: 20 records.
    assert report["capture"]["records"] == 20
    assert counts(calls) == [{"1": 1}] * 10
    raw = raw_header(out)
    assert (raw["state"], raw["records"], raw["capacity"]) == \
        (FINALIZED, 20, 65536)
    assert raw["capture_id"] == calls["capture_id"] == \
        report["capture"]["capture_id"]
    assert (out / "scopes" / "events.bin").stat().st_size == 256 + 16 * 65536
    scan = read_library(SCENARIOS)
    assert raw["build_id"] == scan.identity["build_id"]
    assert report["identities"]["instrumented"]["sha256"] == \
        file_sha256(SCENARIOS)
    assert report["identities"]["original"]["sha256"] == \
        file_sha256(SCENARIOS)
    assert report["configuration"]["participant"] == PARTICIPANT
    for row in report["artifacts"]:
        assert file_sha256(out / row["path"]) == row["sha256"]
    assert report["missing"] == []
    assert "capture: complete" in proc.stdout
    assert "scope 1 plain: 10 calls" in proc.stdout
    # The capture Run records what the ordinary runner records.
    code, recording = ordinary(sil_run, manifest, tmp_path)
    assert code == 0 and recording == (out / "run.mcap").read_bytes()


@needs_capture
def test_recursion_nests_three_calls(tools, scenarios, tmp_path):
    proc = capture(scenario_manifest(tmp_path, "recursion"), tools, tmp_path)
    assert proc.returncode == 0, proc.stderr
    _, calls = saved(tmp_path / "scopes")
    for activation in calls["activations"]:
        assert [(c["scope"], c["depth"], c["parent"])
                for c in activation["calls"]] == \
            [(2, 0, None), (2, 1, 0), (2, 2, 1)]


@needs_capture
def test_an_early_return_is_a_return_event(tools, scenarios, tmp_path):
    proc = capture(scenario_manifest(tmp_path, "early_return"), tools,
                   tmp_path)
    assert proc.returncode == 0, proc.stderr
    _, calls = saved(tmp_path / "scopes")
    assert calls["totals"]["entries"] == calls["totals"]["returns"] == 30
    for activation in calls["activations"]:
        assert [(c["scope"], c["depth"]) for c in activation["calls"]] == \
            [(3, 0), (4, 1), (4, 1)]


@needs_capture
def test_two_tasks_at_one_virtual_time_keep_their_calls(
        tools, scenarios, tmp_path):
    proc = capture(scenario_manifest(tmp_path, "two_tasks"), tools, tmp_path)
    assert proc.returncode == 0, proc.stderr
    _, calls = saved(tmp_path / "scopes")
    tick = [a for a in calls["activations"] if a["task"] == "tick"]
    tock = [a for a in calls["activations"] if a["task"] == "tock"]
    assert [a["virtual_time_ns"] for a in tick] == \
        [a["virtual_time_ns"] for a in tock] == [n * MS for n in range(10)]
    assert counts({"activations": tick}) == [{"5": 1}] * 10
    assert counts({"activations": tock}) == [{"6": 2}] * 10


@needs_capture
def test_longjmp_out_of_two_scopes_is_unbalanced(tools, scenarios, tmp_path):
    proc = capture(scenario_manifest(tmp_path, "longjmp"), tools, tmp_path)
    assert proc.returncode == 1, proc.stderr
    report, calls = saved(tmp_path / "scopes")
    assert (report["capture"]["status"], report["capture"]["reasons"]) == \
        ("partial", ["unbalanced_scopes"])
    assert {a["reason"] for a in calls["activations"]} == {"unbalanced_scopes"}
    assert (calls["totals"]["entries"], calls["totals"]["returns"]) == (20, 0)
    assert calls["unattributed"]["reasons"] == {"missing_return": 20}
    # Per Activation: the outer and the inner scope, without a return.
    assert [r["scope"] for r in calls["unattributed"]["rows"][:2]] == [7, 8]


@needs_capture
def test_an_exception_through_a_scope_has_no_return(build_dir, tools,
                                                    tmp_path):
    if not EXCEPTION.exists():
        pytest.skip("the instrumented fixtures are built on Linux only")
    proc = capture(scenario_manifest(tmp_path, "exception",
                                     library=EXCEPTION), tools, tmp_path)
    assert proc.returncode == 1, proc.stderr
    report, calls = saved(tmp_path / "scopes")
    assert report["run"]["status"] == "succeeded"
    assert report["capture"]["reasons"] == ["unbalanced_scopes"]
    assert (calls["totals"]["entries"], calls["totals"]["returns"]) == (10, 0)
    assert calls["unattributed"]["reasons"] == {"missing_return": 10}
    assert {a["reason"] for a in calls["activations"]} == {"unbalanced_scopes"}


@needs_capture
def test_a_worker_thread_scope_is_unattributed(tools, scenarios, tmp_path):
    proc = capture(scenario_manifest(tmp_path, "worker"), tools, tmp_path)
    assert proc.returncode == 0, proc.stderr
    _, calls = saved(tmp_path / "scopes")
    assert counts(calls) == [{"10": 1}] * 10
    assert calls["unattributed"]["reasons"] == {"worker_thread": 10}
    runner = {r["tid"] for r in calls["unattributed"]["rows"]}
    assert len(runner) == 10  # a new worker thread in each Activation


@needs_capture
@pytest.mark.parametrize("scenario, outcome, status", [
    ("fail", "failed", "run_failure"),
    ("interrupt", "interrupted", "interrupted")])
def test_calls_of_a_failed_or_interrupted_activation_are_withheld(
        tools, scenarios, tmp_path, scenario, outcome, status):
    proc = capture(scenario_manifest(tmp_path, scenario), tools, tmp_path)
    assert proc.returncode == 1, proc.stderr
    report, calls = saved(tmp_path / "scopes")
    assert report["run"]["status"] == status
    assert report["capture"]["status"] == "complete"
    activations = calls["activations"]
    assert [a["outcome"] for a in activations] == \
        ["completed", "completed", outcome]
    assert counts(calls)[:2] == [{"11": 1}] * 2
    assert (activations[2]["status"], activations[2]["reason"],
            activations[2]["withheld_calls"]) == \
        ("unavailable", "not_completed", 1)


@needs_capture
@pytest.mark.parametrize("scenario, options, counter", [
    ("plain", ["--max-scope-records", "7"], "dropped_full"),
    ("contention", ["--max-scope-records", "262144"], "dropped_contended")],
    ids=["seven-records", "contention"])
def test_lost_events_make_the_capture_partial(tools, scenarios, tmp_path,
                                              sil_run, scenario, options,
                                              counter):
    manifest = scenario_manifest(tmp_path, scenario)
    proc = capture(manifest, tools, tmp_path, *options)
    assert proc.returncode == 1, proc.stderr
    out = tmp_path / "scopes"
    report, calls = saved(out)
    assert report["run"]["status"] == "succeeded"
    assert report["capture"]["status"] == "partial"
    assert "events_lost" in report["capture"]["reasons"]
    assert report["capture"]["loss"][counter] > 0
    assert {a["reason"] for a in calls["activations"]} == {"events_lost"}
    if counter == "dropped_full":
        assert report["capture"]["records"] == 7
    code, recording = ordinary(sil_run, manifest, tmp_path)
    assert code == 0 and recording == (out / "run.mcap").read_bytes()


def run_with_session(runner: Path, manifest: Path, tmp_path: Path,
                     events: Path, descriptor_flags: int = os.O_RDWR, *,
                     session_pid: str = "$$", capacity: int = 7,
                     capture_id: uuid.UUID | None = None
                     ) -> subprocess.CompletedProcess:
    """The diagnostics runner with a session of `events`, as the command
    and the launcher make it; `$$` is the runner's PID after exec."""
    capture_id = capture_id or uuid.UUID(
        header(events.read_bytes())["capture_id"])
    descriptor = os.open(events, descriptor_flags)
    try:
        session = session_value(capture_id, capacity, descriptor)
        return subprocess.run(
            ["/bin/sh", "-c",
             f'SIL_SOURCE_SCOPE_SESSION="$0,pid={session_pid}" exec "$@"',
             session, str(runner), str(manifest), "-o",
             str(tmp_path / "run.mcap"), "--diagnostics-out",
             str(tmp_path / "run.json"), "--activation-observations",
             str(tmp_path / "observations.jsonl")],
            capture_output=True, text=True, cwd=tmp_path,
            pass_fds=(descriptor,))
    finally:
        os.close(descriptor)


def new_raw_file(path: Path, capacity=7) -> Path:
    os.close(create_raw_file(path, uuid.uuid4(), capacity))
    return path


@needs_capture
@pytest.mark.parametrize("damage", ["size", "header", "mmap"])
def test_an_observer_that_cannot_activate_is_unavailable(
        scenarios, tmp_path, sil_run_diagnostics, sil_run, damage):
    """The session and the file disagree, so a check of the observer fails;
    the file itself stays a valid raw file for the decoder."""
    manifest = scenario_manifest(tmp_path, "plain")
    events = new_raw_file(tmp_path / "events.bin")
    options = {
        # The session's capacity gives another file size.
        "size": {"capacity": 8},
        # The header's capture ID is not the session's.
        "header": {"capture_id": uuid.uuid4()},
        # A shared writable mapping needs a descriptor open for reading.
        "mmap": {"descriptor_flags": os.O_WRONLY}}[damage]
    done = run_with_session(sil_run_diagnostics, manifest, tmp_path, events,
                            **options)
    code, recording = ordinary(sil_run, manifest, tmp_path, "plain")
    assert (done.returncode, (tmp_path / "run.mcap").read_bytes()) == \
        (code, recording)
    data = events.read_bytes()
    assert header(data)["state"] == UNAVAILABLE
    scope_map = map_document(read_library(SCENARIOS), None)
    with pytest.raises(CaptureFailed) as failed:
        decode(data, (tmp_path / "observations.jsonl").read_text(),
               scope_map, participant=PARTICIPANT,
               capture_id=header(data)["capture_id"],
               library_sha256=file_sha256(SCENARIOS))
    assert failed.value.reason == "observer_unavailable"


@needs_capture
def test_a_process_that_is_not_the_runner_stays_dormant(
        scenarios, tmp_path, sil_run_diagnostics):
    manifest = scenario_manifest(tmp_path, "plain")
    events = new_raw_file(tmp_path / "events.bin")
    before = events.read_bytes()
    done = run_with_session(sil_run_diagnostics, manifest, tmp_path, events,
                            session_pid="1")
    assert done.returncode == 0, done.stderr
    assert events.read_bytes() == before


@needs_capture
def test_output_problems_and_raw_failures(tools, scenarios, tmp_path):
    manifest = scenario_manifest(tmp_path, "plain")

    def small_files():
        # map.json fits; the 1 MiB raw file cannot be allocated (EFBIG).
        resource.setrlimit(resource.RLIMIT_FSIZE, (64 << 10, 64 << 10))

    proc = capture(manifest, tools, tmp_path, preexec_fn=small_files)
    assert proc.returncode == 1, proc.stderr
    assert "the Run did not start" in proc.stderr
    out = tmp_path / "scopes"
    assert sorted(p.name for p in out.iterdir()) == ["scopes"]
    assert not (out / "scopes.json").exists()

    corrupt = scenario_manifest(tmp_path, "corrupt", name="corrupt")
    proc = capture(corrupt, tools, tmp_path, out="corrupt")
    assert proc.returncode == 1, proc.stderr
    report, calls = saved(tmp_path / "corrupt")
    assert report["run"]["status"] == "succeeded"
    assert (report["capture"]["status"], report["capture"]["reasons"]) == \
        ("failed", ["raw_invalid"])
    assert calls is None
    assert {"role": "scope_calls", "path": "scopes/calls.json",
            "reason": "not_written"} in report["missing"]


@needs_capture
def test_a_killed_runner_keeps_its_records(tools, scenarios, tmp_path):
    proc = capture(scenario_manifest(tmp_path, "kill"), tools, tmp_path)
    assert proc.returncode == 128 + signal.SIGKILL, proc.stderr
    out = tmp_path / "scopes"
    report, calls = saved(out)
    assert (report["run"]["status"], report["run"]["signal"]) == \
        ("ended_by_signal", signal.SIGKILL)
    assert (report["capture"]["status"], report["capture"]["reasons"]) == \
        ("failed", ["raw_incomplete"])
    raw = raw_header(out)
    # Three Activations of one call, then one entry before the kill.
    assert (raw["state"], raw["records"]) == (ACTIVE, 7)
    assert report["capture"]["records"] == 7 and calls is None
    assert {"role": "diagnostics_report", "path": "run.diagnostics.json",
            "reason": "not_written"} in report["missing"]
    # A Recording without the MCAP magic is not listed as one.
    recording = out / "run.mcap"
    if recording.read_bytes()[:8] != b"\x89MCAP0\r\n":
        assert {"role": "recording", "path": "run.mcap",
                "reason": "not_written"} in report["missing"]


@needs_capture
def test_an_interrupted_command_keeps_the_run_outcome(tools, scenarios,
                                                     tmp_path):
    manifest = scenario_manifest(tmp_path, "slow", duration_ms=20000)
    process = capture(manifest, tools, tmp_path, wait=False)
    events = tmp_path / "scopes" / "scopes" / "events.bin"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if events.exists() and header(events.read_bytes())["state"] == ACTIVE:
            break
        time.sleep(0.01)
    process.send_signal(signal.SIGINT)
    stdout, stderr = process.communicate(timeout=60)
    assert process.returncode == 1, stderr
    report, calls = saved(tmp_path / "scopes")
    assert report["run"]["status"] == "interrupted"
    assert report["run"]["supervisor_signal"] == signal.SIGINT
    assert report["capture"]["status"] == "interrupted"
    assert report["capture"]["reasons"][0] == "interrupted"
    assert raw_header(tmp_path / "scopes")["state"] == FINALIZED
    assert calls["activations"]


@needs_capture
def test_finalization_is_one_store(build_dir):
    """The observer's finalization calls nothing: no file I/O, no wait."""
    observer = build_dir / "libsil_source_scope.so.1"
    if not observer.exists():
        pytest.skip("the observer is built on Linux only")
    code = subprocess.run(["objdump", "-d", "--no-show-raw-insn",
                           "--disassemble=finalize", str(observer)],
                          capture_output=True, text=True, check=True).stdout
    body = code.split("<finalize>:", 1)[1]
    instructions = [line.split("\t")[-1] for line in body.splitlines()
                    if "\t" in line]
    assert instructions
    assert not [i for i in instructions if i.split()[0] in
                ("call", "jmp", "syscall") and "finalize" not in i]


@needs_capture
def test_the_library_is_finalized_before_the_run_ends(tools, scenarios,
                                                      tmp_path, sil_run):
    manifest = scenario_manifest(tmp_path, "plain")
    proc = capture(manifest, tools, tmp_path)
    assert proc.returncode == 0, proc.stderr
    out = tmp_path / "scopes"
    assert raw_header(out)["state"] == FINALIZED
    lines = [json.loads(line) for line in
             (out / "activation" / "observations.jsonl").read_text()
             .splitlines()]
    phases = {r["phase"] for r in lines if r.get("kind") == "phase"}
    assert {"shutdown", "recording_finalization"} <= phases
    assert lines[-1]["kind"] == "end"
    assert not lines[-1]["write_failed"] and not lines[-1]["truncated"]
    code, recording = ordinary(sil_run, manifest, tmp_path)
    assert code == 0 and recording == (out / "run.mcap").read_bytes()


@needs_capture
@pytest.mark.skipif(not os.environ.get("SIL_SCOPE_SMALL_FS"),
                    reason="needs SIL_SCOPE_SMALL_FS: a small file system")
def test_a_full_file_system_keeps_the_raw_records(tools, scenarios, tmp_path):
    """The participant fills the small file system after the runner wrote
    the first 64 KiB of observations, and frees it in its last Activation.
    The observation stream fails on the full file system; the preallocated
    raw file keeps every record."""
    small = Path(os.environ["SIL_SCOPE_SMALL_FS"])
    work = small / f"capture-{uuid.uuid4().hex}"
    work.mkdir()
    activations = 2000
    manifest = scenario_manifest(
        tmp_path, "fill", duration_ms=activations,
        path=str(work / "filler"), fill_at="600", last=str(activations - 1))
    proc = capture(manifest, tools, work, "-o", tmp_path / "run.mcap")
    assert proc.returncode == 1, proc.stderr
    report, calls = saved(work / "scopes")
    assert report["run"]["status"] == "succeeded"
    assert (report["capture"]["status"], report["capture"]["reasons"]) == \
        ("partial", ["observations_incomplete"])
    assert raw_header(work / "scopes")["records"] == 2 * activations
    assert calls["unattributed"]["reasons"]["observations_incomplete"] > 0
    assert len(calls["activations"]) < activations


@needs_capture
def test_the_library_is_dormant_outside_a_scopes_capture(
        scenarios, tmp_path, tools, sil_run, sil_run_diagnostics):
    """sil-run, the diagnostics runner and a Process participant that loads
    the library: equal Recordings and exit codes, and no observer output.
    The CPU profile is in the source-scope qualification workflow."""
    m = toy_manifest(duration_ns=10 * MS)
    m.add_channel("ticks", schema="toy.Counter")
    m.add_channel("probe", schema="toy.Counter")
    m.add_native(PARTICIPANT, library=str(SCENARIOS),
                 config={"scenario": "plain"}, publishes=["ticks"])
    m.add_process("process", command=[sys.executable, str(SCOPE_PROCESS),
                                      str(SCENARIOS), "probe"],
                  step_period_ns=MS, publishes=["probe"])
    manifest = m.write(tmp_path / "mixed.json").path
    code, recording = ordinary(sil_run, manifest, tmp_path)
    assert code == 0
    diagnostics = subprocess.run(
        [str(sil_run_diagnostics), str(manifest), "-o",
         str(tmp_path / "diagnostics.mcap"), "--diagnostics-out",
         str(tmp_path / "diagnostics.json")], capture_output=True, cwd=tmp_path)
    assert diagnostics.returncode == 0
    assert (tmp_path / "diagnostics.mcap").read_bytes() == recording
    proc = capture(manifest, tools, tmp_path)
    assert proc.returncode == 0, proc.stderr
    out = tmp_path / "scopes"
    assert (out / "run.mcap").read_bytes() == recording
    _, calls = saved(out)
    # Only the runner's scopes: the Process participant ran its scopes
    # (its probe values are in the Recording) in a dormant observer.
    assert counts(calls) == [{"1": 1}] * 10
    assert calls["unattributed"]["rows"] == []
    # `sil run` starts the ordinary runner on PATH.
    (tools / "sil-run").symlink_to(sil_run)
    environment = {**os.environ,
                   "PATH": os.pathsep.join([str(tools), "/usr/bin:/bin"])}
    done = subprocess.run([sys.executable, "-m", "sil.cli", "run",
                           str(manifest), "-o", str(tmp_path / "cli.mcap")],
                          capture_output=True, cwd=tmp_path, env=environment)
    assert done.returncode == 0
    assert (tmp_path / "cli.mcap").read_bytes() == recording


@needs_capture
@pytest.mark.skipif(os.environ.get("SIL_PROFILE_PERF") != "available",
                    reason="needs perf: SIL_PROFILE_PERF=available")
def test_the_library_is_dormant_in_a_cpu_profile(scenarios, tmp_path, tools,
                                                 sil_run):
    manifest = scenario_manifest(tmp_path, "plain")
    code, recording = ordinary(sil_run, manifest, tmp_path)
    environment = {**os.environ,
                   "PATH": os.pathsep.join([str(tools), os.environ["PATH"]])}
    done = subprocess.run([sys.executable, "-m", "sil.cli", "profile",
                           str(manifest), "--out", "cpu", "--collector",
                           "cpu"], capture_output=True, text=True,
                          cwd=tmp_path, env=environment)
    assert done.returncode == code == 0, done.stderr
    assert (tmp_path / "cpu" / "run.mcap").read_bytes() == recording


@needs_capture
@pytest.mark.parametrize("scenario, code", [("plain", 0), ("longjmp", 1),
                                            ("kill", 137)])
def test_sil_profile_render_presents_a_saved_capture(tools, scenarios,
                                                     tmp_path, scenario,
                                                     code):
    """The renderer (#373) verifies and presents what the capture wrote:
    complete, partial, and failed without a diagnostics report."""
    proc = capture(scenario_manifest(tmp_path, scenario), tools, tmp_path)
    assert proc.returncode == code, proc.stderr
    report, _ = saved(tmp_path / "scopes")
    shown = sil(["render", "scopes/scopes.json", "--html", "scopes.html"],
                tools, tmp_path)
    assert shown.returncode == 0, shown.stderr
    assert f"capture: {report['capture']['status']}" in shown.stdout or \
        report["capture"]["status"] in shown.stdout
    assert (tmp_path / "scopes.html").stat().st_size > 0
