"""Replayer participant at the run boundary: a prior run's recording is fed
back as stimulus and observed only through exit codes, stderr, and MCAP bytes.

The exit criterion (PRD #10): re-simulating an unchanged consumer against its
own recorded input reproduces the consumer's recorded output bit-identically.
"""

import hashlib
import sys
from collections import defaultdict

import pytest

from conftest import ROOT
from test_run_boundary import ARRAY_SCHEMAS, ARRAY_TYPES, TYPES, read_mcap, sums
from sil.manifest import Manifest
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


def channel_streams(mcap_path):
    """
    Group recorded messages into per-channel timestamp and payload streams.
    
    Cross-channel serialization order is not represented; message order is preserved within each channel.
    
    Parameters:
        mcap_path: Path to the MCAP recording.
    
    Returns:
        A mapping from channel names to lists of ``(log_time, data)`` pairs.
    """
    _, msgs = read_mcap(mcap_path)
    streams = defaultdict(list)
    for topic, t, data in msgs:
        streams[topic].append((t, data))
    return dict(streams)


def array_source_manifest(participant="array_source.py"):
    """
    Build a manifest for a process that publishes fixed-size array payloads.
    
    Parameters:
    	participant (str): Filename of the participant process to run.
    
    Returns:
    	Manifest: The configured process manifest.
    """
    m = Manifest(duration_ns=30_000_000)
    m.add_schemas(ARRAY_SCHEMAS)
    m.add_channel("payload", schema="big.Payload")
    m.add_process(
        "source",
        command=[sys.executable, str(ROOT / "tests" / "participants" / participant)],
        step_period_ns=10_000_000,
        publishes=["payload"],
    )
    return m


def record_array_source_run(run_sil, tmp_path, *, participant="array_source.py"):
    """
    Record fixed-size array messages for replay transport tests.
    
    Parameters:
        participant (str): Participant script used to publish the array messages.
    
    Returns:
        tuple: The recorded MCAP path and its SHA-256 hash.
    """
    m = array_source_manifest(participant)
    out = tmp_path / "array-record.mcap"
    proc = run_sil(m.write(tmp_path / "array-record.json").path, out=out)
    assert proc.returncode == 0, proc.stderr
    return out, hashlib.sha256(out.read_bytes()).hexdigest()


def record_multi_channel_array_run(run_sil, tmp_path):
    """
    Record same-time messages interleaved across two array channels.
    
    Returns:
    	tuple: The recorded MCAP path and its SHA-256 hash
    """
    m = Manifest(duration_ns=30_000_000)
    m.add_schemas(ARRAY_SCHEMAS)
    m.add_channel("left", schema="big.Payload")
    m.add_channel("right", schema="big.Payload")
    m.add_process(
        "source",
        command=[
            sys.executable,
            str(ROOT / "tests" / "participants" / "multi_channel_burst_source.py"),
        ],
        step_period_ns=10_000_000,
        publishes=["left", "right"],
    )
    out = tmp_path / "multi-record.mcap"
    proc = run_sil(m.write(tmp_path / "multi-record.json").path, out=out)
    assert proc.returncode == 0, proc.stderr
    return out, hashlib.sha256(out.read_bytes()).hexdigest()


def replay_array_manifest(recording, *, transport):
    """Build a replay run whose process input uses the requested transport."""
    m = Manifest(duration_ns=30_000_000)
    m.add_schemas(ARRAY_SCHEMAS)
    m.add_channel("payload", schema="big.Payload", transport=transport)
    m.add_channel("mirror", schema="big.Payload")
    m.add_replay("rep", recording=str(recording), channels=["payload"])
    m.add_process(
        "sink",
        command=[sys.executable, str(ROOT / "tests" / "participants" / "array_echo.py")],
        step_period_ns=10_000_000,
        subscribes=["payload"],
        publishes=["mirror"],
    )
    return m


def replay_multi_channel_array_manifest(recording, *, transport):
    """Build a replay manifest with two input channels and an echo output channel.
    
    Parameters:
    	transport: Transport used for the replayed input channels.
    
    Returns:
    	The configured replay manifest.
    """
    m = Manifest(duration_ns=30_000_000)
    m.add_schemas(ARRAY_SCHEMAS)
    m.add_channel("left", schema="big.Payload", transport=transport)
    m.add_channel("right", schema="big.Payload", transport=transport)
    m.add_channel("mirror", schema="big.Payload")
    m.add_replay("rep", recording=str(recording), channels=["left", "right"])
    m.add_process(
        "sink",
        command=[sys.executable, str(ROOT / "tests" / "participants" / "array_echo.py")],
        step_period_ns=10_000_000,
        subscribes=["left", "right"],
        publishes=["mirror"],
    )
    return m


