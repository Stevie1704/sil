"""The intended authoring experience: a test is a scheduled participant,
written in Python, executed under pytest, and a failing assertion inside
the simulation fails the pytest test with that message."""

import pytest

from conftest import ROOT
from toys import add_producer, toy_manifest

from sil.manifest import SubscriberRoute
from sil.testing import RunFailure, participant_command, run_simulation

CHECKS = ROOT / "tests" / "participants" / "tick_checks.py"


def manifest_with_checker(checker: str):
    m = toy_manifest(duration_ns=100_000_000)
    m.add_channel("ticks", schema="toy.Counter")
    add_producer(m, "producer", channel="ticks", period_ns=10_000_000)
    m.add_process(
        "test",
        command=participant_command(CHECKS, checker),
        step_period_ns=10_000_000,
        subscribes=[SubscriberRoute("ticks", capacity=1024)],
    )
    return m


def test_in_schedule_assertions_pass_and_recording_is_typed(sil_run, tmp_path):
    result = run_simulation(
        manifest_with_checker("TicksAreCorrect"),
        runner=sil_run, workdir=tmp_path,
    )
    ticks = result.messages("ticks")
    assert len(ticks) == 10
    assert ticks[7] == (70_000_000, {"seq": 7, "value": 21})


def test_in_schedule_assertion_failure_fails_the_pytest_test(sil_run, tmp_path):
    # Tick seq 0 has value 0 and passes the bogus invariant; seq 1 is
    # published at t=10ms and seen (unit delay) at t=20ms.
    with pytest.raises(RunFailure, match="value should equal seq at t=20000000"):
        run_simulation(
            manifest_with_checker("TicksAreWrong"),
            runner=sil_run, workdir=tmp_path,
        )
