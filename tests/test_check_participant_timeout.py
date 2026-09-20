"""Checker forwarding of the Process-participant response deadline (issue #134).

The deadline is a run-boundary guard, so the two Runs `sil-check` compares
have to carry the same value a direct `sil-run` invocation carries. Otherwise
the Run the checker judges is not the Run the caller executes, and a stalled
Participant hangs the check that was supposed to bound it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from test_determinism import full_pipeline_manifest
from test_participant_timeout import timeout_manifest


# A stalled Participant has to be caught by the forwarded deadline. This guard
# only keeps a checker that forwards nothing from hanging the whole suite.
HARNESS_TIMEOUT_S = 120

# A runner stand-in: the two invocations of a check are otherwise
# indistinguishable from outside, and only the second one can be failed on
# demand.
STUB_RUNNER = '''#!/usr/bin/env python3
import json
import pathlib
import sys

log = pathlib.Path({log!r})
calls = json.loads(log.read_text()) if log.exists() else []
calls.append(sys.argv[1:])
log.write_text(json.dumps(calls))

pathlib.Path(sys.argv[sys.argv.index("-o") + 1]).write_bytes(b"recording")
if len(calls) == {fails_on_call!r}:
    sys.stderr.write("stub runner failed\\n")
    raise SystemExit(1)
'''


def stub_runner(tmp_path: Path, *, fails_on_call: int | None = None
                ) -> tuple[Path, Path]:
    """A fake runner and the file it appends each invocation's arguments to."""
    log = tmp_path / "calls.json"
    runner = tmp_path / "stub-run"
    runner.write_text(
        STUB_RUNNER.format(log=str(log), fails_on_call=fails_on_call)
    )
    runner.chmod(0o755)
    return runner, log


def calls(log: Path) -> list[list[str]]:
    return json.loads(log.read_text()) if log.exists() else []


def deadlines(log: Path) -> list[list[str]]:
    """The deadline argument pair of every recorded invocation, if any."""
    return [
        arguments[index : index + 2]
        for arguments in calls(log)
        for index, argument in enumerate(arguments)
        if argument == "--participant-timeout-ms"
    ]


def run_check(runner: Path, manifest: Path, *arguments: str):
    return subprocess.run(
        [sys.executable, "-m", "sil.check", str(manifest),
         "--runner", str(runner), *arguments],
        capture_output=True, text=True, timeout=HARNESS_TIMEOUT_S,
    )


class TestForwarding:
    def test_deadline_reaches_both_runs(self, tmp_path):
        runner, log = stub_runner(tmp_path)

        proc = run_check(runner, timeout_manifest(tmp_path, "ok"),
                         "--participant-timeout-ms", "30000")

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.startswith("deterministic: ")
        assert deadlines(log) == [["--participant-timeout-ms", "30000"]] * 2

    def test_omitting_the_deadline_passes_no_argument(self, tmp_path):
        runner, log = stub_runner(tmp_path)

        proc = run_check(runner, timeout_manifest(tmp_path, "ok"))

        assert proc.returncode == 0, proc.stderr
        assert len(calls(log)) == 2
        assert deadlines(log) == []

    def test_the_runners_largest_deadline_is_forwarded_verbatim(self, tmp_path):
        # The literal the runner's own suite pins as its largest accepted
        # deadline. The checker's ceiling is a copy of the runner's, so both
        # suites have to name the same number for either to stay honest.
        runner, log = stub_runner(tmp_path)
        largest = str(2**63 - 1)

        proc = run_check(runner, timeout_manifest(tmp_path, "ok"),
                         "--participant-timeout-ms", largest)

        assert proc.returncode == 0, proc.stderr
        assert deadlines(log) == [["--participant-timeout-ms", largest]] * 2

    def test_first_run_failure_prevents_the_second(self, tmp_path):
        runner, log = stub_runner(tmp_path, fails_on_call=1)

        proc = run_check(runner, timeout_manifest(tmp_path, "ok"),
                         "--participant-timeout-ms", "30000")

        assert proc.returncode == 1
        assert "stub runner failed" in proc.stderr
        assert len(calls(log)) == 1

    def test_second_run_failure_is_propagated_not_called_a_violation(
        self, tmp_path
    ):
        runner, log = stub_runner(tmp_path, fails_on_call=2)

        proc = run_check(runner, timeout_manifest(tmp_path, "ok"),
                         "--participant-timeout-ms", "30000")

        assert proc.returncode == 1
        assert "stub runner failed" in proc.stderr
        assert "DETERMINISM VIOLATION" not in proc.stderr
        assert deadlines(log) == [["--participant-timeout-ms", "30000"]] * 2


class TestArgumentRejection:
    @pytest.mark.parametrize(
        "arguments",
        [
            ["--participant-timeout-ms"],
            ["--participant-timeout-ms", "0"],
            ["--participant-timeout-ms", "-1"],
            ["--participant-timeout-ms", "1.5"],
            ["--participant-timeout-ms", "1", "--participant-timeout-ms", "2"],
            ["--participant-timeout-ms", str(2**63)],
        ],
    )
    def test_invalid_deadline_is_rejected_before_spawn(
        self, sil_run, tmp_path, arguments
    ):
        marker = tmp_path / "spawned"
        manifest = timeout_manifest(tmp_path, "ok", marker)

        proc = run_check(sil_run, manifest, *arguments)

        assert proc.returncode == 2
        assert "--participant-timeout-ms" in proc.stderr
        assert not marker.exists()


class TestRunBoundary:
    def test_stalled_participant_fails_the_check_with_the_run_diagnostic(
        self, sil_run, tmp_path
    ):
        manifest = timeout_manifest(tmp_path, "step")

        proc = run_check(sil_run, manifest, "--participant-timeout-ms", "100")

        assert proc.returncode == 1
        assert "DETERMINISM VIOLATION" not in proc.stderr
        assert "hung" in proc.stderr
        assert "timeout" in proc.stderr
        assert "virtual time 0 ns" in proc.stderr

    def test_deadline_does_not_change_the_recorded_digest(
        self, sil_run, tmp_path
    ):
        manifest = full_pipeline_manifest(tmp_path).path

        without = run_check(sil_run, manifest)
        with_deadline = run_check(sil_run, manifest,
                                  "--participant-timeout-ms", "30000")

        assert without.returncode == 0, without.stderr
        assert with_deadline.returncode == 0, with_deadline.stderr
        assert without.stdout == with_deadline.stdout
