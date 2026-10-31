"""The Recording digest that the runner calculates while it writes (#328).

The expected value of each digest comes from `hashlib` over the file that the
Run left on disk. Work counts come from the instrumented runner, not from time.
"""

from __future__ import annotations

import hashlib
import json
import os
import resource
import signal
import subprocess
from pathlib import Path

import pytest

from conftest import BUILD_DIR
from test_provenance import sidecar
from toys import add_producer, add_thrower, toy_manifest

MS = 1_000_000
MIB = 1 << 20
CHUNK_BYTES = 1 << 20  # the default chunk size of the MCAP writer


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(runner: Path, manifest: Path, out: Path, *options: str, **kwargs):
    return subprocess.run(
        [str(runner), str(manifest), "-o", str(out), *options],
        capture_output=True, text=True, cwd=manifest.parent, **kwargs)


def multitask_manifest(directory: Path, duration_s: float) -> Path:
    """1,200 Messages per simulated second."""
    manifest = toy_manifest(duration_ns=int(duration_s * 1000) * MS)
    manifest.add_channel("ticks", schema="toy.Counter")
    manifest.add_native(
        "multi", library=str(BUILD_DIR / "toy_multitask.silp"),
        config={"channel": "ticks", "fast_period_ns": 1 * MS,
                "slow_period_ns": 5 * MS, "seed": 7},
        publishes=["ticks"])
    return manifest.write(directory / "manifest.json").path


def message_free_manifest(directory: Path) -> Path:
    manifest = toy_manifest(duration_ns=1)
    manifest.add_channel("ticks", schema="toy.Counter")
    return manifest.write(directory / "manifest.json").path


def counters(instrumented: Path, manifest: Path, out: Path,
             *options: str) -> dict:
    report = out.with_name(out.name + ".counters.json")
    proc = run(instrumented, manifest, out, *options,
               env={**os.environ, "SIL_COPY_COUNTERS_OUT": str(report)})
    assert proc.returncode == 0, proc.stderr
    return json.loads(report.read_text())


@pytest.mark.parametrize("shape", ["message_free", "small", "multi_chunk",
                                   "large"])
def test_the_digest_is_the_digest_of_the_finalized_file(
        sil_run, tmp_path, shape):
    manifest = {
        "message_free": lambda: message_free_manifest(tmp_path),
        "small": lambda: multitask_manifest(tmp_path, 0.05),
        "multi_chunk": lambda: multitask_manifest(tmp_path, 100),
        "large": lambda: multitask_manifest(tmp_path, 1000),
    }[shape]()
    out = tmp_path / "out.mcap"

    proc = run(sil_run, manifest, out)

    assert proc.returncode == 0, proc.stderr
    size = out.stat().st_size
    if shape == "multi_chunk":
        assert size > 3 * CHUNK_BYTES
    if shape == "large":
        assert size > 64 * MIB
    assert sidecar(out)["recording"] == {"digest_state": "calculated",
                                         "sha256": file_digest(out)}


def test_a_successful_run_does_not_read_the_recording_again(
        sil_run_instrumented, tmp_path):
    manifest = multitask_manifest(tmp_path, 100)
    out = tmp_path / "out.mcap"

    deterministic = counters(sil_run_instrumented, manifest, out)[
        "deterministic"]

    # The runner and the library are the only files hashed in one pass.
    assert deterministic["provenance_hash"]["count"] == 2
    assert deterministic["recording_digest"]["bytes"] == out.stat().st_size


def test_external_mode_accumulates_no_output_digest(
        sil_run_instrumented, tmp_path):
    manifest = multitask_manifest(tmp_path, 100)
    full, external = tmp_path / "full.mcap", tmp_path / "external.mcap"
    counters(sil_run_instrumented, manifest, full)

    report = counters(sil_run_instrumented, manifest, external,
                      "--provenance-mode", "external")

    assert report["deterministic"]["recording_digest"] == {"count": 0,
                                                           "bytes": 0}
    assert report["deterministic"]["provenance_hash"] == {"count": 0,
                                                          "bytes": 0}
    assert external.read_bytes() == full.read_bytes()
    assert sidecar(external)["recording"] == {"digest_state": "omitted",
                                              "sha256": None}


