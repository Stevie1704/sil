"""Replayer participant at the run boundary: a prior run's recording is fed
back as stimulus and observed only through exit codes, stderr, and MCAP bytes.

The exit criterion (PRD #10): re-simulating an unchanged consumer against its
own recorded input reproduces the consumer's recorded output bit-identically.
"""

import hashlib

import pytest

from test_run_boundary import TYPES, read_mcap, sums
from toys import accumulator_library, producer_library, toy_manifest


def record_producer_run(run_sil, tmp_path, *, duration_ns=50_000_000):
    """Run A: producer -> accumulator, recorded. Returns (mcap_path, hash)."""
    m = toy_manifest(duration_ns=duration_ns)
    m.add_channel("ticks", schema="toy.Counter")
    m.add_channel("sums", schema="toy.Accum")
    m.add_native(
        "aprod",
        library=producer_library(),
        config={"channel": "ticks", "period_ns": 10_000_000},
    )
    m.add_native(
        "acc",
        library=accumulator_library(),
        config={"input": "ticks", "output": "sums", "period_ns": 10_000_000},
    )
    out = tmp_path / "record.mcap"
    proc = run_sil(m.write(tmp_path / "record.json").path, out=out)
    assert proc.returncode == 0, proc.stderr
    return out, hashlib.sha256(out.read_bytes()).hexdigest()


def replay_manifest(recording, *, channels=("ticks",), duration_ns=50_000_000):
    """Run B: replay the producer channel into a fresh accumulator."""
    m = toy_manifest(duration_ns=duration_ns)
    m.add_channel("ticks", schema="toy.Counter")
    m.add_channel("sums", schema="toy.Accum")
    m.add_replay("rep", recording=str(recording), channels=list(channels))
    m.add_native(
        "acc",
        library=accumulator_library(),
        config={"input": "ticks", "output": "sums", "period_ns": 10_000_000},
    )
    return m


def ticks(mcap_path):
    _, msgs = read_mcap(mcap_path)
    return [
        (t, TYPES["toy.Counter"].unpack(data))
        for topic, t, data in msgs
        if topic == "ticks"
    ]


class TestFaithfulReplay:
    def test_replay_reproduces_recorded_consumer_output(self, run_sil, tmp_path):
        # Exit criterion: run B's accumulator output == run A's, bit for bit.
        rec, _ = record_producer_run(run_sil, tmp_path)
        recorded_sums = sums(rec)

        m = replay_manifest(rec)
        proc = run_sil(m.write(tmp_path / "replay.json").path)
        assert proc.returncode == 0, proc.stderr
        assert sums(proc.mcap_path) == recorded_sums

    def test_replayed_channel_is_recorded_in_the_new_run(self, run_sil, tmp_path):
        # Replayed messages are recorded like any other published channel, at
        # their original virtual timestamps and values.
        rec, _ = record_producer_run(run_sil, tmp_path)
        original_ticks = ticks(rec)

        m = replay_manifest(rec)
        proc = run_sil(m.write(tmp_path / "replay.json").path)
        assert proc.returncode == 0, proc.stderr
        assert ticks(proc.mcap_path) == original_ticks


class TestReplaySemantics:
    def test_channel_latency_of_new_manifest_is_applied(self, run_sil, tmp_path):
        # Zero-latency + consumer after the replayer: same-slot feedthrough, so
        # the accumulator sees each replayed tick in its own slot (count leads
        # by one versus the unit-delay default).
        rec, _ = record_producer_run(run_sil, tmp_path)

        m = toy_manifest(duration_ns=30_000_000)
        m.add_channel("ticks", schema="toy.Counter", latency_ns=0)
        m.add_channel("sums", schema="toy.Accum")
        m.add_replay("rep", recording=str(rec), channels=["ticks"])
        m.add_native(
            "acc",
            library=accumulator_library(),
            config={"input": "ticks", "output": "sums",
                    "period_ns": 10_000_000, "priority": 10},
        )
        proc = run_sil(m.write(tmp_path / "replay.json").path)
        assert proc.returncode == 0, proc.stderr
        assert sums(proc.mcap_path) == [
            (0, {"count": 1, "sum": 0}),
            (10_000_000, {"count": 2, "sum": 3}),
            (20_000_000, {"count": 3, "sum": 9}),
        ]

    def test_short_recording_lets_run_reach_full_duration(self, run_sil, tmp_path):
        # Recording spans 30ms; the replay run spans 60ms. The accumulator
        # keeps stepping after the recording ends, holding its final total.
        rec, _ = record_producer_run(run_sil, tmp_path, duration_ns=30_000_000)
        m = replay_manifest(rec, duration_ns=60_000_000)
        proc = run_sil(m.write(tmp_path / "replay.json").path)
        assert proc.returncode == 0, proc.stderr

        result = sums(proc.mcap_path)
        assert result[-1][0] == 50_000_000  # last accumulator step before 60ms
        assert result[-1][1] == {"count": 3, "sum": 9}  # 3 ticks, held after end

    def test_over_long_recording_truncated_at_duration(self, run_sil, tmp_path):
        # Recording spans 50ms; the replay run spans only 20ms. Ticks at or
        # after 20ms must be dropped, not delivered.
        rec, _ = record_producer_run(run_sil, tmp_path, duration_ns=50_000_000)
        m = replay_manifest(rec, duration_ns=20_000_000)
        proc = run_sil(m.write(tmp_path / "replay.json").path)
        assert proc.returncode == 0, proc.stderr
        assert [t for t, _ in ticks(proc.mcap_path)] == [0, 10_000_000]


