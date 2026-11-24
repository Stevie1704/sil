"""Scheduler trace cases, the independent oracle, and the Recording reader (#404).

A case is a successful periodic Run: Native participants with Tasks and Process
participants with one Step each, and no Replay participants. The oracle states
the scheduling contract directly over Python integers:

- An entry is due at every `offset + k * period` (k >= 0) in [0, duration).
- The trace is ordered by Virtual time, then priority, then registration index.
- Registration indices follow name-sorted Participant setup and, inside one
  Native participant, the declared Task order. A Process Step has offset 0.

The oracle does not share code with the runner's slot selection, its overflow
guard, or its Recording output (see `docs/scheduler-verification.md`).
"""

from __future__ import annotations

import json
import random
import struct
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from conftest import BUILD_DIR, ROOT, run_manifest
from mcap.reader import make_reader
from toys import toy_manifest

U64_MAX = 2**64 - 1
I32_MIN = -(2**31)
I32_MAX = 2**31 - 1

# The bound is on Activations, not on timestamps: a case can use times near
# UINT64_MAX and still produce a short trace.
MAX_ACTIVATIONS = 400
# A broken scheduler that loops must fail with a message, not hang the suite.
RUN_TIMEOUT_S = 60

CORPUS_SEED = 404
CORPUS_SIZE = 48

TRACE_STEP = ROOT / "tests" / "participants" / "trace_step.py"


@dataclass(frozen=True)
class Task:
    name: str
    period_ns: int
    offset_ns: int
    priority: int


@dataclass(frozen=True)
class NativeParticipant:
    name: str
    tasks: tuple[Task, ...]


@dataclass(frozen=True)
class ProcessParticipant:
    name: str
    step_period_ns: int
    priority: int


@dataclass(frozen=True)
class Case:
    case_id: str
    duration_ns: int
    # Authoring order: the order the Manifest declares them in.
    participants: tuple[NativeParticipant | ProcessParticipant, ...]

    def describe(self) -> str:
        return json.dumps(asdict(self), indent=2)


@dataclass(frozen=True)
class Entry:
    """One periodic schedule entry, with its registration index."""

    participant: str
    task: str
    period_ns: int
    offset_ns: int
    priority: int
    index: int


def registration_entries(case: Case) -> list[Entry]:
    """The entries in registration order, by the documented rules."""
    entries: list[Entry] = []
    for p in sorted(case.participants, key=lambda p: p.name.encode()):
        if isinstance(p, NativeParticipant):
            for t in p.tasks:
                entries.append(Entry(p.name, t.name, t.period_ns, t.offset_ns,
                                     t.priority, len(entries)))
        else:
            entries.append(Entry(p.name, "step", p.step_period_ns, 0,
                                 p.priority, len(entries)))
    return entries


def activation_count(entry: Entry, duration_ns: int) -> int:
    if entry.offset_ns >= duration_ns:
        return 0
    return (duration_ns - 1 - entry.offset_ns) // entry.period_ns + 1


def expected_trace(case: Case) -> list[tuple[int, str, str]]:
    """The complete ordered Activation trace that the contract requires."""
    entries = registration_entries(case)
    total = sum(activation_count(e, case.duration_ns) for e in entries)
    assert total <= MAX_ACTIVATIONS, (
        f"case {case.case_id} has {total} Activations, above the bound "
        f"{MAX_ACTIVATIONS}")
    keyed = []
    for e in entries:
        for k in range(activation_count(e, case.duration_ns)):
            keyed.append((e.offset_ns + k * e.period_ns, e.priority, e.index,
                          e.participant, e.task))
    keyed.sort()
    return [(t, participant, task) for t, _, _, participant, task in keyed]


def channel_of(participant: str) -> str:
    return f"trace.{participant}"