def test_the_digest_keeps_memory_bounded_as_the_file_grows(
        sil_run_instrumented, tmp_path):
    """A digest that kept the file in memory would add the size of the file.
    The Recording is far larger than the margin, so load on the machine does
    not decide this result."""
    manifest = multitask_manifest(tmp_path, 1000)
    full, external = tmp_path / "full.mcap", tmp_path / "external.mcap"

    peak = {
        "full": counters(sil_run_instrumented, manifest, full),
        "external": counters(sil_run_instrumented, manifest, external,
                             "--provenance-mode", "external"),
    }

    assert full.stat().st_size > 64 * MIB
    growth = (peak["full"]["observational"]["kernel_max_rss_bytes"]
              - peak["external"]["observational"]["kernel_max_rss_bytes"])
    assert growth < 16 * MIB


def test_a_failed_run_has_the_digest_of_its_partial_recording(
        sil_run_instrumented, tmp_path):
    manifest = toy_manifest(duration_ns=100 * MS)
    manifest.add_channel("ticks", schema="toy.Counter")
    add_producer(manifest, channel="ticks", period_ns=10 * MS)
    add_thrower(manifest, throw_in="task", kind="std", period_ns=30 * MS)
    ref = manifest.write(tmp_path / "manifest.json")
    out = tmp_path / "failed.mcap"
    report = out.with_name("failed.counters.json")

    proc = run(sil_run_instrumented, ref.path, out,
               env={**os.environ, "SIL_COPY_COUNTERS_OUT": str(report)})

    assert proc.returncode == 1
    document = sidecar(out)
    assert document["run_exit_code"] == 1
    assert document["recording"] == {"digest_state": "calculated",
                                     "sha256": file_digest(out)}
    deterministic = json.loads(report.read_text())["deterministic"]
    assert deterministic["recording_digest"]["bytes"] == out.stat().st_size


def limit_file_size(limit: int):
    """A write past `limit` then fails with EFBIG on Linux and on macOS."""
    def apply() -> None:
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
    return apply


@pytest.mark.parametrize("options, digest", [
    ((), "partial"), (("--provenance-mode", "external"), "omitted")],
    ids=["full", "external"])
def test_a_failed_write_fails_the_run_without_a_false_digest(
        sil_run, tmp_path, options, digest):
    manifest = multitask_manifest(tmp_path, 100)
    out = tmp_path / "out.mcap"
    limit = 2 * CHUNK_BYTES

    proc = run(sil_run, manifest, out, *options,
               preexec_fn=limit_file_size(limit))

    assert proc.returncode == 1
    assert "recording" in proc.stderr
    assert out.stat().st_size <= limit
    document = sidecar(out)
    assert document["run_exit_code"] == 1
    assert document["recording"] == (
        {"digest_state": "calculated", "sha256": file_digest(out)}
        if digest == "partial" else {"digest_state": "omitted",
                                     "sha256": None})


def test_a_run_that_opened_no_recording_claims_no_digest(sil_run, tmp_path):
    """A file of an earlier Run at this path is not the output of this Run."""
    out = tmp_path / "out.mcap"
    out.write_bytes(b"an earlier Run wrote this file")
    manifest = toy_manifest(duration_ns=1)
    manifest.add_native("gone", library=str(tmp_path / "missing.silp"),
                        config={}, publishes=[])
    ref = manifest.write(tmp_path / "manifest.json")

    proc = run(sil_run, ref.path, out)

    assert proc.returncode == 2
    document = sidecar(out)
    assert document["recording"]["digest_state"] == "failed"
    assert document["recording"]["sha256"] is None
    assert document["recording"]["reason"]
    assert any(error["artifact"] == "Recording"
               for error in document["errors"])
