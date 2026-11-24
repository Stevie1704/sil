"""The proven Lean scheduler model against the real runner (#404).

The cases, the trace reader and the oracle are the ones of
`test_scheduler_trace.py`. The tests need the `scheduler-model` executable of
`verification/scheduler/`. Without it they skip, so the regular suite needs no
Lean installation; the Lean PR job sets `SIL_REQUIRE_LEAN_MODEL=1` to make a
missing executable fail instead.

The model reads and writes decimal text, which keeps all values up to
`UINT64_MAX` exact.
"""

import os
import subprocess
from pathlib import Path

import pytest
from conftest import ROOT
from scheduler_trace import (
    RUN_TIMEOUT_S,
    Case,
    corpus,
    expected_trace,
    first_divergence,
    observed_trace,
    registration_entries,
    run_case,
)

MODEL = (ROOT / "verification" / "scheduler" / ".lake" / "build" / "bin"
         / "scheduler-model")

CASES = corpus()


def model_input(case: Case) -> str:
    """One line with the duration, then `period offset priority index`."""
    lines = [str(case.duration_ns)]
    lines += [f"{e.period_ns} {e.offset_ns} {e.priority} {e.index}"
              for e in registration_entries(case)]
    return "\n".join(lines) + "\n"


def model_trace(case: Case, output: str) -> list[tuple[int, str, str]]:
    """Map the model's `time index` lines to trace entries."""
    entries = registration_entries(case)
    trace = []
    for line in output.splitlines():
        try:
            time_ns, index = line.split()
            e = entries[int(index)]
        except (ValueError, IndexError) as error:
            raise AssertionError(
                f"case {case.case_id}: unreadable model output line {line!r}"
            ) from error
        trace.append((int(time_ns), e.participant, e.task))
    return trace


def failure_context(case: Case) -> str:
    return ("rerun: SIL_REQUIRE_LEAN_MODEL=1 .venv/bin/python -m pytest "
            f"tests/test_scheduler_lean.py -k '{case.case_id}'"
            f"\ninput schedule:\n{case.describe()}")


def assert_same(case: Case, expected: list, observed: list, what: str) -> None:
    divergence = first_divergence(expected, observed)
    assert divergence is None, (
        f"case {case.case_id}: {what} at {divergence}\n{failure_context(case)}")


@pytest.fixture(scope="module")
def scheduler_model() -> Path:
    if not MODEL.exists():
        message = (f"Lean scheduler model not built at {MODEL}; run "
                   "`lake build` in verification/scheduler")
        if os.environ.get("SIL_REQUIRE_LEAN_MODEL") == "1":
            pytest.fail(message)
        pytest.skip(message)
    return MODEL


def run_model(model: Path, case: Case, workdir: Path):
    path = workdir / f"{case.case_id}.model"
    path.write_text(model_input(case))
    proc = subprocess.run([str(model), str(path)], capture_output=True,
                          text=True, timeout=RUN_TIMEOUT_S, check=False)
    assert proc.returncode == 0, (
        f"case {case.case_id}: scheduler-model exit {proc.returncode}\n"
        f"{proc.stderr}\n{failure_context(case)}")
    return model_trace(case, proc.stdout)


@pytest.mark.parametrize("case", CASES, ids=[c.case_id for c in CASES])
def test_model_trace_matches_the_runner(scheduler_model, sil_run, tmp_path,
                                        case):
    model = run_model(scheduler_model, case, tmp_path)
    assert_same(case, expected_trace(case), model,
                "Lean model trace differs from the contract")
    runner = observed_trace(case, run_case(sil_run, case, tmp_path))
    assert_same(case, model, runner,
                "sil-run trace differs from the Lean model")


def test_model_rejects_duplicate_indices(scheduler_model, tmp_path):
    path = tmp_path / "dup.model"
    path.write_text("10\n1 0 0 0\n1 0 0 0\n")
    proc = subprocess.run([str(scheduler_model), str(path)],
                          capture_output=True, text=True,
                          timeout=RUN_TIMEOUT_S, check=False)
    assert proc.returncode == 2
    assert "distinct" in proc.stderr
