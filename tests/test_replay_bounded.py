"""Replay memory is bounded by the Recording's largest record, not its length
(issue #180).

A Replayer hashes its Recording and validates every record before any
participant is stepped, then reads the messages a second time while the Run
advances. The instrumented kernel reports the largest read buffer a Replayer
held; that deterministic value establishes the bound. Linux RSS only
corroborates it.
"""

import hashlib
import json
import os
import struct
import sys

import pytest
from mcap.reader import make_reader
from mcap.writer import CompressionType, Writer

from conftest import ROOT, run_manifest
from sil.manifest import Manifest
from test_run_boundary import read_mcap
from toys import TOY_SCHEMAS, add_accumulator, toy_manifest

BLOB_BYTES = 64 * 1024
SCHEMAS = {
    **TOY_SCHEMAS,
    "big.Blob": {"fields": [{"name": "bytes", "type": "u8", "count": BLOB_BYTES}]},
}
MS = 1_000_000
CHUNK_BYTES = 256 * 1024


def canonical(schema_name):
    return json.dumps(SCHEMAS[schema_name], sort_keys=True,
                      separators=(",", ":")).encode()


def write_recording(path, messages, *, chunk_size=CHUNK_BYTES):
    """Write (channel, schema, log_time, payload) messages in the given order."""
    with open(path, "wb") as f:
        writer = Writer(f, compression=CompressionType.NONE, chunk_size=chunk_size)
        writer.start(profile="sil", library="test")
        schema_ids, channel_ids = {}, {}
        for channel, schema, _, _ in messages:
            if schema not in schema_ids:
                schema_ids[schema] = writer.register_schema(
                    schema, "sil_pod", canonical(schema))
            if channel not in channel_ids:
                channel_ids[channel] = writer.register_channel(
                    channel, "sil_pod", schema_ids[schema])
        for channel, _, log_time, payload in messages:
            writer.add_message(channel_ids[channel], log_time=log_time,
                               publish_time=log_time, data=payload)
        writer.finish()
    return path


def tick(seq, value):
    return struct.pack("<Qq", seq, value)


def ticks_at(*times):
    return [("ticks", "toy.Counter", t, tick(i, i + 1)) for i, t in enumerate(times)]


def long_recording(path, *, noise_per_tick):
    """Ten selected ticks, each followed by `noise_per_tick` large payloads on
    an unselected channel, so file length scales with `noise_per_tick` while
    the selected stream stays the same."""
    messages = []
    for i in range(10):
        t = i * 10 * MS
        messages.append(("ticks", "toy.Counter", t, tick(i, i + 1)))
        for n in range(noise_per_tick):
            messages.append(("noise", "big.Blob", t,
                             bytes([(i + n) % 256]) * BLOB_BYTES))
    return write_recording(path, messages)


def largest_record_body(path):
    """Largest record body in the file: every chunk, and the summary records."""
    with open(path, "rb") as f:
        summary = make_reader(f).get_summary()
    # chunk_length counts the opcode byte and the 8-byte length prefix.
    return max(index.chunk_length - 9 for index in summary.chunk_indexes)


def replay_manifest(recording, *, duration_ns=100 * MS, channels=("ticks",)):
    m = Manifest(duration_ns=duration_ns)
    m.add_schemas(SCHEMAS)
    m.add_channel("ticks", schema="toy.Counter")
    m.add_channel("sums", schema="toy.Accum")
    if "noise" in channels:
        m.add_channel("noise", schema="big.Blob")
    m.add_replay("rep", recording=str(recording), channels=list(channels))
    add_accumulator(m, "acc", input_channel="ticks", output_channel="sums",
                    period_ns=10 * MS)
    return m


def replayed_ticks(out):
    return [(t, data) for topic, t, data in read_mcap(out)[1] if topic == "ticks"]


def instrumented_run(runner, manifest, out, report):
    env = dict(os.environ, SIL_COPY_COUNTERS_OUT=str(report))
    proc = run_manifest(runner, manifest, out, env)
    assert proc.returncode == 0, proc.stderr
    return json.loads(report.read_text())


