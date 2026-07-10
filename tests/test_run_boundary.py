"""External behavior at the run boundary: invoke the runner with a manifest
and artifacts, assert on exit code and MCAP content only."""

import json
import shutil
import subprocess

import pytest
from mcap.reader import make_reader

from toys import TOY_SCHEMAS, accumulator_library, producer_library, toy_manifest

from sil import schema

TYPES = schema.load(TOY_SCHEMAS)


def read_mcap(path):
    """Returns (metadata dict, [(topic, log_time, data), ...])."""
    with open(path, "rb") as f:
        reader = make_reader(f)
        meta = {}
        for m in reader.iter_metadata():
            meta.update(m.metadata)
        msgs = [
            (channel.topic, message.log_time, message.data)
            for _, channel, message in reader.iter_messages()
        ]
    return meta, msgs


class TestManifestRejection:
    def test_invalid_json_is_config_error(self, run_sil, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        proc = run_sil(bad)
        assert proc.returncode == 2
        assert "manifest" in proc.stderr.lower()

    def test_missing_file_is_config_error(self, run_sil, tmp_path):
        proc = run_sil(tmp_path / "does_not_exist.json")
        assert proc.returncode == 2

    def test_wrong_manifest_version_is_config_error(self, run_sil, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text('{"sil_manifest": 99}')
        proc = run_sil(bad)
        assert proc.returncode == 2
        assert "sil_manifest" in proc.stderr


class TestClockShimRejection:
    """Eager load-time validation of the clock-shim manifest surface. Each rule
    is a config error (exit 2) with a diagnostic, distinguishable from a run
    failure (exit 1)."""

    def test_negative_epoch_is_config_error(self, run_sil, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text(
            '{"sil_manifest":1,"duration_ns":1000,"epoch_ns":-1,'
            '"schemas":{},"channels":{},"participants":{}}'
        )
        proc = run_sil(bad)
        assert proc.returncode == 2
        assert "epoch_ns" in proc.stderr

    def test_shim_on_native_participant_is_config_error(self, run_sil, tmp_path):
        # Unrepresentable via the Python builder, but a hand-written manifest
        # can still express it; the kernel must reject it.
        bad = tmp_path / "bad.json"
        bad.write_text(
            '{"sil_manifest":1,"duration_ns":1000,"schemas":{},"channels":{},'
            '"participants":{"n":{"type":"native","library":"x","shim":true}}}'
        )
        proc = run_sil(bad)
        assert proc.returncode == 2
        assert "shim" in proc.stderr

    def test_shim_requested_but_library_missing_is_config_error(
        self, sil_run, tmp_path
    ):
        # The runner resolves the shim relative to its own path, so a copy of
        # sil-run in a bare directory has no shim library beside it.
        bare = tmp_path / "bare"
        bare.mkdir()
        runner = bare / "sil-run"
        shutil.copy2(sil_run, runner)

        m = toy_manifest()
        m.add_process(
            "vecu",
            command=["true"],
            step_period_ns=10_000_000,
            shim=True,
        )
        manifest = tmp_path / "m.json"
        m.write(manifest)

        proc = subprocess.run(
            [str(runner), str(manifest), "-o", str(tmp_path / "out.mcap")],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 2
        assert "shim" in proc.stderr

    def test_shim_with_library_present_is_not_rejected_for_missing_shim(
        self, run_sil, tmp_path
    ):
        # The real sil-run ships the shim beside it, so a shimmed manifest must
        # not fail the shim-not-found check. Spawn-time injection is issue #28,
        # so the run may still fail later — we only assert the shim-missing
        # diagnostic is absent.
        m = toy_manifest()
        m.add_process(
            "vecu",
            command=["true"],
            step_period_ns=10_000_000,
            shim=True,
        )
        manifest = tmp_path / "m.json"
        m.write(manifest)
        proc = run_sil(manifest)
        assert "shim library not found" not in proc.stderr


class TestClockShimRunBoundary:
    """End-to-end shim behavior at the run boundary (issue #28): a process
    participant that reads the interposed POSIX clocks each step, run under a
    shimmed manifest, records readings equal to the step's virtual time
    (monotonic) and the declared epoch plus virtual time (realtime). No
    knowledge of the shared region or preload leaks into the assertions — only
    what a clock read returns and what lands in the recording."""

    EPOCH = 1_700_000_000_000_000_000  # fixed calendar epoch, ns
    PERIOD = 10_000_000
    DURATION = 30_000_000  # steps at t = 0, 10ms, 20ms

    def _manifest(self, *, shim, epoch_ns=0):
        import sys as _sys

        from conftest import ROOT

        m = toy_manifest(duration_ns=self.DURATION, epoch_ns=epoch_ns)
        m.add_channel("readings", schema="toy.Counter")
        m.add_process(
            "vecu",
            command=[_sys.executable,
                     str(ROOT / "tests" / "participants" / "clock_reader.py")],
            step_period_ns=self.PERIOD,
            publishes=["readings"],
            shim=shim,
        )
        return m

    def _readings(self, mcap_path):
        _, msgs = read_mcap(mcap_path)
        return [
            (t, TYPES["toy.Counter"].unpack(data))
            for topic, t, data in msgs
            if topic == "readings"
        ]

    def test_monotonic_reads_return_step_virtual_time(self, run_sil, tmp_path):
        m = self._manifest(shim=True)
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr
        readings = self._readings(proc.mcap_path)
        # Each step's monotonic reading equals that step's virtual time t.
        assert [t for t, _ in readings] == [0, self.PERIOD, 2 * self.PERIOD]
        assert [r["seq"] for _, r in readings] == [0, self.PERIOD, 2 * self.PERIOD]

    def test_realtime_reads_return_epoch_plus_virtual_time(self, run_sil, tmp_path):
        m = self._manifest(shim=True, epoch_ns=self.EPOCH)
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr
        readings = self._readings(proc.mcap_path)
        assert [r["value"] for _, r in readings] == [
            self.EPOCH + 0,
            self.EPOCH + self.PERIOD,
            self.EPOCH + 2 * self.PERIOD,
        ]

    def test_default_epoch_is_zero(self, run_sil, tmp_path):
        # No epoch declared: realtime reads are just virtual time (epoch 0).
        m = self._manifest(shim=True)
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr
        readings = self._readings(proc.mcap_path)
        assert [r["value"] for _, r in readings] == [0, self.PERIOD, 2 * self.PERIOD]

    def test_unshimmed_participant_sees_wall_clock_not_virtual_time(
        self, run_sil, tmp_path
    ):
        # Control: without the shim, the same participant's monotonic read is
        # real wall-clock time — far larger than the step's virtual time — so
        # the recorded readings do not equal t. This proves the shimmed runs
        # above are the shim's doing, not an accident of the fixture.
        m = self._manifest(shim=False)
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr
        readings = self._readings(proc.mcap_path)
        assert all(r["seq"] > 2 * self.PERIOD for _, r in readings)

    def test_two_shimmed_runs_are_bit_identical(self, run_sil, tmp_path):
        # The determinism contract extends to clock-reading vECUs: two runs of
        # the same shimmed manifest, started at different wall-clock times,
        # produce byte-identical MCAPs (req #6). Start the second run a moment
        # later so any wall-clock leak would diverge the bytes.
        import time as _time

        ref = self._manifest(shim=True, epoch_ns=self.EPOCH).write(
            tmp_path / "m.json"
        )
        a = run_sil(ref.path, out=tmp_path / "a.mcap")
        _time.sleep(0.05)
        b = run_sil(ref.path, out=tmp_path / "b.mcap")
        assert a.returncode == 0, a.stderr
        assert b.returncode == 0, b.stderr
        assert a.mcap_path.read_bytes() == b.mcap_path.read_bytes()


class TestEmptyRun:
    def test_produces_valid_mcap_tied_to_manifest_hash(self, run_sil, tmp_path):
        m = toy_manifest()
        ref = m.write(tmp_path / "m.json")
        proc = run_sil(ref.path)
        assert proc.returncode == 0, proc.stderr
        assert f"manifest_hash {ref.hash}" in proc.stdout
        meta, msgs = read_mcap(proc.mcap_path)
        assert meta["manifest_hash"] == ref.hash
        assert msgs == []


class TestNativeScheduling:
    def test_periodic_producer_records_typed_messages(self, run_sil, tmp_path):
        m = toy_manifest(duration_ns=100_000_000)
        m.add_channel("ticks", schema="toy.Counter")
        m.add_native(
            "producer",
            library=producer_library(),
            config={"channel": "ticks", "period_ns": 10_000_000},
        )
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr

        _, msgs = read_mcap(proc.mcap_path)
        assert [t for _, t, _ in msgs] == [i * 10_000_000 for i in range(10)]
        counters = [TYPES["toy.Counter"].unpack(data) for _, _, data in msgs]
        assert counters[3] == {"seq": 3, "value": 9}

    def test_offset_delays_first_activation(self, run_sil, tmp_path):
        m = toy_manifest(duration_ns=50_000_000)
        m.add_channel("ticks", schema="toy.Counter")
        m.add_native(
            "producer",
            library=producer_library(),
            config={"channel": "ticks", "period_ns": 20_000_000,
                    "offset_ns": 5_000_000},
        )
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr
        _, msgs = read_mcap(proc.mcap_path)
        assert [t for _, t, _ in msgs] == [5_000_000, 25_000_000, 45_000_000]


def pipeline_manifest(producer_name, *, latency_ns=None, consumer_priority=0):
    m = toy_manifest(duration_ns=30_000_000)
    m.add_channel("ticks", schema="toy.Counter", latency_ns=latency_ns)
    m.add_channel("sums", schema="toy.Accum")
    m.add_native(
        producer_name,
        library=producer_library(),
        config={"channel": "ticks", "period_ns": 10_000_000},
    )
    m.add_native(
        "mid",
        library=accumulator_library(),
        config={"input": "ticks", "output": "sums", "period_ns": 10_000_000,
                "priority": consumer_priority},
    )
    return m


def sums(mcap_path):
    _, msgs = read_mcap(mcap_path)
    return [
        (t, TYPES["toy.Accum"].unpack(data))
        for topic, t, data in msgs
        if topic == "sums"
    ]


class TestDeliverySemantics:
    def test_default_latency_is_unit_delay(self, run_sil, tmp_path):
        # Producer publishes at t; consumer sees it at its next activation.
        m = pipeline_manifest("aprod")
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr
        assert sums(proc.mcap_path) == [
            (0, {"count": 0, "sum": 0}),
            (10_000_000, {"count": 1, "sum": 0}),
            (20_000_000, {"count": 2, "sum": 3}),
        ]

    def test_output_independent_of_in_slot_execution_order(self, run_sil, tmp_path):
        # 'aprod' runs before the consumer within a slot, 'zprod' after
        # (name-sorted registration): recorded results must not change.
        a = run_sil(pipeline_manifest("aprod").write(tmp_path / "a.json").path,
                    out=tmp_path / "a.mcap")
        z = run_sil(pipeline_manifest("zprod").write(tmp_path / "z.json").path,
                    out=tmp_path / "z.mcap")
        assert a.returncode == 0 and z.returncode == 0
        assert sums(a.mcap_path) == sums(z.mcap_path)

    def test_declared_zero_latency_is_same_slot_feedthrough(self, run_sil, tmp_path):
        # Explicit latency 0 + consumer ordered after producer: direct
        # feedthrough within the slot.
        m = pipeline_manifest("aprod", latency_ns=0, consumer_priority=10)
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr
        assert sums(proc.mcap_path) == [
            (0, {"count": 1, "sum": 0}),
            (10_000_000, {"count": 2, "sum": 3}),
            (20_000_000, {"count": 3, "sum": 9}),
        ]


class TestProcessParticipant:
    def test_python_step_participant_transforms_messages(self, run_sil, tmp_path):
        import sys as _sys

        from conftest import ROOT

        m = toy_manifest(duration_ns=30_000_000)
        m.add_channel("ticks", schema="toy.Counter")
        m.add_channel("echo", schema="toy.Counter")
        m.add_native(
            "aprod",
            library=producer_library(),
            config={"channel": "ticks", "period_ns": 10_000_000},
        )
        m.add_process(
            "pecho",
            command=[_sys.executable,
                     str(ROOT / "tests" / "participants" / "echo.py")],
            step_period_ns=10_000_000,
            subscribes=["ticks"],
            publishes=["echo"],
        )
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr

        _, msgs = read_mcap(proc.mcap_path)
        echoes = [
            (t, TYPES["toy.Counter"].unpack(data))
            for topic, t, data in msgs
            if topic == "echo"
        ]
        # Default latency: the step at t sees ticks published before t.
        assert echoes == [
            (10_000_000, {"seq": 0, "value": 0}),
            (20_000_000, {"seq": 1, "value": 30}),
        ]


def write_with_raw_interceptor(tmp_path, entry, *, channel="ticks"):
    """Build a valid producer manifest, then splice a raw interceptor entry
    into the named channel and write it canonically. Lets the kernel's
    load-time validation be exercised with declarations the Python builder
    would itself reject."""
    m = toy_manifest(duration_ns=100_000_000)
    m.add_channel(channel, schema="toy.Counter")
    m.add_native(
        "producer",
        library=producer_library(),
        config={"channel": channel, "period_ns": 10_000_000},
    )
    doc = m.to_doc()
    doc["channels"][channel]["interceptors"] = [entry]
    path = tmp_path / "m.json"
    path.write_text(json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n")
    return path


class TestInterceptorRejection:
    """The kernel mirrors the builder's eager checks at load: a malformed
    interceptor is a config error (exit 2), distinguishable from a test
    failure (exit 1), with a diagnostic naming the offending channel."""

    def test_unknown_kind_is_config_error(self, run_sil, tmp_path):
        path = write_with_raw_interceptor(tmp_path, {"kind": "bogus"})
        proc = run_sil(path)
        assert proc.returncode == 2
        assert "ticks" in proc.stderr and "bogus" in proc.stderr

    def test_inverted_window_is_config_error(self, run_sil, tmp_path):
        path = write_with_raw_interceptor(
            tmp_path, {"kind": "drop", "start_ns": 4, "end_ns": 2}
        )
        proc = run_sil(path)
        assert proc.returncode == 2
        assert "ticks" in proc.stderr

    def test_negative_delay_is_config_error(self, run_sil, tmp_path):
        # -1 as a JSON number is not is_number_unsigned in the kernel.
        path = write_with_raw_interceptor(
            tmp_path, {"kind": "delay", "delay_ns": -1}
        )
        proc = run_sil(path)
        assert proc.returncode == 2
        assert "ticks" in proc.stderr

    def test_drop_nth_below_one_is_config_error(self, run_sil, tmp_path):
        path = write_with_raw_interceptor(tmp_path, {"kind": "drop_nth", "n": 0})
        proc = run_sil(path)
        assert proc.returncode == 2
        assert "ticks" in proc.stderr

    def test_override_unknown_field_is_config_error(self, run_sil, tmp_path):
        path = write_with_raw_interceptor(
            tmp_path, {"kind": "override", "field": "missing", "value": 1}
        )
        proc = run_sil(path)
        assert proc.returncode == 2
        assert "ticks" in proc.stderr and "missing" in proc.stderr

    def test_override_out_of_range_is_config_error(self, run_sil, tmp_path):
        # seq is u64; -1 cannot be represented.
        path = write_with_raw_interceptor(
            tmp_path, {"kind": "override", "field": "seq", "value": -1}
        )
        proc = run_sil(path)
        assert proc.returncode == 2
        assert "ticks" in proc.stderr


class TestInterceptorInertness:
    """Inertness proof (req #19): a well-formed interceptor whose window matches
    no published message is observationally inert. The same pipeline run twice —
    plain, then with the never-matching fault — yields bit-identical recorded
    channel streams; the recordings differ *only* in the embedded manifest hash.
    An interceptor is a pure function of what it matches, so declaring one that
    matches nothing costs only the hash change the reproducibility contract
    requires (a different manifest is a different run)."""

    # A window entirely past the run's end can never match a published message.
    # Both a shifting (delay) and a mutating (override) fault must be inert when
    # they match nothing.
    NEVER_MATCHING = {
        "delay": dict(
            kind="delay",
            start_ns=200_000_000, end_ns=300_000_000, delay_ns=7_000_000,
        ),
        "override": dict(
            kind="override",
            start_ns=200_000_000, end_ns=300_000_000, field="value", value=999,
        ),
        "drop": dict(
            kind="drop", start_ns=200_000_000, end_ns=300_000_000,
        ),
        "drop_nth": dict(
            kind="drop_nth", start_ns=200_000_000, end_ns=300_000_000, n=2,
        ),
    }

    def _producer(self, fault):
        m = toy_manifest(duration_ns=100_000_000)
        m.add_channel("ticks", schema="toy.Counter")
        m.add_native(
            "producer",
            library=producer_library(),
            config={"channel": "ticks", "period_ns": 10_000_000},
        )
        if fault is not None:
            m.add_interceptor("ticks", **fault)
        return m

    @pytest.mark.parametrize("kind", list(NEVER_MATCHING))
    def test_never_matching_fault_differs_only_by_manifest_hash(
        self, run_sil, tmp_path, kind
    ):
        base_ref = self._producer(None).write(tmp_path / "base.json")
        faulted_ref = self._producer(self.NEVER_MATCHING[kind]).write(
            tmp_path / "faulted.json"
        )
        # A different manifest is a different run: the hashes must diverge.
        assert base_ref.hash != faulted_ref.hash

        base_proc = run_sil(base_ref.path, out=tmp_path / "base.mcap")
        faulted_proc = run_sil(faulted_ref.path, out=tmp_path / "faulted.mcap")
        assert base_proc.returncode == 0, base_proc.stderr
        assert faulted_proc.returncode == 0, faulted_proc.stderr

        base_meta, base_msgs = read_mcap(base_proc.mcap_path)
        faulted_meta, faulted_msgs = read_mcap(faulted_proc.mcap_path)

        # The recorded channel streams — topics, times, and raw payload bytes —
        # are bit-identical: the never-matching fault is inert.
        assert base_msgs == faulted_msgs

        # The recordings differ *only* in the manifest-hash metadata.
        assert base_meta["manifest_hash"] == base_ref.hash
        assert faulted_meta["manifest_hash"] == faulted_ref.hash
        assert base_meta.pop("manifest_hash") != faulted_meta.pop("manifest_hash")
        assert base_meta == faulted_meta


class TestDelayInterceptor:
    """A `delay` interceptor shifts a message's visibility — and the recorded
    ground truth — later by `delay_ns`, but only for messages published inside
    its virtual-time window. The recording is the post-interceptor stream."""

    def _producer(self, duration_ns=100_000_000):
        m = toy_manifest(duration_ns=duration_ns)
        m.add_channel("ticks", schema="toy.Counter")
        m.add_native(
            "producer",
            library=producer_library(),
            config={"channel": "ticks", "period_ns": 10_000_000},
        )
        return m

    def test_window_scoped_delay_shifts_recorded_time(self, run_sil, tmp_path):
        m = self._producer()
        # Messages published in [25ms, 55ms) — at 30ms, 40ms, 50ms — are shifted
        # by 7ms; everything outside the window is untouched.
        m.add_interceptor(
            "ticks", kind="delay",
            start_ns=25_000_000, end_ns=55_000_000, delay_ns=7_000_000,
        )
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr

        _, msgs = read_mcap(proc.mcap_path)
        times = [t for _, t, _ in msgs]
        assert times == [
            0, 10_000_000, 20_000_000,
            37_000_000, 47_000_000, 57_000_000,
            60_000_000, 70_000_000, 80_000_000, 90_000_000,
        ]
        # Payloads and their seq order are unchanged; only visibility moved.
        counters = [TYPES["toy.Counter"].unpack(d) for _, _, d in msgs]
        assert [c["seq"] for c in counters] == list(range(10))

    def test_delay_past_run_duration_drops_message(self, run_sil, tmp_path):
        m = self._producer()
        # From 85ms to end of run, add 20ms. The message at 90ms would surface
        # at 110ms — past the 100ms duration — so it is dropped entirely, never
        # delivered and never recorded.
        m.add_interceptor(
            "ticks", kind="delay", start_ns=85_000_000, delay_ns=20_000_000,
        )
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr

        _, msgs = read_mcap(proc.mcap_path)
        times = [t for _, t, _ in msgs]
        # 0..80ms recorded as published; the delayed 90ms message is gone.
        assert times == [i * 10_000_000 for i in range(9)]

    def test_delay_landing_exactly_at_duration_drops_message(self, run_sil, tmp_path):
        m = self._producer()
        # 80ms + 20ms == 100ms == duration. The run is the half-open interval
        # [0, duration), so a message surfacing at the boundary is dropped —
        # matching the replayer's duration-truncation rule.
        m.add_interceptor(
            "ticks", kind="delay",
            start_ns=75_000_000, end_ns=85_000_000, delay_ns=20_000_000,
        )
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr

        _, msgs = read_mcap(proc.mcap_path)
        times = [t for _, t, _ in msgs]
        assert 100_000_000 not in times
        # The 80ms message (seq 8) is dropped; 90ms (seq 9) is outside the
        # window and untouched.
        assert times == [
            0, 10_000_000, 20_000_000, 30_000_000, 40_000_000,
            50_000_000, 60_000_000, 70_000_000, 90_000_000,
        ]

    def test_enormous_delay_saturates_and_drops(self, run_sil, tmp_path):
        # A delay near u64 max must not wrap around into the visible range; the
        # sum saturates and every matched message is dropped as past-duration.
        m = self._producer()
        m.add_interceptor(
            "ticks", kind="delay",
            start_ns=45_000_000, delay_ns=18_000_000_000_000_000_000,
        )
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr
        _, msgs = read_mcap(proc.mcap_path)
        # Only the pre-window messages (0..40ms) survive.
        assert [t for _, t, _ in msgs] == [i * 10_000_000 for i in range(5)]

    def test_delay_changes_downstream_consumer_view(self, run_sil, tmp_path):
        # The delay is a routing-layer effect: a subscriber sees the shifted
        # visibility, not just the recording. An accumulator consuming delayed
        # ticks must fold them in later than it otherwise would.
        m = toy_manifest(duration_ns=60_000_000)
        m.add_channel("ticks", schema="toy.Counter")
        m.add_channel("sums", schema="toy.Accum")
        m.add_native(
            "aprod",
            library=producer_library(),
            config={"channel": "ticks", "period_ns": 10_000_000},
        )
        m.add_native(
            "mid",
            library=accumulator_library(),
            config={"input": "ticks", "output": "sums",
                    "period_ns": 10_000_000, "priority": 0},
        )
        # Delay the tick published at 10ms by 15ms → it becomes visible at 25ms,
        # so the consumer at 20ms no longer sees it but the one at 30ms does.
        m.add_interceptor(
            "ticks", kind="delay",
            start_ns=5_000_000, end_ns=15_000_000, delay_ns=15_000_000,
        )
        with_delay = sums(run_sil(m.write(tmp_path / "d.json").path,
                                  out=tmp_path / "d.mcap").mcap_path)

        base = toy_manifest(duration_ns=60_000_000)
        base.add_channel("ticks", schema="toy.Counter")
        base.add_channel("sums", schema="toy.Accum")
        base.add_native("aprod", library=producer_library(),
                        config={"channel": "ticks", "period_ns": 10_000_000})
        base.add_native("mid", library=accumulator_library(),
                        config={"input": "ticks", "output": "sums",
                                "period_ns": 10_000_000, "priority": 0})
        base_sums = sums(run_sil(base.write(tmp_path / "b.json").path,
                                 out=tmp_path / "b.mcap").mcap_path)
        # The delay must change what the consumer folds in — otherwise the
        # interceptor had no runtime effect.
        assert with_delay != base_sums


class TestOverrideInterceptor:
    """An `override` interceptor rewrites a named schema field to a declared
    constant for messages published inside its virtual-time window. The
    overridden value is the recorded ground truth and what subscribers see;
    the field's byte layout is respected and other fields are untouched."""

    def _producer(self, duration_ns=100_000_000):
        m = toy_manifest(duration_ns=duration_ns)
        m.add_channel("ticks", schema="toy.Counter")
        m.add_native(
            "producer",
            library=producer_library(),
            config={"channel": "ticks", "period_ns": 10_000_000},
        )
        return m

    def test_window_scoped_override_rewrites_only_inside_window(
        self, run_sil, tmp_path
    ):
        # Producer emits toy.Counter{seq, value=seq*3}. Override `value` to 777
        # for messages published in [25ms, 55ms) — at 30ms, 40ms, 50ms.
        m = self._producer()
        m.add_interceptor(
            "ticks", kind="override",
            start_ns=25_000_000, end_ns=55_000_000, field="value", value=777,
        )
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr

        _, msgs = read_mcap(proc.mcap_path)
        counters = [TYPES["toy.Counter"].unpack(d) for _, _, d in msgs]
        # seq is never touched; value is 777 only for seq 3,4,5 (published in
        # the window) and the untouched seq*3 elsewhere.
        assert [c["seq"] for c in counters] == list(range(10))
        assert [c["value"] for c in counters] == [
            0, 3, 6, 777, 777, 777, 18, 21, 24, 27,
        ]

    def test_override_changes_downstream_consumer_view(self, run_sil, tmp_path):
        # The override is a routing-layer effect: a subscriber folds in the
        # constant, not the original value. Overriding every tick's `value` to
        # 0 must flatten the accumulator's running sum.
        def build():
            m = toy_manifest(duration_ns=40_000_000)
            m.add_channel("ticks", schema="toy.Counter")
            m.add_channel("sums", schema="toy.Accum")
            m.add_native("aprod", library=producer_library(),
                         config={"channel": "ticks", "period_ns": 10_000_000})
            m.add_native("mid", library=accumulator_library(),
                         config={"input": "ticks", "output": "sums",
                                 "period_ns": 10_000_000, "priority": 0})
            return m

        faulted = build()
        faulted.add_interceptor("ticks", kind="override", field="value", value=0)
        with_override = sums(run_sil(faulted.write(tmp_path / "o.json").path,
                                     out=tmp_path / "o.mcap").mcap_path)
        base = sums(run_sil(build().write(tmp_path / "b.json").path,
                            out=tmp_path / "b.mcap").mcap_path)
        # With every value forced to 0 the running sum never grows past 0.
        assert with_override != base
        assert all(accum["sum"] == 0 for _, accum in with_override)

    def test_override_encodes_negative_constant(self, run_sil, tmp_path):
        # `value` is i64; a negative override must round-trip through the
        # little-endian two's-complement byte encoding.
        m = self._producer(duration_ns=30_000_000)
        m.add_interceptor("ticks", kind="override", field="value", value=-5)
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr
        _, msgs = read_mcap(proc.mcap_path)
        counters = [TYPES["toy.Counter"].unpack(d) for _, _, d in msgs]
        assert all(c["value"] == -5 for c in counters)

    def test_override_composes_with_delay_on_same_channel(self, run_sil, tmp_path):
        # A delay and an override on the same channel both fire at the choke
        # point: the recorded message is shifted in time *and* carries the
        # overridden value.
        m = self._producer(duration_ns=60_000_000)
        m.add_interceptor(
            "ticks", kind="delay",
            start_ns=15_000_000, end_ns=25_000_000, delay_ns=5_000_000,
        )
        m.add_interceptor(
            "ticks", kind="override",
            start_ns=15_000_000, end_ns=25_000_000, field="value", value=999,
        )
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr
        _, msgs = read_mcap(proc.mcap_path)
        # The 20ms tick (seq 2) is shifted to 25ms and its value forced to 999;
        # neighbours are untouched.
        at_25 = [
            (t, TYPES["toy.Counter"].unpack(d))
            for _, t, d in msgs if t == 25_000_000
        ]
        assert at_25 == [(25_000_000, {"seq": 2, "value": 999})]
        # 20ms (the shifted-away slot) carries no message.
        assert 20_000_000 not in [t for _, t, _ in msgs]


class TestDropInterceptor:
    """A `drop` interceptor silences a channel for its virtual-time window: a
    message published inside [start_ns, end_ns) is never recorded and never
    delivered — the first-named fault in the PRD (a sensor channel goes
    silent). Messages outside the window are untouched."""

    def _producer(self, duration_ns=100_000_000):
        m = toy_manifest(duration_ns=duration_ns)
        m.add_channel("ticks", schema="toy.Counter")
        m.add_native(
            "producer",
            library=producer_library(),
            config={"channel": "ticks", "period_ns": 10_000_000},
        )
        return m

    def test_window_scoped_drop_silences_only_inside_window(self, run_sil, tmp_path):
        m = self._producer()
        # Ticks published in [25ms, 55ms) — at 30ms, 40ms, 50ms (seq 3,4,5) —
        # are dropped; every other tick is recorded unchanged.
        m.add_interceptor(
            "ticks", kind="drop", start_ns=25_000_000, end_ns=55_000_000,
        )
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr

        _, msgs = read_mcap(proc.mcap_path)
        counters = [TYPES["toy.Counter"].unpack(d) for _, _, d in msgs]
        times = [t for _, t, _ in msgs]
        assert [c["seq"] for c in counters] == [0, 1, 2, 6, 7, 8, 9]
        assert times == [
            0, 10_000_000, 20_000_000,
            60_000_000, 70_000_000, 80_000_000, 90_000_000,
        ]

    def test_open_ended_drop_silences_to_end_of_run(self, run_sil, tmp_path):
        m = self._producer()
        # No end_ns: the channel goes silent from 45ms onward.
        m.add_interceptor("ticks", kind="drop", start_ns=45_000_000)
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr

        _, msgs = read_mcap(proc.mcap_path)
        # Only 0..40ms survive; 50ms onward is dropped.
        assert [t for _, t, _ in msgs] == [i * 10_000_000 for i in range(5)]

    def test_drop_removes_message_from_downstream_consumer(self, run_sil, tmp_path):
        # The drop is a routing-layer effect: a subscriber never folds in a
        # dropped tick. Dropping every tick starves the accumulator entirely.
        def build():
            m = toy_manifest(duration_ns=40_000_000)
            m.add_channel("ticks", schema="toy.Counter")
            m.add_channel("sums", schema="toy.Accum")
            m.add_native("aprod", library=producer_library(),
                         config={"channel": "ticks", "period_ns": 10_000_000})
            m.add_native("mid", library=accumulator_library(),
                         config={"input": "ticks", "output": "sums",
                                 "period_ns": 10_000_000, "priority": 0})
            return m

        faulted = build()
        faulted.add_interceptor("ticks", kind="drop")
        with_drop = sums(run_sil(faulted.write(tmp_path / "d.json").path,
                                 out=tmp_path / "d.mcap").mcap_path)
        base = sums(run_sil(build().write(tmp_path / "b.json").path,
                            out=tmp_path / "b.mcap").mcap_path)
        # With no tick ever delivered the accumulator never counts or sums.
        assert with_drop != base
        assert all(a["count"] == 0 and a["sum"] == 0 for _, a in with_drop)


class TestDropNthInterceptor:
    """A `drop_nth` interceptor drops every nth message that falls inside its
    virtual-time window, counting from the first in-window message. The window
    index is per-interceptor and independent of channel sequence numbers."""

    def _producer(self, duration_ns=100_000_000):
        m = toy_manifest(duration_ns=duration_ns)
        m.add_channel("ticks", schema="toy.Counter")
        m.add_native(
            "producer",
            library=producer_library(),
            config={"channel": "ticks", "period_ns": 10_000_000},
        )
        return m

    def test_drops_every_nth_message_in_window(self, run_sil, tmp_path):
        m = self._producer()
        # Drop every 2nd tick published in [25ms, 95ms) — the in-window ticks
        # are seq 3..8 (30..80ms). Counting from 1, the 2nd, 4th, 6th are
        # dropped: seq 4 (40ms), seq 6 (60ms), seq 8 (80ms).
        m.add_interceptor(
            "ticks", kind="drop_nth", start_ns=25_000_000, end_ns=95_000_000, n=2,
        )
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr

        _, msgs = read_mcap(proc.mcap_path)
        counters = [TYPES["toy.Counter"].unpack(d) for _, _, d in msgs]
        assert [c["seq"] for c in counters] == [0, 1, 2, 3, 5, 7, 9]

    def test_window_index_resets_relative_to_window_not_channel(
        self, run_sil, tmp_path
    ):
        # The count starts at the first in-window message, not at channel seq 0.
        # Window [45ms, end): in-window ticks are seq 5..9 (50..90ms). Counting
        # from 1, the 3rd in-window tick is dropped: seq 7 (70ms).
        m = self._producer()
        m.add_interceptor("ticks", kind="drop_nth", start_ns=45_000_000, n=3)
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr

        _, msgs = read_mcap(proc.mcap_path)
        counters = [TYPES["toy.Counter"].unpack(d) for _, _, d in msgs]
        assert [c["seq"] for c in counters] == [0, 1, 2, 3, 4, 5, 6, 8, 9]

    def test_drop_nth_one_drops_every_in_window_message(self, run_sil, tmp_path):
        # n=1 drops every message inside the window — equivalent to a plain
        # drop over that window.
        m = self._producer()
        m.add_interceptor(
            "ticks", kind="drop_nth", start_ns=25_000_000, end_ns=55_000_000, n=1,
        )
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr

        _, msgs = read_mcap(proc.mcap_path)
        counters = [TYPES["toy.Counter"].unpack(d) for _, _, d in msgs]
        assert [c["seq"] for c in counters] == [0, 1, 2, 6, 7, 8, 9]


class TestRecordingFormatSeam:
    """The recording format is selected from the output extension at the run
    boundary (req #24). An unrecognized extension is a config error, caught
    before any participant starts; the sole v1 format, .mcap, is unchanged."""

    def _producer_with_marker(self, tmp_path):
        """A manifest whose process participant writes a marker file the moment
        the kernel spawns it, so a missing marker proves no participant ran."""
        import sys as _sys

        from conftest import ROOT

        marker = tmp_path / "spawned.marker"
        m = toy_manifest(duration_ns=30_000_000)
        m.add_channel("ticks", schema="toy.Counter")
        m.add_native(
            "aprod",
            library=producer_library(),
            config={"channel": "ticks", "period_ns": 10_000_000},
        )
        m.add_process(
            "marker",
            command=[_sys.executable,
                     str(ROOT / "tests" / "participants" / "spawn_marker.py"),
                     str(marker)],
            step_period_ns=10_000_000,
        )
        return m, marker

    def test_unknown_extension_is_config_error_and_starts_no_participants(
        self, run_sil, tmp_path
    ):
        m, marker = self._producer_with_marker(tmp_path)
        proc = run_sil(m.write(tmp_path / "m.json").path,
                       out=tmp_path / "out.bogus")
        assert proc.returncode == 2
        assert ".bogus" in proc.stderr
        # The format check precedes engine setup, so the process participant
        # was never spawned.
        assert not marker.exists()

    def test_mcap_output_is_byte_identical(self, run_sil, tmp_path):
        # Naming the .mcap output explicitly must produce exactly the same bytes
        # as the default path: the seam is a pure dispatch, not a re-encode.
        m = toy_manifest(duration_ns=100_000_000)
        m.add_channel("ticks", schema="toy.Counter")
        m.add_native(
            "producer",
            library=producer_library(),
            config={"channel": "ticks", "period_ns": 10_000_000},
        )
        ref = m.write(tmp_path / "m.json").path
        a = run_sil(ref, out=tmp_path / "a.mcap")
        b = run_sil(ref, out=tmp_path / "b.mcap")
        assert a.returncode == 0, a.stderr
        assert b.returncode == 0, b.stderr
        assert a.mcap_path.read_bytes() == b.mcap_path.read_bytes()


class TestRunAbort:
    def test_failing_assertion_aborts_run_with_reason(self, run_sil, tmp_path):
        import sys as _sys

        from conftest import ROOT

        m = toy_manifest(duration_ns=100_000_000)
        m.add_channel("ticks", schema="toy.Counter")
        m.add_native(
            "aprod",
            library=producer_library(),
            config={"channel": "ticks", "period_ns": 10_000_000},
        )
        m.add_process(
            "test",
            command=[_sys.executable,
                     str(ROOT / "tests" / "participants" / "fail_at_20ms.py")],
            step_period_ns=10_000_000,
        )
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 1
        assert "boom at t=20000000" in proc.stderr
        assert "'test'" in proc.stderr

        # The recording is finalized and readable up to the failure.
        _, msgs = read_mcap(proc.mcap_path)
        assert [t for _, t, _ in msgs] == [0, 10_000_000, 20_000_000]
