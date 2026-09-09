"""Instrumentation the large-Message routing baseline rests on (issue #61).

Two things have to hold before any measurement is worth reading: the kernel can
run without a Recording, so subscriber copies can be told apart from Recording
I/O, and the copy counters report the copies the routing path actually makes
rather than a number inferred from timing. Both are asserted here as counts,
never as durations, so the baseline's conclusions stay independent of
wall-clock behavior.
"""

import json
import subprocess

import pytest

from conftest import BUILD_DIR, ROOT

from sil.manifest import Manifest

BENCH_SCHEMAS = json.loads((ROOT / "schemas" / "bench.json").read_text())

PERIOD_NS = 10_000_000
DURATION_NS = 100_000_000
PUBLISHES = DURATION_NS // PERIOD_NS
SMALL_BYTES = 16


def native_manifest(tmp_path, *, subscribers: int):
    """One Native publisher sending bench.Small to `subscribers` subscribers."""
    m = Manifest(duration_ns=DURATION_NS)
    m.add_schemas({"bench.Small": BENCH_SCHEMAS["bench.Small"]})
    m.add_channel("payload", schema="bench.Small")
    m.add_native(
        "source",
        library=str(BUILD_DIR / "bench_publisher.silp"),
        config={"channel": "payload", "bytes": SMALL_BYTES,
                "period_ns": PERIOD_NS, "burst": 1},
        publishes=["payload"],
    )
    for i in range(subscribers):
        m.add_native(
            f"sink{i}",
            library=str(BUILD_DIR / "bench_subscriber.silp"),
            config={"input": "payload", "period_ns": PERIOD_NS},
            subscribes=["payload"],
        )
    return m.write(tmp_path / f"bench{subscribers}.json")


def count_copies(runner, manifest_path, tmp_path, *, recording: bool):
    """Runs the instrumented kernel and returns its copy-counter report."""
    out = tmp_path / "counters.json"
    args = [str(runner), str(manifest_path)]
    args += ["-o", str(tmp_path / "out.mcap")] if recording else ["--no-recording"]
    proc = subprocess.run(
        args, capture_output=True, text=True,
        env={"SIL_COPY_COUNTERS_OUT": str(out), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(out.read_text())


class TestRecordingSwitch:
    """Recording-off has to be reachable from the run boundary."""

    def test_run_without_recording_writes_no_file(self, sil_run, tmp_path):
        ref = native_manifest(tmp_path, subscribers=1)
        proc = subprocess.run(
            [str(sil_run), str(ref.path), "--no-recording"],
            capture_output=True, text=True, cwd=tmp_path,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.startswith("manifest_hash ")
        assert list(tmp_path.glob("*.mcap")) == []

    def test_output_path_with_no_recording_is_a_config_error(
        self, sil_run, tmp_path
    ):
        ref = native_manifest(tmp_path, subscribers=1)
        proc = subprocess.run(
            [str(sil_run), str(ref.path), "--no-recording",
             "-o", str(tmp_path / "out.mcap")],
            capture_output=True, text=True,
        )
        assert proc.returncode == 2
        assert not (tmp_path / "out.mcap").exists()


class TestCopyCounters:
    """The counters state copies; they never stand in for a timing threshold."""

    @pytest.mark.parametrize("subscribers", [0, 1, 4])
    def test_one_caller_copy_and_one_copy_per_subscriber(
        self, sil_run_instrumented, tmp_path, subscribers
    ):
        ref = native_manifest(tmp_path, subscribers=subscribers)
        counters = count_copies(
            sil_run_instrumented, ref.path, tmp_path, recording=True
        )
        assert counters["caller_to_kernel"]["count"] == PUBLISHES
        assert counters["subscriber_copy"]["count"] == PUBLISHES * subscribers
        assert counters["subscriber_copy"]["bytes"] == (
            PUBLISHES * subscribers * SMALL_BYTES
        )

    def test_recording_adds_no_subscriber_or_caller_copy(
        self, sil_run_instrumented, tmp_path
    ):
        ref = native_manifest(tmp_path, subscribers=2)
        on = count_copies(sil_run_instrumented, ref.path, tmp_path, recording=True)
        off = count_copies(sil_run_instrumented, ref.path, tmp_path, recording=False)
        for site in ("caller_to_kernel", "subscriber_copy"):
            assert on[site] == off[site]
        assert on["recorded"]["count"] == PUBLISHES
        assert off["recorded"]["count"] == 0

    def test_production_runner_carries_no_counters(self, sil_run, tmp_path):
        """The instrumentation is absent from sil-run, not merely switched off."""
        ref = native_manifest(tmp_path, subscribers=1)
        out = tmp_path / "counters.json"
        proc = subprocess.run(
            [str(sil_run), str(ref.path), "-o", str(tmp_path / "out.mcap")],
            capture_output=True, text=True,
            env={"SIL_COPY_COUNTERS_OUT": str(out), "PATH": "/usr/bin:/bin"},
        )
        assert proc.returncode == 0, proc.stderr
        assert not out.exists()
