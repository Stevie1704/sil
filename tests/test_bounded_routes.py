"""Bounded subscriber route behavior at the Run boundary (issue #75)."""

import hashlib
import json
import os
import subprocess

from conftest import BUILD_DIR
from toys import add_producer, toy_manifest

from sil.manifest import SubscriberRoute


def slow_subscriber(m, *, capacity, overflow="fail", name="slow"):
    m.add_native(
        name,
        library=str(BUILD_DIR / "bench_subscriber.silp"),
        config={"input": "ticks", "period_ns": 1_000_000_000, "drain": False},
        subscribes=[
            SubscriberRoute("ticks", capacity=capacity, overflow=overflow)
        ],
    )


def bounded_manifest(*, capacity, overflow="fail"):
    m = toy_manifest(duration_ns=50_000_000)
    m.add_channel("ticks", schema="toy.Counter")
    add_producer(m, "publisher", channel="ticks", period_ns=10_000_000)
    slow_subscriber(m, capacity=capacity, overflow=overflow)
    return m


class TestFailOnCapacity:
    def test_aborts_with_the_complete_route_diagnostic(self, run_sil, tmp_path):
        path = bounded_manifest(capacity=2).write(tmp_path / "m.json").path

        proc = run_sil(path)

        assert proc.returncode == 1
        assert "Channel 'ticks'" in proc.stderr
        assert "publisher 'publisher'" in proc.stderr
        assert "subscriber 'slow'" in proc.stderr
        assert "capacity 2" in proc.stderr
        assert "current depth 2" in proc.stderr
        assert "policy 'fail'" in proc.stderr

    def test_overflow_failure_and_final_live_depth_are_instrumented(
        self, sil_run_instrumented, tmp_path
    ):
        path = bounded_manifest(capacity=2).write(tmp_path / "m.json").path
        report = tmp_path / "routes.json"
        env = os.environ.copy()
        env["SIL_COPY_COUNTERS_OUT"] = str(report)

        proc = subprocess.run(
            [
                str(sil_run_instrumented),
                str(path),
                "-o",
                str(tmp_path / "out.mcap"),
            ],
            capture_output=True,
            text=True,
            env=env,
        )

        assert proc.returncode == 1
        assert json.loads(report.read_text())["routes"] == [
            {
                "channel": "ticks",
                "subscriber": "slow",
                "current_depth": 2,
                "high_water_depth": 2,
                "dropped_newest": 0,
                "overflow_failures": 1,
            }
        ]


class TestDropNewest:
    def test_drops_are_deterministic_and_reported_by_test_instrumentation(
        self, sil_run_instrumented, tmp_path
    ):
        path = bounded_manifest(capacity=2, overflow="drop_newest").write(
            tmp_path / "m.json"
        ).path

        results = []
        for name in ("a", "b"):
            recording = tmp_path / f"{name}.mcap"
            report = tmp_path / f"{name}.json"
            env = os.environ.copy()
            env["SIL_COPY_COUNTERS_OUT"] = str(report)
            proc = subprocess.run(
                [str(sil_run_instrumented), str(path), "-o", str(recording)],
                capture_output=True,
                text=True,
                env=env,
            )
            results.append(
                (proc, recording.read_bytes(), json.loads(report.read_text()))
            )

        assert results[0][0].returncode == results[1][0].returncode == 0
        assert results[0][1] == results[1][1]
        assert results[0][2]["subscriber_copy"]["count"] == 2
        assert results[1][2]["subscriber_copy"]["count"] == 2
        assert results[0][2]["routes"] == results[1][2]["routes"] == [
            {
                "channel": "ticks",
                "subscriber": "slow",
                "current_depth": 2,
                "high_water_depth": 2,
                "dropped_newest": 3,
                "overflow_failures": 0,
            }
        ]