class TestTimestampCharacterization:
    """Recorded timestamps are replayed as stored, never sorted (#180 asks
    for this to be pinned before reader behavior changes)."""

    def test_non_monotone_message_publishes_at_its_own_instant(
        self, run_sil, tmp_path
    ):
        # Stored order 20ms, 10ms, 30ms. The 20ms message goes first; the 10ms
        # message stays next and opens its own, earlier slot after that; then
        # 30ms. Each message keeps its recorded time and payload.
        rec = write_recording(tmp_path / "r.mcap", ticks_at(20 * MS, 10 * MS, 30 * MS))
        proc = run_sil(replay_manifest(rec).write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr
        assert sorted(replayed_ticks(proc.mcap_path)) == [
            (10 * MS, tick(1, 2)), (20 * MS, tick(0, 1)), (30 * MS, tick(2, 3)),
        ]
        # All three reach the consumer: none is dropped for arriving late.
        sums = [data for topic, _, data in read_mcap(proc.mcap_path)[1]
                if topic == "sums"]
        assert sums[-1] == struct.pack("<Qq", 3, 6)

    def test_recording_shifted_past_the_duration_replays_nothing(
        self, run_sil, tmp_path
    ):
        rec = write_recording(tmp_path / "r.mcap",
                              ticks_at(1_000 * MS, 1_010 * MS))
        proc = run_sil(replay_manifest(rec).write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr
        assert replayed_ticks(proc.mcap_path) == []


class TestBoundedReplayMemory:
    def test_read_buffer_is_bounded_by_the_largest_record_not_the_file(
        self, sil_run_instrumented, tmp_path
    ):
        short = long_recording(tmp_path / "short.mcap", noise_per_tick=4)
        long = long_recording(tmp_path / "long.mcap", noise_per_tick=32)
        assert long.stat().st_size > 7 * short.stat().st_size

        reports = {}
        for rec in (short, long):
            manifest = replay_manifest(rec).write(tmp_path / f"{rec.stem}.json").path
            reports[rec.stem] = instrumented_run(
                sil_run_instrumented, manifest, tmp_path / f"{rec.stem}.out.mcap",
                tmp_path / f"{rec.stem}.report.json")

        short_high = reports["short"]["deterministic"]["replay_read_buffer"]
        long_high = reports["long"]["deterministic"]["replay_read_buffer"]
        assert long_high["high_water_bytes"] == short_high["high_water_bytes"]
        assert long_high["high_water_bytes"] <= largest_record_body(long)
        assert long_high["high_water_bytes"] < long.stat().st_size // 16

    @pytest.mark.skipif(not sys.platform.startswith("linux"),
                        reason="RSS corroboration is measured on Linux")
    def test_kernel_rss_does_not_grow_with_recording_length(
        self, sil_run_instrumented, tmp_path
    ):
        # Corroboration only: the ceiling is a quarter of the added file bytes,
        # far above allocator noise and far below a retained file (which would
        # add at least all of them).
        short = long_recording(tmp_path / "short.mcap", noise_per_tick=4)
        long = long_recording(tmp_path / "long.mcap", noise_per_tick=128)
        rss = {}
        for rec in (short, long):
            manifest = replay_manifest(rec).write(tmp_path / f"{rec.stem}.json").path
            report = instrumented_run(
                sil_run_instrumented, manifest, tmp_path / f"{rec.stem}.out.mcap",
                tmp_path / f"{rec.stem}.report.json")
            rss[rec.stem] = report["observational"]["kernel_max_rss_bytes"]
        added = long.stat().st_size - short.stat().st_size
        assert rss["long"] - rss["short"] < added // 4

    def test_long_recording_replays_the_selected_channel_exactly(
        self, run_sil, tmp_path
    ):
        # Mostly unselected large payloads: the selected ticks, and nothing
        # else, reach the Run; two Runs repeat their Recording byte for byte.
        rec = long_recording(tmp_path / "r.mcap", noise_per_tick=16)
        manifest = replay_manifest(rec, duration_ns=50 * MS).write(
            tmp_path / "m.json").path
        a = run_sil(manifest, out=tmp_path / "a.mcap")
        b = run_sil(manifest, out=tmp_path / "b.mcap")
        assert a.returncode == 0, a.stderr
        assert b.returncode == 0, b.stderr
        assert a.mcap_path.read_bytes() == b.mcap_path.read_bytes()
        assert replayed_ticks(a.mcap_path) == [
            (i * 10 * MS, tick(i, i + 1)) for i in range(5)
        ]
        assert {topic for topic, _, _ in read_mcap(a.mcap_path)[1]} == {
            "ticks", "sums"}

    def test_selected_large_payloads_replay_exactly(self, run_sil, tmp_path):
        rec = long_recording(tmp_path / "r.mcap", noise_per_tick=3)
        m = replay_manifest(rec, duration_ns=30 * MS, channels=("ticks", "noise"))
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr
        noise = [(t, data) for topic, t, data in read_mcap(proc.mcap_path)[1]
                 if topic == "noise"]
        assert noise == [
            (i * 10 * MS, bytes([(i + n) % 256]) * BLOB_BYTES)
            for i in range(3) for n in range(3)
        ]


def test_equal_time_messages_keep_stored_order_across_chunks(run_sil, tmp_path):
    # Three messages per instant, one small chunk each few messages: the
    # stored order is the tie-break even where a chunk boundary splits a tie.
    times = [i // 3 * MS for i in range(30)]
    rec = write_recording(tmp_path / "r.mcap", ticks_at(*times), chunk_size=100)
    proc = run_sil(replay_manifest(rec).write(tmp_path / "m.json").path)
    assert proc.returncode == 0, proc.stderr
    with open(proc.mcap_path, "rb") as f:
        stored = [(m.log_time, m.data) for _, c, m in
                  make_reader(f).iter_messages(log_time_order=False)
                  if c.topic == "ticks"]
    assert stored == [(t, tick(i, i + 1)) for i, t in enumerate(times)]


class TestReplayStartupValidation:
    """A Recording that cannot be read to its end is rejected before any
    participant is stepped, even when the Manifest hashed it as it is."""

    def recording(self, tmp_path):
        return write_recording(
            tmp_path / "r.mcap",
            ticks_at(*range(0, 50 * MS, MS)), chunk_size=200).read_bytes()

    def run_hashed(self, run_sil, tmp_path, data):
        rec = tmp_path / "damaged.mcap"
        rec.write_bytes(data)
        # The builder hashes the damaged bytes, so only the reader can object.
        return run_sil(replay_manifest(rec).write(tmp_path / "m.json").path)

    @pytest.mark.parametrize("cut", [8, 100, 400])
    def test_truncated_recording_is_config_error(self, run_sil, tmp_path, cut):
        data = self.recording(tmp_path)
        proc = self.run_hashed(run_sil, tmp_path, data[:-cut])
        assert proc.returncode == 2
        assert "'rep'" in proc.stderr
        assert "damaged.mcap" in proc.stderr

    def test_truncated_to_half_is_config_error(self, run_sil, tmp_path):
        data = self.recording(tmp_path)
        proc = self.run_hashed(run_sil, tmp_path, data[: len(data) // 2])
        assert proc.returncode == 2
        assert "damaged.mcap" in proc.stderr

    def test_corrupt_record_is_config_error(self, run_sil, tmp_path):
        data = bytearray(self.recording(tmp_path))
        # A byte in the record framing just before the eleventh tick payload.
        at = data.find(tick(10, 11)) - 30
        data[at] ^= 0xFF
        proc = self.run_hashed(run_sil, tmp_path, bytes(data))
        assert proc.returncode == 2
        assert "damaged.mcap" in proc.stderr

    @pytest.mark.parametrize("damage", ["truncated", "corrupt", "hash-mismatch"])
    def test_rejected_recording_leaves_a_closed_output(
        self, run_sil, tmp_path, damage
    ):
        data = bytearray(self.recording(tmp_path))
        if damage == "truncated":
            data = data[:-100]
        elif damage == "corrupt":
            data[data.find(tick(10, 11)) - 30] ^= 0xFF
        rec = tmp_path / "damaged.mcap"
        rec.write_bytes(bytes(data))
        manifest = replay_manifest(rec).write(tmp_path / "m.json").path
        if damage == "hash-mismatch":
            rec.write_bytes(bytes(data[:-1]) + bytes([data[-1] ^ 0xFF]))
        proc = run_sil(manifest)
        assert proc.returncode == 2
        assert "damaged.mcap" in proc.stderr
        # The Run's own Recording is still a complete, readable file.
        assert read_mcap(proc.mcap_path)[1] == []


def mutator_manifest(recording, mode, *extra):
    m = replay_manifest(recording, duration_ns=100 * MS)
    m.add_process(
        "mutator",
        command=[sys.executable,
                 str(ROOT / "tests" / "participants" / "recording_mutator.py"),
                 mode, str(recording), *map(str, extra)],
        step_period_ns=1_000 * MS,
    )
    return m


class TestRecordingChangedAfterValidation:
    """The Replayer reads the file it validated through the descriptor it
    opened; a change to that file after validation fails the Run."""

    def recording(self, tmp_path):
        # Many small chunks, so the Replayer reads the file again after the
        # mutator's step at 0ms.
        return write_recording(
            tmp_path / "r.mcap",
            ticks_at(*range(0, 100 * MS, MS)), chunk_size=200)

    def test_replacing_the_path_does_not_change_the_replay(self, run_sil, tmp_path):
        rec = self.recording(tmp_path)
        expected = [(t, data) for t, data in replayed_ticks(
            run_sil(replay_manifest(rec).write(tmp_path / "plain.json").path,
                    out=tmp_path / "plain.mcap").mcap_path)]
        other = write_recording(tmp_path / "other.mcap", ticks_at(5 * MS))
        m = mutator_manifest(rec, "replace", other)
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr
        assert replayed_ticks(proc.mcap_path) == expected
        assert len(expected) == 100

    def test_overwriting_the_file_in_place_fails_the_run(self, run_sil, tmp_path):
        rec = self.recording(tmp_path)
        m = mutator_manifest(rec, "overwrite")
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 1
        assert "'rep'" in proc.stderr
        assert "changed" in proc.stderr
        # The failed Run closes its own Recording: it is complete and readable.
        assert replayed_ticks(proc.mcap_path)


def test_builder_hash_matches_file_bytes(tmp_path):
    rec = write_recording(tmp_path / "r.mcap", ticks_at(0, 10 * MS))
    m = replay_manifest(rec)
    assert m.to_doc()["participants"]["rep"]["recording_hash"] == hashlib.sha256(
        rec.read_bytes()).hexdigest()


def test_builder_hashes_without_holding_the_file(tmp_path):
    import tracemalloc

    rec = tmp_path / "large.mcap"
    with open(rec, "wb") as f:
        f.truncate(64 * 1024 * 1024)
    m = toy_manifest()
    m.add_channel("ticks", schema="toy.Counter")
    tracemalloc.start()
    try:
        m.add_replay("rep", recording=str(rec), channels=["ticks"])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 4 * 1024 * 1024
