"""Manifest builder seam: pure function from builder calls to canonical JSON.

The manifest is the single execution input: canonical, hashable, archived
with every run. These tests pin the behaviors the reproducibility contract
depends on, not the JSON layout details.
"""

import hashlib
import json

import pytest

from sil.manifest import Manifest, ManifestError

TOY_SCHEMAS = {
    "toy.Counter": {
        "fields": [
            {"name": "seq", "type": "u64"},
            {"name": "value", "type": "i64"},
        ]
    }
}


def make_minimal() -> Manifest:
    m = Manifest(duration_ns=100_000_000)
    m.add_schemas(TOY_SCHEMAS)
    m.add_channel("ticks", schema="toy.Counter")
    m.add_native("producer", library="libtoy_producer.dylib")
    return m


class TestCanonicalOutput:
    def test_same_content_gives_identical_bytes_and_hash(self):
        a = make_minimal()
        b = make_minimal()
        assert a.to_json() == b.to_json()
        assert a.hash() == hashlib.sha256(a.to_json().encode()).hexdigest()
        assert a.hash() == b.hash()

    def test_declaration_order_does_not_change_hash(self):
        a = Manifest(duration_ns=1_000_000)
        a.add_schemas(TOY_SCHEMAS)
        a.add_channel("a", schema="toy.Counter")
        a.add_channel("b", schema="toy.Counter")

        b = Manifest(duration_ns=1_000_000)
        b.add_schemas(TOY_SCHEMAS)
        b.add_channel("b", schema="toy.Counter")
        b.add_channel("a", schema="toy.Counter")

        assert a.hash() == b.hash()

    def test_output_is_valid_json_with_version_marker(self):
        doc = json.loads(make_minimal().to_json())
        assert doc["sil_manifest"] == 1

    def test_write_returns_path_and_hash(self, tmp_path):
        m = make_minimal()
        ref = m.write(tmp_path / "m.json")
        assert ref.path.read_text() == m.to_json()
        assert ref.hash == m.hash()


class TestValidation:
    def test_duplicate_channel_rejected(self):
        m = make_minimal()
        with pytest.raises(ManifestError, match="ticks"):
            m.add_channel("ticks", schema="toy.Counter")

    def test_duplicate_participant_rejected(self):
        m = make_minimal()
        with pytest.raises(ManifestError, match="producer"):
            m.add_native("producer", library="x.dylib")

    def test_channel_with_unknown_schema_rejected(self):
        m = Manifest(duration_ns=1)
        with pytest.raises(ManifestError, match="toy.Missing"):
            m.add_channel("c", schema="toy.Missing")
            m.to_json()

    def test_process_participant_with_unknown_channel_rejected(self):
        m = make_minimal()
        m.add_process(
            "echo",
            command=["python3", "echo.py"],
            step_period_ns=10_000_000,
            subscribes=["nope"],
        )
        with pytest.raises(ManifestError, match="nope"):
            m.to_json()

    def test_nonpositive_duration_rejected(self):
        with pytest.raises(ManifestError, match="duration"):
            Manifest(duration_ns=0)

    def test_nonpositive_step_period_rejected(self):
        m = make_minimal()
        with pytest.raises(ManifestError, match="step_period"):
            m.add_process("p", command=["x"], step_period_ns=0)

    def test_negative_latency_rejected(self):
        m = make_minimal()
        with pytest.raises(ManifestError, match="latency"):
            m.add_channel("c", schema="toy.Counter", latency_ns=-1)


