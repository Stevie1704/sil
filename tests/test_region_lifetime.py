"""Lifetime of the shared regions the kernel maps for a process participant.

Arenas and clock regions are temp files the kernel creates before it forks, so
the child can map them by path. Nothing may survive the run — not after a clean
run, and not after a run that fails halfway through mapping them.
"""

import os
import subprocess
import sys

from sil.manifest import Manifest

from conftest import ROOT
from test_run_boundary import ARRAY_SCHEMAS

# A single field far larger than any address space: its arena passes manifest
# validation and fails at ftruncate or mmap, whichever the platform rejects
# first.
UNMAPPABLE_SCHEMAS = {
    "big.Unmappable": {
        "fields": [{"name": "bulk", "type": "u64", "count": 576_460_752_303_423_488}]
    }
}


def participant(script):
    return [sys.executable, str(ROOT / "tests" / "participants" / script)]


def run_in(sil_run, manifest_path, tmp_path, region_dir):
    """Run the kernel with its temp files confined to `region_dir`."""
    return subprocess.run(
        [str(sil_run), str(manifest_path), "-o", str(tmp_path / "out.mcap")],
        capture_output=True,
        text=True,
        env={**os.environ, "TMPDIR": str(region_dir)},
    )


def leftovers(region_dir):
    return sorted(
        str(p) for p in region_dir.iterdir()
        if p.name.startswith(("sil_arena_", "sil_clock_"))
    )


def region_dir(tmp_path):
    path = tmp_path / "regions"
    path.mkdir()
    return path


class TestRegionLifetime:
    """Every mapped region unlinks its file, on success and on failure."""

    def _manifest(self, extra_channel=None):
        """A shimmed sink reading an arena-backed channel, so a run maps both
        region kinds. `extra_channel` appends a second subscribed arena."""
        m = Manifest(duration_ns=30_000_000)
        m.add_schemas(ARRAY_SCHEMAS)
        m.add_channel("payload", schema="big.Payload", transport="shm")
        m.add_channel("mirror", schema="big.Payload")
        subscribes = ["payload"]
        if extra_channel:
            m.add_schemas(UNMAPPABLE_SCHEMAS)
            m.add_channel(extra_channel, schema="big.Unmappable", transport="shm")
            subscribes.append(extra_channel)
        m.add_process(
            "source",
            command=participant("array_source.py"),
            step_period_ns=10_000_000,
            publishes=["payload"],
        )
        m.add_process(
            "sink",
            command=participant("array_echo.py"),
            step_period_ns=10_000_000,
            subscribes=subscribes,
            publishes=["mirror"],
            shim=True,
        )
        return m

    def test_clean_run_leaves_no_region_files(self, sil_run, tmp_path):
        regions = region_dir(tmp_path)
        manifest = self._manifest().write(tmp_path / "m.json").path
        proc = run_in(sil_run, manifest, tmp_path, regions)
        assert proc.returncode == 0, proc.stderr
        assert leftovers(regions) == []

    def test_failure_mapping_the_second_arena_releases_the_first(
        self, sil_run, tmp_path
    ):
        # The sink maps "payload" first and fails on the second arena. The run
        # must report the config error and still leave neither the first arena
        # nor the clock region behind.
        regions = region_dir(tmp_path)
        manifest = (
            self._manifest(extra_channel="oversized").write(tmp_path / "m.json").path
        )
        proc = run_in(sil_run, manifest, tmp_path, regions)
        assert proc.returncode == 2, proc.stderr
        assert "participant 'sink' channel 'oversized': arena" in proc.stderr
        assert leftovers(regions) == []

    def test_unusable_temp_dir_fails_the_clock_region_as_a_run_error(
        self, sil_run, tmp_path
    ):
        # The clock region reports through the exception class its call site
        # already uses: a run failure (exit 1), unlike an arena's exit 2.
        m = Manifest(duration_ns=30_000_000)
        m.add_schemas(ARRAY_SCHEMAS)
        m.add_channel("mirror", schema="big.Payload")
        m.add_process(
            "sink",
            command=participant("array_echo.py"),
            step_period_ns=10_000_000,
            publishes=["mirror"],
            shim=True,
        )
        manifest = m.write(tmp_path / "m.json").path
        proc = run_in(sil_run, manifest, tmp_path, tmp_path / "does-not-exist")
        assert proc.returncode == 1, proc.stderr
        assert "participant 'sink': clock region: mkstemp failed" in proc.stderr