def write_manifest(case: Case, path: Path) -> Path:
    """Write the case, with its Participants in authoring order.

    `Manifest.write` sorts the keys, so `sil-run` would never see the
    authoring order whose independence the trace must show.
    """
    m = toy_manifest(duration_ns=case.duration_ns)
    for p in case.participants:
        m.add_channel(channel_of(p.name), schema="toy.Counter")
    for p in case.participants:
        if isinstance(p, NativeParticipant):
            m.add_native(
                p.name,
                library=str(BUILD_DIR / "trace_tasks.silp"),
                config={
                    "channel": channel_of(p.name),
                    "duration_ns": case.duration_ns,
                    "tasks": [asdict(t) for t in p.tasks],
                },
                subscribes=[],
                publishes=[channel_of(p.name)],
            )
        else:
            m.add_process(
                p.name,
                command=[sys.executable, str(TRACE_STEP), channel_of(p.name),
                         str(case.duration_ns)],
                step_period_ns=p.step_period_ns,
                priority=p.priority,
                publishes=[channel_of(p.name)],
            )
    doc = m.to_doc()
    doc["participants"] = {p.name: doc["participants"][p.name]
                           for p in case.participants}
    path.write_text(json.dumps(doc, allow_nan=False))
    return path


def run_case(sil_run: Path, case: Case, workdir: Path):
    """Run the case through the ordinary `sil-run` path, with a time bound."""
    manifest = write_manifest(case, workdir / f"{case.case_id}.json")
    out = workdir / f"{case.case_id}.mcap"
    try:
        proc = run_manifest(sil_run, manifest, out, timeout=RUN_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        raise AssertionError(
            f"case {case.case_id}: sil-run did not finish in {RUN_TIMEOUT_S} s"
            f"\n{failure_context(case)}") from e
    assert proc.returncode == 0, (
        f"case {case.case_id}: sil-run exit {proc.returncode}\n{proc.stderr}"
        f"\n{failure_context(case)}")
    return out


def observed_trace(case: Case, mcap_path: Path) -> list[tuple[int, str, str]]:
    """The markers in stored Publish order, without sorting or deduplication."""
    by_channel = {channel_of(p.name): p for p in case.participants}
    trace = []
    with open(mcap_path, "rb") as f:
        for _, channel, message in make_reader(f).iter_messages(
                log_time_order=False):
            p = by_channel[channel.topic]
            activation_ns, task_index = struct.unpack("<Qq", message.data)
            assert activation_ns == message.log_time, (
                f"case {case.case_id}: marker for {activation_ns} ns was "
                f"published at {message.log_time} ns")
            task = (p.tasks[task_index].name
                    if isinstance(p, NativeParticipant) else "step")
            trace.append((message.log_time, p.name, task))
    return trace


def failure_context(case: Case) -> str:
    """The rerun command and the input schedule, for a failure message."""
    return ("rerun: .venv/bin/python -m pytest tests/test_scheduler_trace.py "
            f"-k '{case.case_id}'"
            f"\ninput schedule:\n{case.describe()}")


def first_divergence(expected: list, observed: list) -> str | None:
    """Describe the first entry where two traces differ, or None."""
    for i, (want, got) in enumerate(zip(expected, observed)):
        if want != got:
            return f"entry {i}: expected {want}, observed {got}"
    if len(observed) < len(expected):
        return (f"entry {len(observed)}: observed trace ends early, expected "
                f"{expected[len(observed)]}")
    if len(observed) > len(expected):
        return (f"entry {len(expected)}: observed trace has an extra entry "
                f"{observed[len(expected)]}")
    return None


def assert_same_trace(case: Case, expected: list, observed: list) -> None:
    divergence = first_divergence(expected, observed)
    assert divergence is None, (
        f"case {case.case_id}: sil-run trace differs from the contract at "
        f"{divergence}\n{failure_context(case)}")


# --- fixed edge cases and the generated corpus ---

def native(name: str, *tasks: tuple) -> NativeParticipant:
    return NativeParticipant(name, tuple(Task(*t) for t in tasks))


FIXED_CASES = (
    # Equal priorities across Participants: authored out of name order.
    Case("tie-across-participants", 30, (
        native("bravo", ("tick", 10, 0, 0)),
        native("alpha", ("tick", 10, 0, 0)),
        native("charlie", ("tick", 5, 0, 0)),
    )),
    # Equal priorities within one Native participant: the declared order is
    # not the name order.
    Case("tie-within-participant", 40, (
        native("solo", ("zulu", 10, 0, 0), ("alpha", 20, 0, 0),
               ("mike", 10, 0, 0)),
    )),
    Case("signed-priorities", 20, (
        native("bravo", ("low", 10, 0, -5), ("high", 10, 0, 7)),
        native("alpha", ("mid", 5, 0, 0), ("min", 10, 0, I32_MIN),
               ("max", 10, 0, I32_MAX)),
    )),
    Case("offsets-at-and-past-duration", 30, (
        native("alpha", ("at", 10, 30, 0), ("past", 10, 31, 0),
               ("last", 10, 29, 0)),
    )),
    # The next Activation after 15 lands exactly on the duration.
    Case("next-activation-at-duration", 30, (
        native("alpha", ("tick", 15, 0, 0)),
        native("bravo", ("tick", 10, 0, 1)),
    )),
    Case("activation-at-u64-max-minus-one", U64_MAX, (
        native("alpha", ("edge", U64_MAX - 1, U64_MAX - 1, 0),
               ("first", U64_MAX, 0, 0)),
    )),
    # 'aprod' overflows after 2^63; the others keep running past it.
    Case("overflow-while-others-continue", U64_MAX, (
        native("aprod", ("tick", 2**63, 0, 0)),
        native("acc", ("tick", 2**62, 0, 0), ("odd", 2**62, 2**62 - 1, 0)),
    )),
    Case("mixed-native-process", 50, (
        ProcessParticipant("plant", 10, 0),
        native("ecu", ("fast", 5, 0, 0), ("slow", 10, 0, -1)),
        ProcessParticipant("bus", 25, 0),
    )),
)


_NAMES = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")
_TASK_NAMES = ("tick", "zulu", "able", "mike", "baker")
_PRIORITIES = (-2, -1, 0, 0, 0, 1, 2, I32_MIN, I32_MAX)


def generated_case(index: int) -> Case:
    """One reproducible case. Each case has its own seed, so it reruns alone."""
    rng = random.Random(f"{CORPUS_SEED}:{index}")
    if rng.random() < 0.5:
        duration = rng.randint(20, 2_000)
    else:
        duration = rng.choice([
            U64_MAX,
            U64_MAX - rng.randint(0, 2**40),
            2**63 + rng.randint(0, 2**62),
        ])
    # Every period is at least `base`, so an entry has at most about 17
    # Activations whatever the magnitude of the times.
    base = max(1, duration // rng.randint(4, 16))

    def period() -> int:
        if rng.random() < 0.1:
            return U64_MAX - rng.randint(0, 3)
        return min(U64_MAX, base * rng.choice([1, 1, 2, 3, 4, 6])
                   + rng.choice([0, 0, 0, 1]))

    def offset() -> int:
        return min(U64_MAX, rng.choice([
            0, 0, base, 2 * base,
            rng.randint(0, duration - 1),
            duration,
            duration + rng.randint(1, base),
        ]))

    with_process = index % 6 == 0
    participants = []
    for i, name in enumerate(rng.sample(_NAMES, rng.randint(1, 4))):
        if with_process and i == 0:
            participants.append(
                ProcessParticipant(name, period(), rng.choice(_PRIORITIES)))
            continue
        task_names = rng.sample(_TASK_NAMES, rng.randint(1, 3))
        participants.append(NativeParticipant(name, tuple(
            Task(t, period(), offset(), rng.choice(_PRIORITIES))
            for t in task_names)))
    return Case(f"gen-{index:03d}", duration, tuple(participants))


def corpus() -> list[Case]:
    return list(FIXED_CASES) + [generated_case(i) for i in range(CORPUS_SIZE)]
