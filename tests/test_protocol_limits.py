"""Run-boundary tests for Step-protocol resource limits (issue #120)."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT
from test_run_boundary import read_mcap
from toys import toy_manifest


LIMIT_PARTICIPANT = ROOT / "tests" / "participants" / "protocol_limits.py"
TIMEOUT_PARTICIPANT = ROOT / "tests" / "participants" / "timeout.py"


def _manifest(
    tmp_path: Path,
    mode: str,
    observer: Path | None = None,
    *,
    publishes: bool = False,
):
    manifest = toy_manifest(duration_ns=10_000_000)
    if publishes:
        manifest.add_channel("ticks", schema="toy.Counter")
    command = [sys.executable, str(LIMIT_PARTICIPANT), mode]
    if observer is not None:
        command.append(str(observer))
    manifest.add_process(
        "bounded",
        command=command,
        step_period_ns=10_000_000,
        publishes=["ticks"] if publishes else [],
    )
    return manifest.write(tmp_path / f"{mode}.json").path


def _run(sil_run: Path, manifest: Path, *args: str):
    return subprocess.run(
        [str(sil_run), str(manifest), *args],
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "arguments",
    [
        ["--max-protocol-line-bytes"],
        ["--max-protocol-line-bytes", "0"],
        ["--max-protocol-line-bytes", "-1"],
        ["--max-protocol-line-bytes", "1.5"],
        ["--max-protocol-line-bytes", "1", "--max-protocol-line-bytes", "2"],
        ["--max-protocol-line-bytes", str(2**64)],
        ["--max-step-output-messages"],
        ["--max-step-output-messages", "0"],
        ["--max-step-output-messages", "-1"],
        ["--max-step-output-messages", "1.5"],
        ["--max-step-output-messages", "1", "--max-step-output-messages", "2"],
        ["--max-step-output-messages", str(2**64)],
        ["--max-step-inline-payload-bytes"],
        ["--max-step-inline-payload-bytes", "0"],
        ["--max-step-inline-payload-bytes", "-1"],
        ["--max-step-inline-payload-bytes", "1.5"],
        [
            "--max-step-inline-payload-bytes",
            "1",
            "--max-step-inline-payload-bytes",
            "2",
        ],
        ["--max-step-inline-payload-bytes", str(2**64)],
    ],
)
def test_invalid_protocol_limit_is_rejected_before_spawn(
    sil_run, tmp_path, arguments
):
    marker = tmp_path / "spawned"
    manifest = toy_manifest(duration_ns=10_000_000)
    manifest.add_process(
        "marker",
        command=[
            sys.executable,
            str(TIMEOUT_PARTICIPANT),
            "ok",
            str(marker),
        ],
        step_period_ns=10_000_000,
    )
    path = manifest.write(tmp_path / "invalid.json").path

    proc = _run(sil_run, path, *arguments, "--no-recording")

    assert proc.returncode == 2
    assert not marker.exists()


def test_ready_response_line_limit_is_a_run_failure(sil_run, tmp_path):
    proc = _run(
        sil_run,
        _manifest(tmp_path, "ready-long"),
        "--max-protocol-line-bytes",
        "128",
        "--no-recording",
    )

    assert proc.returncode == 1
    assert "participant 'bounded'" in proc.stderr
    assert "initialization" in proc.stderr
    assert "maximum protocol line length" in proc.stderr
    assert "configured 128 bytes" in proc.stderr
    assert "observed" in proc.stderr


def test_unterminated_step_response_is_rejected_while_reading(
    sil_run, tmp_path
):
    proc = _run(
        sil_run,
        _manifest(tmp_path, "step-unterminated"),
        "--max-protocol-line-bytes",
        "128",
        "--no-recording",
    )

    assert proc.returncode == 1
    assert "participant 'bounded'" in proc.stderr
    assert "Step at virtual time 0 ns" in proc.stderr
    assert "maximum protocol line length" in proc.stderr
    assert "configured 128 bytes" in proc.stderr
    assert "observed" in proc.stderr


def test_output_message_count_is_checked_before_decoding_or_publishing(
    sil_run, tmp_path
):
    output = tmp_path / "count.mcap"
    manifest = _manifest(tmp_path, "many", publishes=True)
    proc = _run(
        sil_run,
        manifest,
        "--max-step-output-messages",
        "1",
        "-o",
        str(output),
    )

    assert proc.returncode == 1
    assert "participant 'bounded'" in proc.stderr
    assert "maximum output-Message count per Step" in proc.stderr
    assert "configured 1 Messages" in proc.stderr
    assert "observed 2 Messages" in proc.stderr
    _, messages = read_mcap(output)
    assert messages == []


def test_inline_payload_limit_is_checked_before_publishing(
    sil_run, tmp_path
):
    output = tmp_path / "inline.mcap"
    manifest = _manifest(tmp_path, "inline", publishes=True)
    proc = _run(
        sil_run,
        manifest,
        "--max-step-inline-payload-bytes",
        "8",
        "-o",
        str(output),
    )

    assert proc.returncode == 1
    assert "participant 'bounded'" in proc.stderr
    assert "maximum total inline payload bytes per Step" in proc.stderr
    assert "configured 8 bytes" in proc.stderr
    assert "observed 16 bytes" in proc.stderr
    _, messages = read_mcap(output)
    assert messages == []


def test_arena_payload_is_not_charged_to_inline_limit(sil_run, tmp_path):
    from test_run_boundary import ARRAY_SCHEMAS

    from sil.manifest import Manifest

    manifest = Manifest(duration_ns=10_000_000)
    manifest.add_schemas(ARRAY_SCHEMAS)
    manifest.add_channel("payload", schema="big.Payload", transport="shm")
    manifest.add_process(
        "source",
        command=[sys.executable, str(ROOT / "tests" / "participants" / "array_source.py")],
        step_period_ns=10_000_000,
        publishes=["payload"],
    )
    path = manifest.write(tmp_path / "arena.json").path

    proc = _run(
        sil_run,
        path,
        "--max-step-inline-payload-bytes",
        "1",
        "--no-recording",
    )

    assert proc.returncode == 0, proc.stderr


def test_explicit_large_limits_preserve_recording_bytes(sil_run, tmp_path):
    manifest = _manifest(tmp_path, "inline", publishes=True)
    without_limits = _run(sil_run, manifest, "-o", str(tmp_path / "without.mcap"))
    with_limits = _run(
        sil_run,
        manifest,
        "--max-protocol-line-bytes",
        "1000000",
        "--max-step-output-messages",
        "1000",
        "--max-step-inline-payload-bytes",
        "1000000",
        "-o",
        str(tmp_path / "with.mcap"),
    )

    assert without_limits.returncode == with_limits.returncode == 0
    assert without_limits.stdout == with_limits.stdout
    assert (tmp_path / "without.mcap").read_bytes() == (
        tmp_path / "with.mcap"
    ).read_bytes()


def test_limit_failure_reaps_child_and_removes_working_directory(
    sil_run, tmp_path
):
    observer = tmp_path / "observer.txt"
    proc = _run(
        sil_run,
        _manifest(tmp_path, "step-unterminated", observer),
        "--max-protocol-line-bytes",
        "128",
        "--no-recording",
    )

    assert proc.returncode == 1
    pid_text, working_directory = observer.read_text().splitlines()
    pid = int(pid_text)
    assert not Path(working_directory).exists()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_limit_failure_keeps_only_partial_recording_metadata(
    sil_run, tmp_path
):
    output = tmp_path / "partial.mcap"
    manifest = _manifest(tmp_path, "inline", publishes=True)
    proc = _run(
        sil_run,
        manifest,
        "--max-step-inline-payload-bytes",
        "8",
        "-o",
        str(output),
    )

    assert proc.returncode == 1
    assert proc.stdout == ""
    metadata, messages = read_mcap(output)
    assert metadata["manifest_hash"] == hashlib.sha256(
        manifest.read_bytes()
    ).hexdigest()
    assert messages == []


def test_inline_fallback_on_an_arena_channel_is_charged(sil_run, tmp_path):
    """The Transport is read per Message, not per Channel.

    The Arena exemption belongs to a Message the line names with `shm_seq`.
    A participant that answers with an inline `data` field on an Arena-backed
    Channel is taking the inline transport and is charged for it, which is
    what keeps the exemption from becoming a way to opt out of the budget.
    """
    manifest = toy_manifest(duration_ns=10_000_000)
    manifest.add_channel("ticks", schema="toy.Counter", transport="shm")
    manifest.add_process(
        "bounded",
        command=[sys.executable, str(LIMIT_PARTICIPANT), "inline"],
        step_period_ns=10_000_000,
        publishes=["ticks"],
    )
    path = manifest.write(tmp_path / "arena-inline.json").path

    proc = _run(
        sil_run, path, "--max-step-inline-payload-bytes", "8", "--no-recording"
    )

    assert proc.returncode == 1
    assert "maximum total inline payload bytes per Step" in proc.stderr
    assert "observed 16 bytes" in proc.stderr


def test_the_kernel_buffers_no_more_than_the_line_limit(sil_run, tmp_path):
    """The read-side guarantee, asserted without measuring memory.

    copy_counters.hpp states that the kernel's own resource use is
    observational and never asserted on, so this pins the bound through the
    diagnostic instead. The fixture offers a 64 MiB line against a 1 MiB
    limit. The kernel inspects each read before appending it and stops at the
    limit, so the observed value can exceed the limit by at most the 4 KiB it
    reads at a time. A kernel that buffered the line first would report a
    number near what the child sent.
    """
    sent_bytes = 64 * 1024 * 1024
    line_bytes = 1024 * 1024
    proc = subprocess.run(
        [
            str(sil_run),
            str(_manifest(tmp_path, "flood", publishes=True)),
            "--max-protocol-line-bytes",
            str(line_bytes),
            "--no-recording",
        ],
        capture_output=True,
        text=True,
        env=dict(os.environ, SIL_TEST_FLOOD_LINE_BYTES=str(sent_bytes)),
    )

    assert proc.returncode == 1
    assert "maximum protocol line length" in proc.stderr
    observed = int(proc.stderr.split("observed ")[1].split(" bytes")[0])
    assert line_bytes < observed <= line_bytes + 4096