class TestClockShim:
    """The shim flag and realtime epoch are hashed config: a shimmed and an
    unshimmed variant of the same run must never collide, while manifests that
    predate the shim keep byte-identical output."""

    def test_negative_epoch_rejected(self):
        with pytest.raises(ManifestError, match="epoch"):
            Manifest(duration_ns=1_000_000, epoch_ns=-1)

    def test_non_integer_epoch_rejected(self):
        # The kernel loader requires an unsigned integer; the builder must
        # reject non-integers eagerly so the two validators agree.
        with pytest.raises(ManifestError, match="epoch"):
            Manifest(duration_ns=1_000_000, epoch_ns=1.5)

    def test_default_epoch_is_omitted(self):
        with_default = Manifest(duration_ns=1_000_000, epoch_ns=0)
        without = Manifest(duration_ns=1_000_000)
        assert "epoch_ns" not in json.loads(with_default.to_json())
        assert with_default.hash() == without.hash()

    def test_positive_epoch_is_emitted_and_changes_hash(self):
        base = make_minimal()
        with_epoch = Manifest(
            duration_ns=100_000_000, epoch_ns=1_700_000_000_000_000_000
        )
        with_epoch.add_schemas(TOY_SCHEMAS)
        with_epoch.add_channel("ticks", schema="toy.Counter")
        with_epoch.add_native("producer", library="libtoy_producer.dylib")
        doc = json.loads(with_epoch.to_json())
        assert doc["epoch_ns"] == 1_700_000_000_000_000_000
        assert with_epoch.hash() != base.hash()

    def test_shim_flag_emitted_only_when_enabled(self):
        m = make_minimal()
        m.add_process(
            "vecu", command=["x"], step_period_ns=10_000_000, shim=True
        )
        entry = json.loads(m.to_json())["participants"]["vecu"]
        assert entry["shim"] is True

    def test_unshimmed_process_omits_flag_and_keeps_hash(self):
        shimless = make_minimal()
        shimless.add_process("vecu", command=["x"], step_period_ns=10_000_000)
        explicit_off = make_minimal()
        explicit_off.add_process(
            "vecu", command=["x"], step_period_ns=10_000_000, shim=False
        )
        assert "shim" not in json.loads(shimless.to_json())["participants"]["vecu"]
        assert shimless.hash() == explicit_off.hash()


class TestReplayBuilder:
    def _recording(self, tmp_path) -> str:
        rec = tmp_path / "rec.mcap"
        rec.write_bytes(b"\x89MCAP0\r\n")  # bytes are irrelevant to the builder
        return str(rec)

    def test_hash_is_embedded_from_recording_bytes(self, tmp_path):
        rec = self._recording(tmp_path)
        m = make_minimal()
        m.add_replay("rep", recording=rec, channels=["ticks"])
        p = json.loads(m.to_json())["participants"]["rep"]
        assert p["type"] == "replay"
        assert p["recording_hash"] == hashlib.sha256(
            open(rec, "rb").read()
        ).hexdigest()
        assert p["channels"] == ["ticks"]

    def test_replay_declaration_is_canonical(self, tmp_path):
        rec = self._recording(tmp_path)
        a = make_minimal()
        a.add_replay("rep", recording=rec, channels=["ticks"])
        b = make_minimal()
        b.add_replay("rep", recording=rec, channels=["ticks"])
        assert a.hash() == b.hash()

    def test_empty_channel_selection_rejected(self, tmp_path):
        m = make_minimal()
        with pytest.raises(ManifestError, match="empty"):
            m.add_replay("rep", recording=self._recording(tmp_path), channels=[])

    def test_unknown_channel_rejected(self, tmp_path):
        m = make_minimal()
        with pytest.raises(ManifestError, match="nope"):
            m.add_replay(
                "rep", recording=self._recording(tmp_path), channels=["nope"]
            )

    def test_missing_recording_rejected(self, tmp_path):
        m = make_minimal()
        with pytest.raises(ManifestError, match="recording"):
            m.add_replay(
                "rep", recording=str(tmp_path / "gone.mcap"), channels=["ticks"]
            )

    def test_replayed_channel_also_published_live_rejected(self, tmp_path):
        # Open-loop replay must not race a live producer on the same channel.
        # The kernel rejects this at load; catch it earlier in the builder.
        m = make_minimal()
        m.add_replay("rep", recording=self._recording(tmp_path), channels=["ticks"])
        m.add_process(
            "live",
            command=["python3", "echo.py"],
            step_period_ns=10_000_000,
            publishes=["ticks"],
        )
        with pytest.raises(ManifestError, match="ticks"):
            m.to_json()