def channel_messages(mcap_path, channel):
    """Return one channel's timestamp/payload messages in recording order."""
    _, msgs = read_mcap(mcap_path)
    return [(t, data) for name, t, data in msgs if name == channel]


def expected_burst_payload(message_id):
    """The fixed-layout payload burst_array_source/multi_channel_burst_source
    compute for one message id — the same formula both fixtures use."""
    return {
        "id": message_id,
        "blob": bytes((message_id + k) % 256 for k in range(4)),
        "samples": [float(message_id * 10 + k) for k in range(8)],
    }


class TestArraySourceFixtureRecording:
    """The burst/multi-channel array fixtures record exactly what their step
    formula computes; the transport-composition tests below only assert that
    inline and shm agree, not that either matches the source's own math."""

    def test_burst_array_source_records_two_full_payloads_per_step(
        self, run_sil, tmp_path
    ):
        recording, _ = record_array_source_run(
            run_sil, tmp_path, participant="burst_array_source.py"
        )
        messages = channel_messages(recording, "payload")
        assert [t for t, _ in messages] == [
            0, 0, 10_000_000, 10_000_000, 20_000_000, 20_000_000,
        ]
        assert [
            ARRAY_TYPES["big.Payload"].unpack(data) for _, data in messages
        ] == [expected_burst_payload(i) for i in range(6)]

    def test_multi_channel_burst_source_records_expected_payloads_per_channel(
        self, run_sil, tmp_path
    ):
        recording, _ = record_multi_channel_array_run(run_sil, tmp_path)

        def unpacked(channel):
            return [
                (t, ARRAY_TYPES["big.Payload"].unpack(data))
                for t, data in channel_messages(recording, channel)
            ]

        # base = step * 4; left publishes base and base+2, right publishes
        # base+1 and base+3, both at the same timestamp per step.
        assert unpacked("left") == [
            (0, expected_burst_payload(0)),
            (0, expected_burst_payload(2)),
            (10_000_000, expected_burst_payload(4)),
            (10_000_000, expected_burst_payload(6)),
            (20_000_000, expected_burst_payload(8)),
            (20_000_000, expected_burst_payload(10)),
        ]
        assert unpacked("right") == [
            (0, expected_burst_payload(1)),
            (0, expected_burst_payload(3)),
            (10_000_000, expected_burst_payload(5)),
            (10_000_000, expected_burst_payload(7)),
            (20_000_000, expected_burst_payload(9)),
            (20_000_000, expected_burst_payload(11)),
        ]

    def test_replay_can_select_a_single_channel_from_a_multi_channel_recording(
        self, run_sil, tmp_path
    ):
        """A recording with two live channels can still be replayed selectively."""
        recording, _ = record_multi_channel_array_run(run_sil, tmp_path)

        m = Manifest(duration_ns=30_000_000)
        m.add_schemas(ARRAY_SCHEMAS)
        m.add_channel("left", schema="big.Payload")
        m.add_replay("rep", recording=str(recording), channels=["left"])
        proc = run_sil(m.write(tmp_path / "left-only.json").path)
        assert proc.returncode == 0, proc.stderr

        assert channel_messages(proc.mcap_path, "left") == channel_messages(
            recording, "left"
        )
        # 'right' was never declared in this manifest, so nothing is recorded
        # for it even though the source recording carries it.
        assert channel_messages(proc.mcap_path, "right") == []


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

    def test_message_at_exactly_duration_is_dropped(self, run_sil, tmp_path):
        # Boundary is half-open: a message recorded at exactly duration_ns is
        # dropped. Ticks land at 0/10/20/30/40ms; duration is exactly 20ms, so
        # the 20ms tick is at duration and must not be published.
        rec, _ = record_producer_run(run_sil, tmp_path, duration_ns=50_000_000)
        m = replay_manifest(rec, duration_ns=20_000_000)
        proc = run_sil(m.write(tmp_path / "replay.json").path)
        assert proc.returncode == 0, proc.stderr
        assert 20_000_000 not in [t for t, _ in ticks(proc.mcap_path)]

    def test_message_just_below_duration_is_published(self, run_sil, tmp_path):
        # The cut is >= duration, not > duration: a message one nanosecond below
        # duration replays. The 20ms tick sits just under a 20ms+1ns duration.
        rec, _ = record_producer_run(run_sil, tmp_path, duration_ns=50_000_000)
        m = replay_manifest(rec, duration_ns=20_000_001)
        proc = run_sil(m.write(tmp_path / "replay.json").path)
        assert proc.returncode == 0, proc.stderr
        assert [t for t, _ in ticks(proc.mcap_path)] == [0, 10_000_000, 20_000_000]


