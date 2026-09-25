"""Process-participant response deadline through the pytest Run helper
(issue #183).

`run_simulation` forwards the runner's existing `--participant-timeout-ms`
guard unchanged, so a test that uses the helper gets a bounded wait without a
subprocess wrapper of its own. The runner owns the deadline semantics; these
tests pin only the helper's half: forwarding, omission, validation, and the
failure it surfaces.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from test_check_participant_timeout import calls, deadlines, stub_runner
from test_participant_timeout import stalling_manifest
from test_process_descendants import assert_cleanup, descendant_manifest
from test_pytest_frontend import manifest_with_checker

from sil.testing import RunFailure, run_simulation


class TestForwarding:
    def test_the_deadline_reaches_the_runner_verbatim(self, tmp_path):
        runner, log = stub_runner(tmp_path)

        run_simulation(stalling_manifest("ok"), runner=runner,
                       workdir=tmp_path, participant_timeout_ms=30000)

        assert deadlines(log) == [["--participant-timeout-ms", "30000"]]

    def test_the_runners_largest_deadline_is_forwarded(self, tmp_path):
        runner, log = stub_runner(tmp_path)
        largest = 2**63 - 1

        run_simulation(stalling_manifest("ok"), runner=runner,
                       workdir=tmp_path, participant_timeout_ms=largest)

        assert deadlines(log) == [["--participant-timeout-ms", str(largest)]]

    def test_omitting_the_deadline_passes_no_argument(self, tmp_path):
        runner, log = stub_runner(tmp_path)

        run_simulation(stalling_manifest("ok"), runner=runner,
                       workdir=tmp_path)

        assert len(calls(log)) == 1
        assert deadlines(log) == []


class TestValidation:
    @pytest.mark.parametrize(
        "value, error",
        [
            (0, ValueError),
            (-1, ValueError),
            (2**63, ValueError),
            (1.5, TypeError),
            ("100", TypeError),
            (True, TypeError),
        ],
    )
    def test_an_invalid_deadline_fails_before_the_run(
        self, tmp_path, value, error
    ):
        runner, log = stub_runner(tmp_path)

        with pytest.raises(error, match="participant_timeout_ms"):
            run_simulation(stalling_manifest("ok"), runner=runner,
                           workdir=tmp_path, participant_timeout_ms=value)

        assert calls(log) == []
        assert not (tmp_path / "manifest.json").exists()


class TestRunBoundary:
    def test_a_participant_stalled_at_ready_is_a_run_failure(
        self, sil_run, tmp_path
    ):
        with pytest.raises(RunFailure) as failure:
            run_simulation(stalling_manifest("init"), runner=sil_run,
                           workdir=tmp_path, participant_timeout_ms=100)

        assert failure.value.exit_code == 1
        message = str(failure.value)
        assert "hung" in message
        assert "initialization" in message
        assert "timeout" in message

    def test_a_participant_stalled_at_step_done_is_a_run_failure(
        self, sil_run, tmp_path
    ):
        with pytest.raises(RunFailure) as failure:
            run_simulation(stalling_manifest("step"), runner=sil_run,
                           workdir=tmp_path, participant_timeout_ms=100)

        assert failure.value.exit_code == 1
        message = str(failure.value)
        assert "hung" in message
        assert "timeout" in message
        assert "virtual time 0 ns" in message

    def test_a_stalled_participants_descendants_are_cleaned_up(
        self, sil_run, tmp_path, monkeypatch
    ):
        regions = tmp_path / "regions"
        regions.mkdir()
        observer = tmp_path / "observer.json"
        # The helper inherits this process's environment; scoping TMPDIR lets
        # the cleanup check see every Arena and Clock region the Run creates.
        monkeypatch.setenv("TMPDIR", str(regions))
        workdir = tmp_path / "run"
        workdir.mkdir()

        with pytest.raises(RunFailure) as failure:
            run_simulation(descendant_manifest("descendant-timeout", observer),
                           runner=sil_run, workdir=workdir,
                           participant_timeout_ms=100)

        assert "timeout" in str(failure.value)
        outcome = SimpleNamespace(returncode=failure.value.exit_code,
                                  stderr=str(failure.value))
        assert_cleanup(outcome, observer, regions, expected_code=1)

    def test_a_timely_run_records_the_same_bytes_with_or_without_it(
        self, sil_run, tmp_path
    ):
        (tmp_path / "without").mkdir()
        (tmp_path / "bounded").mkdir()
        without = run_simulation(manifest_with_checker("TicksAreCorrect"),
                                 runner=sil_run, workdir=tmp_path / "without")
        bounded = run_simulation(manifest_with_checker("TicksAreCorrect"),
                                 runner=sil_run, workdir=tmp_path / "bounded",
                                 participant_timeout_ms=30000)

        assert without.manifest_hash == bounded.manifest_hash
        assert without.mcap_path.read_bytes() == bounded.mcap_path.read_bytes()
