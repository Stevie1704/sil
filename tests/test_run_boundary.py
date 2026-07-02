"""External behavior at the run boundary: invoke the runner with a manifest
and artifacts, assert on exit code and MCAP content only."""

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