class TestReplayTransportComposition:
    """Replay keeps its semantics when the destination channel uses an arena."""

    def test_replay_over_shm_matches_inline_delivery_and_is_deterministic(
        self, run_sil, tmp_path
    ):
        """Replay over shm preserves bytes, delivery, and run determinism."""
        recording, _ = record_array_source_run(run_sil, tmp_path)
        inline = replay_array_manifest(recording, transport="inline").write(
            tmp_path / "replay-inline.json"
        ).path
        shm = replay_array_manifest(recording, transport="shm").write(
            tmp_path / "replay-shm.json"
        ).path

        inline_proc = run_sil(inline, out=tmp_path / "inline.mcap")
        shm_a = run_sil(shm, out=tmp_path / "shm-a.mcap")
        shm_b = run_sil(shm, out=tmp_path / "shm-b.mcap")
        assert inline_proc.returncode == 0, inline_proc.stderr
        assert shm_a.returncode == 0, shm_a.stderr
        assert shm_b.returncode == 0, shm_b.stderr

        # The replayed channel is still recorded at its original timestamps and
        # bytes, while the process subscriber sees the same fixed-size payloads
        # through the arena as it does over inline transport.
        assert channel_messages(shm_a.mcap_path, "payload") == channel_messages(
            inline_proc.mcap_path, "payload"
        )
        assert channel_messages(shm_a.mcap_path, "mirror") == channel_messages(
            inline_proc.mcap_path, "mirror"
        )
        assert [
            ARRAY_TYPES["big.Payload"].unpack(data)
            for _, data in channel_messages(shm_a.mcap_path, "mirror")
        ] == [
            {"id": 0, "blob": b"\x00\x01\x02\x03", "samples": [0.0, 1.0, 2.0, 3.0,
                                                        4.0, 5.0, 6.0, 7.0]},
            {"id": 1, "blob": b"\x01\x02\x03\x04", "samples": [10.0, 11.0, 12.0,
                                                         13.0, 14.0, 15.0, 16.0,
                                                         17.0]},
        ]
        assert shm_a.mcap_path.read_bytes() == shm_b.mcap_path.read_bytes()

    def test_replay_over_shm_preserves_equal_timestamp_publish_order(
        self, run_sil, tmp_path
    ):
        """Replay retains the recording-order tie-break for same-time messages."""
        recording, _ = record_array_source_run(
            run_sil, tmp_path, participant="burst_array_source.py"
        )
        inline = replay_array_manifest(recording, transport="inline").write(
            tmp_path / "burst-inline.json"
        ).path
        shm = replay_array_manifest(recording, transport="shm").write(
            tmp_path / "burst-shm.json"
        ).path

        inline_proc = run_sil(inline, out=tmp_path / "burst-inline.mcap")
        shm_proc = run_sil(shm, out=tmp_path / "burst-shm.mcap")
        assert inline_proc.returncode == 0, inline_proc.stderr
        assert shm_proc.returncode == 0, shm_proc.stderr

        # Two messages share each source timestamp. Their file order is the
        # recording's global publish-order tie-break, and the process receives
        # that order even though the first message uses the arena and the rest
        # use the protocol's inline burst fallback.
        assert [t for t, _ in channel_messages(shm_proc.mcap_path, "payload")] == [
            0, 0, 10_000_000, 10_000_000, 20_000_000, 20_000_000,
        ]
        assert [
            ARRAY_TYPES["big.Payload"].unpack(data)["id"]
            for _, data in channel_messages(shm_proc.mcap_path, "mirror")
        ] == [0, 1, 2, 3]
        assert channel_messages(shm_proc.mcap_path, "payload") == channel_messages(
            inline_proc.mcap_path, "payload"
        )
        assert channel_messages(shm_proc.mcap_path, "mirror") == channel_messages(
            inline_proc.mcap_path, "mirror"
        )

    def test_replay_over_shm_preserves_cross_channel_tie_break(
        self, run_sil, tmp_path
    ):
        """Replay retains global order when equal-time messages cross channels."""
        recording, _ = record_multi_channel_array_run(run_sil, tmp_path)
        inline = replay_multi_channel_array_manifest(
            recording, transport="inline"
        ).write(tmp_path / "multi-inline.json").path
        shm = replay_multi_channel_array_manifest(
            recording, transport="shm"
        ).write(tmp_path / "multi-shm.json").path

        inline_proc = run_sil(inline, out=tmp_path / "multi-inline.mcap")
        shm_proc = run_sil(shm, out=tmp_path / "multi-shm.mcap")
        assert inline_proc.returncode == 0, inline_proc.stderr
        assert shm_proc.returncode == 0, shm_proc.stderr

        # The source publishes left/right/left/right at each timestamp. The
        # sink subscribes to both shm channels and must observe that recording
        # order, rather than its declared channel order, in the mirror output.
        _, msgs = read_mcap(shm_proc.mcap_path)
        mirror = [
            (t, ARRAY_TYPES["big.Payload"].unpack(data)["id"])
            for channel, t, data in msgs
            if channel == "mirror"
        ]
        assert mirror == [
            (10_000_000, 0), (10_000_000, 1),
            (10_000_000, 2), (10_000_000, 3),
            (20_000_000, 4), (20_000_000, 5),
            (20_000_000, 6), (20_000_000, 7),
        ]
        for channel in ("left", "right", "mirror"):
            assert channel_messages(shm_proc.mcap_path, channel) == channel_messages(
                inline_proc.mcap_path, channel
            )

    def test_replay_over_shm_of_burst_source_is_deterministic_across_runs(
        self, run_sil, tmp_path
    ):
        """Two shm runs of the same-time burst replay are bit-identical."""
        recording, _ = record_array_source_run(
            run_sil, tmp_path, participant="burst_array_source.py"
        )
        manifest = replay_array_manifest(recording, transport="shm").write(
            tmp_path / "burst-shm-det.json"
        ).path
        a = run_sil(manifest, out=tmp_path / "burst-shm-a.mcap")
        b = run_sil(manifest, out=tmp_path / "burst-shm-b.mcap")
        assert a.returncode == 0, a.stderr
        assert b.returncode == 0, b.stderr
        assert a.mcap_path.read_bytes() == b.mcap_path.read_bytes()

    def test_replay_over_shm_of_multi_channel_source_is_deterministic_across_runs(
        self, run_sil, tmp_path
    ):
        """Two shm runs of the cross-channel replay are bit-identical."""
        recording, _ = record_multi_channel_array_run(run_sil, tmp_path)
        manifest = replay_multi_channel_array_manifest(
            recording, transport="shm"
        ).write(tmp_path / "multi-shm-det.json").path
        a = run_sil(manifest, out=tmp_path / "multi-shm-a.mcap")
        b = run_sil(manifest, out=tmp_path / "multi-shm-b.mcap")
        assert a.returncode == 0, a.stderr
        assert b.returncode == 0, b.stderr
        assert a.mcap_path.read_bytes() == b.mcap_path.read_bytes()