class TestReplayDeterminism:
    def test_replay_run_is_bit_identical_across_two_runs(self, run_sil, tmp_path):
        rec, _ = record_producer_run(run_sil, tmp_path)
        manifest = replay_manifest(rec).write(tmp_path / "replay.json").path
        a = run_sil(manifest, out=tmp_path / "a.mcap")
        b = run_sil(manifest, out=tmp_path / "b.mcap")
        assert a.returncode == 0 and b.returncode == 0
        assert a.mcap_path.read_bytes() == b.mcap_path.read_bytes()


class TestReplayRejection:
    def test_missing_recording_is_config_error(self, sil_run, run_sil, tmp_path):
        # Reference a recording that never existed: the manifest can still be
        # written by hand, so the kernel must reject it at load.
        manifest = tmp_path / "m.json"
        manifest.write_text(_manifest_json(
            recording=str(tmp_path / "nope.mcap"),
            recording_hash="0" * 64,
        ))
        proc = run_sil(manifest)
        assert proc.returncode == 2
        assert "recording" in proc.stderr.lower()

    def test_hash_mismatch_is_config_error(self, run_sil, tmp_path):
        rec, _ = record_producer_run(run_sil, tmp_path)
        manifest = tmp_path / "m.json"
        manifest.write_text(_manifest_json(
            recording=str(rec), recording_hash="0" * 64,
        ))
        proc = run_sil(manifest)
        assert proc.returncode == 2
        assert "hash" in proc.stderr.lower()

    def test_unknown_selected_channel_is_config_error(self, run_sil, tmp_path):
        rec, h = record_producer_run(run_sil, tmp_path)
        manifest = tmp_path / "m.json"
        manifest.write_text(_manifest_json(
            recording=str(rec), recording_hash=h, channels=["nope"],
        ))
        proc = run_sil(manifest)
        assert proc.returncode == 2

    def test_schema_mismatch_is_config_error(self, run_sil, tmp_path):
        # The recording's 'ticks' is toy.Counter; declare it as toy.Accum in
        # the replay manifest so recorded layout drifts from the manifest.
        rec, h = record_producer_run(run_sil, tmp_path)
        manifest = tmp_path / "m.json"
        manifest.write_text(_manifest_json(
            recording=str(rec), recording_hash=h, ticks_schema="toy.Accum",
        ))
        proc = run_sil(manifest)
        assert proc.returncode == 2
        assert "schema" in proc.stderr.lower()

    def test_live_publisher_collision_is_config_error(self, run_sil, tmp_path):
        # The builder also rejects this collision (see the builder suite); a
        # hand-tampered manifest bypasses that, so prove the *kernel* rejects a
        # replayed channel that a live participant publishes, at load, exit 2.
        import json
        import sys

        from conftest import ROOT

        rec, h = record_producer_run(run_sil, tmp_path)
        manifest = tmp_path / "m.json"
        doc = json.loads(_manifest_json(recording=str(rec), recording_hash=h))
        doc["participants"]["live"] = {
            "type": "process",
            "command": [sys.executable,
                        str(ROOT / "tests" / "participants" / "echo.py")],
            "step_period_ns": 10_000_000,
            "subscribes": [],
            "publishes": ["ticks"],
            "priority": 0,
        }
        manifest.write_text(json.dumps(doc))
        proc = run_sil(manifest)
        assert proc.returncode == 2
        assert "ticks" in proc.stderr


def _manifest_json(*, recording, recording_hash, channels=("ticks",),
                   ticks_schema="toy.Counter"):
    """Hand-built replay manifest, so rejection cases bypass builder validation."""
    import json

    from toys import TOY_SCHEMAS

    m = toy_manifest(duration_ns=30_000_000)
    doc = json.loads(m.to_json())
    doc["schemas"] = TOY_SCHEMAS
    doc["channels"] = {"ticks": {"schema": ticks_schema}}
    doc["participants"] = {
        "rep": {
            "type": "replay",
            "recording": recording,
            "recording_hash": recording_hash,
            "channels": list(channels),
        }
    }
    return json.dumps(doc)