class TestBoundedPathCoverage:
    def test_zero_subscribers_needs_no_route_capacity(self, run_sil, tmp_path):
        m = toy_manifest(duration_ns=50_000_000)
        m.add_channel("ticks", schema="toy.Counter")
        add_producer(m, "publisher", channel="ticks", period_ns=10_000_000)

        proc = run_sil(m.write(tmp_path / "m.json").path)

        assert proc.returncode == 0, proc.stderr

    def test_many_subscribers_have_independent_capacity(
        self, run_sil, tmp_path
    ):
        m = toy_manifest(duration_ns=50_000_000)
        m.add_channel("ticks", schema="toy.Counter")
        add_producer(m, "publisher", channel="ticks", period_ns=10_000_000)
        slow_subscriber(m, name="a", capacity=1, overflow="drop_newest")
        slow_subscriber(m, name="b", capacity=2)

        proc = run_sil(m.write(tmp_path / "m.json").path)

        assert proc.returncode == 1
        assert "subscriber 'b'" in proc.stderr
        assert "capacity 2" in proc.stderr

    def test_suppressed_messages_never_occupy_capacity(
        self, run_sil, tmp_path
    ):
        m = bounded_manifest(capacity=1)
        m.add_interceptor("ticks", kind="drop")

        proc = run_sil(m.write(tmp_path / "m.json").path)

        assert proc.returncode == 0, proc.stderr

    def test_legacy_string_route_remains_unbounded(self, run_sil, tmp_path):
        document = bounded_manifest(capacity=1).to_doc()
        document["participants"]["slow"]["subscribes"] = ["ticks"]
        path = tmp_path / "legacy.json"
        path.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")))

        proc = run_sil(path)

        assert proc.returncode == 0, proc.stderr
        assert hashlib.sha256(path.read_bytes()).hexdigest() in proc.stdout

    def test_route_object_without_policy_fields_remains_unbounded(
        self, run_sil, tmp_path
    ):
        document = bounded_manifest(capacity=1).to_doc()
        document["participants"]["slow"]["subscribes"] = [{"channel": "ticks"}]
        path = tmp_path / "legacy-object.json"
        path.write_text(
            json.dumps(document, sort_keys=True, separators=(",", ":"))
        )

        proc = run_sil(path)

        assert proc.returncode == 0, proc.stderr
        assert hashlib.sha256(path.read_bytes()).hexdigest() in proc.stdout

    def test_replay_publication_uses_the_same_bounded_route(
        self, run_sil, tmp_path
    ):
        source = toy_manifest(duration_ns=50_000_000)
        source.add_channel("ticks", schema="toy.Counter")
        add_producer(
            source, "live", channel="ticks", period_ns=10_000_000
        )
        source_run = run_sil(
            source.write(tmp_path / "source.json").path,
            out=tmp_path / "source.mcap",
        )
        assert source_run.returncode == 0, source_run.stderr

        replay = toy_manifest(duration_ns=50_000_000)
        replay.add_channel("ticks", schema="toy.Counter")
        replay.add_replay(
            "replay", recording=source_run.mcap_path, channels=["ticks"]
        )
        slow_subscriber(replay, capacity=2)

        proc = run_sil(replay.write(tmp_path / "replay.json").path)

        assert proc.returncode == 1
        assert "publisher 'replay'" in proc.stderr
        assert "subscriber 'slow'" in proc.stderr


class TestNativeSubscriptionContract:
    def test_subscribing_twice_reports_the_actual_setup_defect(
        self, run_sil, tmp_path
    ):
        m = toy_manifest(duration_ns=50_000_000)
        m.add_channel("ticks", schema="toy.Counter")
        m.add_native(
            "duplicate",
            library=str(BUILD_DIR / "bench_subscriber.silp"),
            config={
                "input": "ticks",
                "period_ns": 10_000_000,
                "subscribe_twice": True,
            },
            subscribes=[SubscriberRoute("ticks", capacity=2)],
        )

        proc = run_sil(m.write(tmp_path / "m.json").path)

        assert proc.returncode == 2
        assert "participant 'duplicate'" in proc.stderr
        assert "subscribed to Channel 'ticks' more than once" in proc.stderr
        assert "capacity exceeded" not in proc.stderr