class TestInterceptorBuilder:
    """Interceptors are declared per channel and carried in the hashed
    manifest. In this slice they are inert (parsed and validated, not yet
    applied), so the tests pin declaration, canonicalization, and the eager
    validation rules the kernel mirrors at load."""

    def test_drop_interceptor_is_recorded_on_channel(self):
        m = make_minimal()
        m.add_interceptor("ticks", kind="drop", start_ns=2_000_000, end_ns=4_000_000)
        chan = json.loads(m.to_json())["channels"]["ticks"]
        assert chan["interceptors"] == [
            {"kind": "drop", "start_ns": 2_000_000, "end_ns": 4_000_000}
        ]

    def test_window_defaults_to_whole_run_when_omitted(self):
        m = make_minimal()
        m.add_interceptor("ticks", kind="drop")
        [entry] = json.loads(m.to_json())["channels"]["ticks"]["interceptors"]
        assert "start_ns" not in entry and "end_ns" not in entry

    def test_absent_interceptors_key_when_none_declared(self):
        chan = json.loads(make_minimal().to_json())["channels"]["ticks"]
        assert "interceptors" not in chan

    def test_multiple_interceptors_keep_declared_order(self):
        m = make_minimal()
        m.add_interceptor("ticks", kind="delay", delay_ns=1_000_000)
        m.add_interceptor("ticks", kind="override", field="value", value=7)
        kinds = [i["kind"] for i in
                 json.loads(m.to_json())["channels"]["ticks"]["interceptors"]]
        assert kinds == ["delay", "override"]

    def test_declaration_is_canonical(self):
        a = make_minimal()
        a.add_interceptor("ticks", kind="delay", delay_ns=5_000_000)
        b = make_minimal()
        b.add_interceptor("ticks", kind="delay", delay_ns=5_000_000)
        assert a.hash() == b.hash()

    def test_never_matching_interceptor_only_changes_hash(self):
        # Inertness at the builder level: adding an interceptor changes the
        # hash (user story 11) but nothing else about the declaration set.
        base = make_minimal()
        faulted = make_minimal()
        faulted.add_interceptor("ticks", kind="drop",
                                start_ns=1, end_ns=2)
        assert base.hash() != faulted.hash()

    # --- validation (mirrors kernel load-time exit-2 rules) ---

    def test_unknown_channel_rejected(self):
        m = make_minimal()
        with pytest.raises(ManifestError, match="nope"):
            m.add_interceptor("nope", kind="drop")

    def test_unknown_kind_rejected(self):
        m = make_minimal()
        with pytest.raises(ManifestError, match="bogus"):
            m.add_interceptor("ticks", kind="bogus")

    def test_inverted_window_rejected(self):
        m = make_minimal()
        with pytest.raises(ManifestError, match="window"):
            m.add_interceptor("ticks", kind="drop", start_ns=4, end_ns=2)

    def test_empty_window_rejected(self):
        m = make_minimal()
        with pytest.raises(ManifestError, match="window"):
            m.add_interceptor("ticks", kind="drop", start_ns=2, end_ns=2)

    def test_negative_window_bound_rejected(self):
        m = make_minimal()
        with pytest.raises(ManifestError, match="start_ns"):
            m.add_interceptor("ticks", kind="drop", start_ns=-1)

    def test_negative_delay_rejected(self):
        m = make_minimal()
        with pytest.raises(ManifestError, match="delay_ns"):
            m.add_interceptor("ticks", kind="delay", delay_ns=-1)

    def test_delay_requires_delay_ns(self):
        m = make_minimal()
        with pytest.raises(ManifestError, match="delay_ns"):
            m.add_interceptor("ticks", kind="delay")

    def test_drop_nth_below_one_rejected(self):
        m = make_minimal()
        with pytest.raises(ManifestError, match="n"):
            m.add_interceptor("ticks", kind="drop_nth", n=0)

    def test_drop_nth_requires_n(self):
        m = make_minimal()
        with pytest.raises(ManifestError, match="n"):
            m.add_interceptor("ticks", kind="drop_nth")

    def test_override_unknown_field_rejected(self):
        m = make_minimal()
        with pytest.raises(ManifestError, match="missing"):
            m.add_interceptor("ticks", kind="override", field="missing", value=1)

    def test_override_requires_field_and_value(self):
        m = make_minimal()
        with pytest.raises(ManifestError, match="field"):
            m.add_interceptor("ticks", kind="override", value=1)

    def test_override_value_out_of_range_rejected(self):
        m = make_minimal()
        # seq is u64; a negative constant is unrepresentable.
        with pytest.raises(ManifestError, match="value"):
            m.add_interceptor("ticks", kind="override", field="seq", value=-1)

    def test_override_value_in_range_accepted(self):
        m = make_minimal()
        # value is i64; -1 fits.
        m.add_interceptor("ticks", kind="override", field="value", value=-1)
        [entry] = json.loads(m.to_json())["channels"]["ticks"]["interceptors"]
        assert entry == {"kind": "override", "field": "value", "value": -1}

    def test_override_float_field_accepts_number(self):
        m = Manifest(duration_ns=1_000_000)
        m.add_schemas({"S": {"fields": [{"name": "x", "type": "f32"}]}})
        m.add_channel("c", schema="S")
        m.add_interceptor("c", kind="override", field="x", value=1.5)
        [entry] = json.loads(m.to_json())["channels"]["c"]["interceptors"]
        assert entry["value"] == 1.5
