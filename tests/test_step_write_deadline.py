"""The Response deadline covers the write of the Step line (issue #386).

A child that stops reading stdin must not hold the runner in a write to its
stdin pipe. Each case sends a Step line larger than the pipe buffer.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import ROOT
from toys import toy_manifest

from sil.manifest import Manifest, SubscriberRoute


STDIN_STALL = ROOT / "tests" / "participants" / "stdin_stall.py"
BLOB_BYTES = 300_000
STEP_PERIOD_NS = 10_000_000
# A runner without the fix never ends; this bound turns that into a failure.
# It allows for the 2 s grace before the runner sends SIGTERM to a child.
RUN_BOUND_S = 20


def blob_manifest(tmp_path: Path, sink_mode: str) -> tuple[Path, Path]:
    """A source that publishes BLOB_BYTES per Step, and a sink in `sink_mode`."""
    marker = tmp_path / "sink-stopped-reading"
    manifest: Manifest = toy_manifest(duration_ns=10 * STEP_PERIOD_NS)
    manifest.add_schemas({
        "test.Blob": {"fields": [{"name": "bytes", "type": "u8",
                                  "count": BLOB_BYTES}]}
    })
    manifest.add_channel("blob", schema="test.Blob")
    manifest.add_process(
        "a_src",
        command=[sys.executable, str(STDIN_STALL), "source"],
        step_period_ns=STEP_PERIOD_NS,
        publishes=["blob"],
    )
    manifest.add_process(
        "b_sink",
        command=[sys.executable, str(STDIN_STALL), sink_mode, str(marker)],
        step_period_ns=STEP_PERIOD_NS,
        subscribes=[SubscriberRoute("blob", capacity=4)],
    )
    return manifest.write(tmp_path / f"{sink_mode}.json").path, marker


def start(sil_run: Path, manifest: Path, *args: str) -> subprocess.Popen:
    return subprocess.Popen(
        [str(sil_run), str(manifest), "-o", str(manifest.parent / "out.mcap"),
         *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=manifest.parent,
    )


def finish(runner: subprocess.Popen) -> str:
    try:
        _, stderr = runner.communicate(timeout=RUN_BOUND_S)
    except subprocess.TimeoutExpired:
        runner.kill()
        runner.communicate()
        pytest.fail("the runner did not end while it wrote a Step line")
    return stderr


def wait_until_sink_stalls(marker: Path) -> None:
    deadline = time.monotonic() + RUN_BOUND_S
    while not marker.exists():
        assert time.monotonic() < deadline, "the sink did not stop reading"
        time.sleep(0.01)


def test_missed_deadline_during_the_write_is_a_timeout(sil_run, tmp_path):
    manifest, _ = blob_manifest(tmp_path, "stop-reading")
    runner = start(sil_run, manifest, "--participant-timeout-ms", "1000")
    stderr = finish(runner)

    assert runner.returncode == 1, stderr
    assert "participant 'b_sink': timeout" in stderr
    report = json.loads((tmp_path / "out.mcap.failure.json").read_text())
    assert report["category"] == "timeout"
    assert report["participant"] == "b_sink"


@pytest.mark.parametrize(
    "termination_signal", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP]
)
def test_signal_ends_a_run_blocked_in_the_write(
    sil_run, tmp_path, termination_signal
):
    manifest, marker = blob_manifest(tmp_path, "stop-reading")
    # No deadline: only the signal can end this Run.
    runner = start(sil_run, manifest)
    wait_until_sink_stalls(marker)
    # Let the runner fill the pipe, so the signal comes after a partial write.
    time.sleep(0.3)
    runner.send_signal(termination_signal)
    stderr = finish(runner)

    assert runner.returncode == 1, stderr
    assert "run interrupted" in stderr


def test_runner_drains_stderr_while_it_waits_to_write(sil_run, tmp_path):
    manifest, _ = blob_manifest(tmp_path, "stderr-flood")
    runner = start(sil_run, manifest, "--participant-timeout-ms", "5000")
    stderr = finish(runner)

    assert runner.returncode == 0, stderr[-2000:]
    assert "stderr flood done" in stderr
