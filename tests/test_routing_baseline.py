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
import sys

import pytest

from conftest import BUILD_DIR, COMPAT_ROUTE_CAPACITY, ROOT

from sil.manifest import Manifest, SubscriberRoute
from tools import bench_routing

BENCH_SCHEMAS = json.loads((ROOT / "schemas" / "bench.json").read_text())

PERIOD_NS = 10_000_000
DURATION_NS = 100_000_000
PUBLISHES = DURATION_NS // PERIOD_NS
SMALL_BYTES = 16


def process_manifest(tmp_path, *, direction: str, burst: int):
    """One small-payload Process crossing with the default two-slot Arena."""
    messages = 30
    m = Manifest(duration_ns=(messages // burst) * PERIOD_NS)
    m.add_schemas({"bench.Small": BENCH_SCHEMAS["bench.Small"]})
    m.add_channel("payload", schema="bench.Small", transport="shm")
    command = [
        sys.executable,
        str(ROOT / "tools" / "bench_participants.py"),
    ]
    if direction == "in":
        m.add_native(
            "publisher",
            library=str(BUILD_DIR / "bench_publisher.silp"),
            config={
                "channel": "payload", "bytes": SMALL_BYTES,
                "period_ns": PERIOD_NS, "burst": burst,
            },
            publishes=["payload"],
        )
        m.add_process(
            "subscriber", command=command + ["subscriber"],
            step_period_ns=PERIOD_NS, priority=1,
            subscribes=[SubscriberRoute("payload", capacity=COMPAT_ROUTE_CAPACITY)],
        )
    else:
        m.add_process(
            "publisher", command=command + ["publisher", str(burst)],
            step_period_ns=PERIOD_NS, priority=1,
            publishes=["payload"],
        )
        m.add_native(
            "subscriber",
            library=str(BUILD_DIR / "bench_subscriber.silp"),
            config={"input": "payload", "period_ns": PERIOD_NS},
            subscribes=[SubscriberRoute("payload", capacity=COMPAT_ROUTE_CAPACITY)],
        )
    return m.write(tmp_path / f"{direction}-burst{burst}.json")


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
            subscribes=[SubscriberRoute("payload", capacity=COMPAT_ROUTE_CAPACITY)],
        )
    return m.write(tmp_path / f"bench{subscribers}.json")


def run_report(runner, manifest_path, tmp_path, *, recording: bool):
    """Runs the instrumented kernel and returns its whole report."""
    out = tmp_path / "counters.json"
    args = [str(runner), str(manifest_path)]
    args += ["-o", str(tmp_path / "out.mcap")] if recording else ["--no-recording"]
    proc = subprocess.run(
        args, capture_output=True, text=True,
        env={"SIL_COPY_COUNTERS_OUT": str(out), "PATH": "/usr/bin:/bin"},
        # --no-recording still writes a provenance side-car beside the
        # default output path; keep it out of the repository root.
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(out.read_text())


def count_copies(runner, manifest_path, tmp_path, *, recording: bool):
    """The deterministic subtree of that report: counts and route state."""
    return run_report(runner, manifest_path, tmp_path, recording=recording)[
        "deterministic"
    ]


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


def test_process_benchmark_reports_declared_slot_count(build_dir):
    config = bench_routing.process_config(
        build_dir, "small", direction="in", transport="shm", burst=2,
        recording=False, messages=30,
    )

    assert config.dimensions["slots"] == 2
    channel = config.manifest.to_doc()["channels"]["payload"]
    assert channel["slots"] == config.dimensions["slots"]


class TestReportShape:
    """What repeats and what varies live in separate subtrees (#63, #82)."""

    def test_deterministic_counters_and_observational_values_are_separate(
        self, sil_run_instrumented, tmp_path
    ):
        ref = native_manifest(tmp_path, subscribers=1)

        report = run_report(
            sil_run_instrumented, ref.path, tmp_path, recording=True
        )

        assert set(report) == {"run_exit_code", "deterministic", "observational"}
        assert set(report["deterministic"]) == (
            set(bench_routing.COPY_SITES) | {"routes", "replay_read_buffer"}
        )
        assert set(report["observational"]) == {
            "kernel_user_s", "kernel_system_s", "kernel_max_rss_bytes",
        }

    def test_a_clean_run_states_its_exit_code(
        self, sil_run_instrumented, tmp_path
    ):
        ref = native_manifest(tmp_path, subscribers=1)

        report = run_report(
            sil_run_instrumented, ref.path, tmp_path, recording=True
        )

        assert report["run_exit_code"] == 0

    def test_a_config_error_reports_before_any_counter_moves(
        self, sil_run_instrumented, tmp_path
    ):
        """The report describes the Run that failed to start, not a clean one."""
        out = tmp_path / "counters.json"
        proc = subprocess.run(
            [str(sil_run_instrumented), str(tmp_path / "absent.json"),
             "-o", str(tmp_path / "out.mcap")],
            capture_output=True, text=True,
            env={"SIL_COPY_COUNTERS_OUT": str(out), "PATH": "/usr/bin:/bin"},
        )

        assert proc.returncode == 2
        report = json.loads(out.read_text())
        assert report["run_exit_code"] == 2
        assert report["deterministic"]["routes"] == []
        assert report["deterministic"]["caller_to_kernel"]["count"] == 0


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

    @pytest.mark.parametrize(
        ("direction", "arena_site", "inline_site", "expected_arena"),
        [
            ("in", "arena_write", "inline_encode", 28),
            ("out", "arena_read", "inline_decode", 30),
        ],
    )
    def test_burst_within_default_slots_stays_entirely_in_arena(
        self, sil_run_instrumented, tmp_path, direction, arena_site,
        inline_site, expected_arena
    ):
        ref = process_manifest(tmp_path, direction=direction, burst=2)

        counters = count_copies(
            sil_run_instrumented, ref.path, tmp_path, recording=False
        )

        assert counters[arena_site]["count"] == expected_arena
        assert counters[inline_site]["count"] == 0

    @pytest.mark.parametrize(
        ("direction", "arena_site", "inline_site", "expected_arena",
         "expected_inline"),
        [
            ("in", "arena_write", "inline_encode", 18, 9),
            ("out", "arena_read", "inline_decode", 20, 10),
        ],
    )
    def test_burst_beyond_slots_falls_back_per_message(
        self, sil_run_instrumented, tmp_path, direction, arena_site,
        inline_site, expected_arena, expected_inline
    ):
        ref = process_manifest(tmp_path, direction=direction, burst=3)

        counters = count_copies(
            sil_run_instrumented, ref.path, tmp_path, recording=False
        )

        assert counters[arena_site]["count"] == expected_arena
        assert counters[inline_site]["count"] == expected_inline