class TestReplayDeterminism:
    def test_replay_run_is_bit_identical_across_two_runs(self, run_sil, tmp_path):
        rec, _ = record_producer_run(run_sil, tmp_path)
        manifest = replay_manifest(rec).write(tmp_path / "replay.json").path
        a = run_sil(manifest, out=tmp_path / "a.mcap")
        b = run_sil(manifest, out=tmp_path / "b.mcap")
        assert a.returncode == 0 and b.returncode == 0
        assert a.mcap_path.read_bytes() == b.mcap_path.read_bytes()

    def test_record_replay_record_preserves_channel_streams(self, run_sil, tmp_path):
        # The headline reproducibility proof (PRD #2, DESIGN #13): record a run,
        # replay its recorded channel back into an equivalent run recording the
        # replay output, and assert the two recordings agree channel by channel.
        # The producer that live-published 'ticks' in run A is swapped for a
        # replayer in run B; the accumulator's 'sums' and the replayed 'ticks'
        # must both reproduce exactly. Cross-channel interleaving inside a slot
        # is not part of the contract (DESIGN #8), so streams — not raw bytes —
        # are the unit of comparison.
        rec, _ = record_producer_run(run_sil, tmp_path)
        recorded = channel_streams(rec)

        m = replay_manifest(rec)
        proc = run_sil(m.write(tmp_path / "replay.json").path,
                       out=tmp_path / "replay.mcap")
        assert proc.returncode == 0, proc.stderr
        assert channel_streams(proc.mcap_path) == recorded

    def test_replay_interleaved_with_live_publisher_is_deterministic(
        self, run_sil, tmp_path
    ):
        # A replayer and a live participant publish in overlapping slots. The
        # replayed message must take a deterministic position in global publish
        # order, so the run is bit-identical under the run-twice CI discipline.
        # 'pyecho' subscribes to the replayed 'ticks' and publishes 'echo' in
        # the same slots the replayer is active.
        rec, _ = record_producer_run(run_sil, tmp_path)
        m = toy_manifest(duration_ns=50_000_000)
        m.add_channel("ticks", schema="toy.Counter")
        m.add_channel("echo", schema="toy.Counter")
        m.add_replay("rep", recording=str(rec), channels=["ticks"])
        m.add_process(
            "pyecho",
            command=[sys.executable,
                     str(ROOT / "tests" / "participants" / "echo.py")],
            step_period_ns=10_000_000,
            subscribes=["ticks"],
            publishes=["echo"],
        )
        manifest = m.write(tmp_path / "interleaved.json").path
        a = run_sil(manifest, out=tmp_path / "a.mcap")
        b = run_sil(manifest, out=tmp_path / "b.mcap")
        assert a.returncode == 0, a.stderr
        assert b.returncode == 0, b.stderr
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
