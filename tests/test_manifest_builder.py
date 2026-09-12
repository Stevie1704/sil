"""Manifest builder seam: pure function from builder calls to canonical JSON.

The manifest is the single execution input: canonical, hashable, archived
with every run. These tests pin the behaviors the reproducibility contract
depends on, not the JSON layout details.
"""

import hashlib
import json

import pytest

from sil.manifest import Manifest, ManifestError, SubscriberRoute

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
    m.add_native("producer", library="libtoy_producer.dylib", publishes=["ticks"])
    return m


def make_scalar_manifest(field_type: str) -> Manifest:
    m = Manifest(duration_ns=1)
    m.add_schemas(
        {"S": {"fields": [{"name": "value", "type": field_type}]}}
    )
    m.add_channel("c", schema="S")
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


class TestSubscriberRoutes:
    def test_builder_emits_bounded_route_with_fail_default(self):
        m = Manifest(duration_ns=1)
        m.add_schemas(TOY_SCHEMAS)
        m.add_channel("ticks", schema="toy.Counter")
        m.add_native(
            "consumer",
            library="consumer.silp",
            subscribes=[SubscriberRoute("ticks", capacity=4)],
        )

        [route] = json.loads(m.to_json())["participants"]["consumer"][
            "subscribes"
        ]
        assert route == {
            "channel": "ticks",
            "capacity": 4,
            "overflow": "fail",
        }

    def test_route_policy_participates_in_hash(self):
        fail = Manifest(duration_ns=1)
        fail.add_schemas(TOY_SCHEMAS)
        fail.add_channel("ticks", schema="toy.Counter")
        fail.add_native(
            "consumer",
            library="consumer.silp",
            subscribes=[SubscriberRoute("ticks", capacity=4)],
        )

        drop = Manifest(duration_ns=1)
        drop.add_schemas(TOY_SCHEMAS)
        drop.add_channel("ticks", schema="toy.Counter")
        drop.add_native(
            "consumer",
            library="consumer.silp",
            subscribes=[
                SubscriberRoute("ticks", capacity=4, overflow="drop_newest")
            ],
        )

        assert fail.hash() != drop.hash()

    def test_route_capacity_participates_in_hash(self):
        def with_capacity(capacity):
            m = Manifest(duration_ns=1)
            m.add_schemas(TOY_SCHEMAS)
            m.add_channel("ticks", schema="toy.Counter")
            m.add_native(
                "consumer",
                library="consumer.silp",
                subscribes=[SubscriberRoute("ticks", capacity=capacity)],
            )
            return m

        assert with_capacity(1).hash() != with_capacity(2).hash()

    def test_blocking_route_explains_sequential_scheduler_constraint(self):
        m = Manifest(duration_ns=1)
        m.add_schemas(TOY_SCHEMAS)
        m.add_channel("ticks", schema="toy.Counter")

        with pytest.raises(
            ManifestError, match="blocking.*sequential scheduler"
        ):
            m.add_native(
                "consumer",
                library="consumer.silp",
                subscribes=[
                    SubscriberRoute("ticks", capacity=1, overflow="blocking")
                ],
            )

    @pytest.mark.parametrize("capacity", [0, -1, True, 1.5, "2"])
    def test_capacity_must_be_a_positive_integer(self, capacity):
        m = Manifest(duration_ns=1)
        m.add_schemas(TOY_SCHEMAS)
        m.add_channel("ticks", schema="toy.Counter")
        with pytest.raises(ManifestError, match="capacity"):
            m.add_process(
                "consumer",
                command=["consumer"],
                step_period_ns=1,
                subscribes=[SubscriberRoute("ticks", capacity=capacity)],
            )

    def test_unbounded_string_route_is_not_available_from_builder(self):
        m = Manifest(duration_ns=1)
        m.add_schemas(TOY_SCHEMAS)
        m.add_channel("ticks", schema="toy.Counter")
        with pytest.raises(ManifestError, match="SubscriberRoute"):
            m.add_native(
                "consumer", library="consumer.silp", subscribes=["ticks"]
            )


class TestValidation:
    def test_duplicate_schema_field_rejected(self):
        m = Manifest(duration_ns=1_000_000)
        with pytest.raises(ManifestError, match="duplicate field name"):
            m.add_schemas(
                {
                    "S": {
                        "fields": [
                            {"name": "value", "type": "u8"},
                            {"name": "value", "type": "u16"},
                        ]
                    }
                }
            )

    def test_missing_field_name_rejected(self):
        m = Manifest(duration_ns=1_000_000)
        with pytest.raises(ManifestError, match="field name must be a non-empty string"):
            m.add_schemas(
                {
                    "S": {
                        "fields": [
                            {"type": "u8"},
                        ]
                    }
                }
            )

    def test_non_string_field_name_rejected(self):
        m = Manifest(duration_ns=1_000_000)
        with pytest.raises(ManifestError, match="field name must be a non-empty string"):
            m.add_schemas(
                {
                    "S": {
                        "fields": [
                            {"name": 123, "type": "u8"},
                        ]
                    }
                }
            )

    def test_empty_string_field_name_rejected(self):
        m = Manifest(duration_ns=1_000_000)
        with pytest.raises(ManifestError, match="field name must be a non-empty string"):
            m.add_schemas(
                {
                    "S": {
                        "fields": [
                            {"name": "", "type": "u8"},
                        ]
                    }
                }
            )

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
            subscribes=[SubscriberRoute("nope", capacity=1)],
        )
        with pytest.raises(ManifestError, match="nope"):
            m.to_json()

    def test_nonpositive_duration_rejected(self):
        with pytest.raises(ManifestError, match="duration"):
            Manifest(duration_ns=0)

    @pytest.mark.parametrize("value", [True, 1.5, "1000"])
    def test_non_integer_duration_rejected(self, value):
        with pytest.raises(ManifestError, match="duration_ns"):
            Manifest(duration_ns=value)

    def test_nonpositive_step_period_rejected(self):
        m = make_minimal()
        with pytest.raises(ManifestError, match="step_period"):
            m.add_process("p", command=["x"], step_period_ns=0)

    def test_negative_latency_rejected(self):
        m = make_minimal()
        with pytest.raises(ManifestError, match="latency"):
            m.add_channel("c", schema="toy.Counter", latency_ns=-1)

    def test_wrong_schema_container_rejected(self):
        m = Manifest(duration_ns=1_000_000)
        with pytest.raises(ManifestError, match="fields.*array"):
            m.add_schemas({"S": {"fields": {"name": "value"}}})

    def test_unknown_schema_key_rejected(self):
        m = Manifest(duration_ns=1_000_000)
        with pytest.raises(ManifestError, match="unknown key"):
            m.add_schemas(
                {"S": {"fields": [{"name": "value", "type": "u8"}], "extra": 1}}
            )

    def test_wrong_process_element_types_rejected(self):
        m = make_minimal()
        with pytest.raises(ManifestError, match=r"command\[0\]"):
            m.add_process("p", command=[7], step_period_ns=1)
        with pytest.raises(ManifestError, match=r"subscribes\[0\]"):
            m.add_process("p2", command=["x"], step_period_ns=1, subscribes=[7])

    def test_process_numeric_constraints_rejected(self):
        m = make_minimal()
        with pytest.raises(ManifestError, match="priority"):
            m.add_process("p", command=["x"], step_period_ns=1, priority=True)
        with pytest.raises(ManifestError, match="priority"):
            m.add_process("p2", command=["x"], step_period_ns=1, priority=2**31)

    def test_native_library_and_config_types_rejected(self):
        m = make_minimal()
        with pytest.raises(ManifestError, match="library"):
            m.add_native("bad", library=42)
        with pytest.raises(ManifestError, match="config"):
            m.add_native("bad_config", library="x", config=[])

    def test_interceptor_integer_fields_reject_fractional_values(self):
        m = make_minimal()
        with pytest.raises(ManifestError, match="start_ns"):
            m.add_interceptor("ticks", kind="drop", start_ns=1.5)
        with pytest.raises(ManifestError, match="value"):
            m.add_interceptor("ticks", kind="override", field="seq", value=1.5)

    def test_f32_override_rejects_out_of_range_value(self):
        m = Manifest(duration_ns=1_000_000)
        m.add_schemas({"S": {"fields": [{"name": "value", "type": "f32"}]}})
        m.add_channel("c", schema="S")
        with pytest.raises(ManifestError, match="representable"):
            m.add_interceptor("c", kind="override", field="value", value=1e39)


class TestTransport:
    """Channel transport is hashed config. Inline is the default and omitted so
    pre-shm manifests keep byte-identical hashes; shm is the only other value."""

    def test_inline_is_default_and_omitted_from_doc(self):
        m = make_minimal()
        doc = json.loads(m.to_json())
        assert "transport" not in doc["channels"]["ticks"]

    def test_default_transport_matches_pre_shm_hash(self):
        # A channel built without the transport arg must hash exactly as one
        # built with the explicit default, so existing manifests are untouched.
        explicit = Manifest(duration_ns=1_000_000)
        explicit.add_schemas(TOY_SCHEMAS)
        explicit.add_channel("c", schema="toy.Counter", transport="inline")

        implicit = Manifest(duration_ns=1_000_000)
        implicit.add_schemas(TOY_SCHEMAS)
        implicit.add_channel("c", schema="toy.Counter")

        assert explicit.hash() == implicit.hash()

    def test_shm_is_emitted_and_changes_hash(self):
        inline = make_minimal()
        shm = Manifest(duration_ns=100_000_000)
        shm.add_schemas(TOY_SCHEMAS)
        shm.add_channel("ticks", schema="toy.Counter", transport="shm")
        shm.add_native(
            "producer", library="libtoy_producer.dylib", publishes=["ticks"]
        )
        channel = json.loads(shm.to_json())["channels"]["ticks"]
        assert channel["transport"] == "shm"
        assert channel["slots"] == 2
        assert shm.hash() != inline.hash()

    def test_explicit_shm_slot_count_is_always_emitted(self):
        m = Manifest(duration_ns=1_000_000)
        m.add_schemas(TOY_SCHEMAS)
        m.add_channel("c", schema="toy.Counter", transport="shm", slots=1)

        assert json.loads(m.to_json())["channels"]["c"]["slots"] == 1

    @pytest.mark.parametrize("slots", [0, -1, 1.5, True, "2"])
    def test_invalid_shm_slot_count_is_rejected(self, slots):
        m = Manifest(duration_ns=1_000_000)
        m.add_schemas(TOY_SCHEMAS)

        with pytest.raises(ManifestError, match="slots"):
            m.add_channel(
                "c", schema="toy.Counter", transport="shm", slots=slots
            )

    def test_slots_are_only_valid_for_shm_channels(self):
        m = Manifest(duration_ns=1_000_000)
        m.add_schemas(TOY_SCHEMAS)

        with pytest.raises(ManifestError, match="slots.*shm"):
            m.add_channel("c", schema="toy.Counter", slots=1)

    def test_unknown_transport_rejected(self):
        m = make_minimal()
        with pytest.raises(ManifestError, match="transport"):
            m.add_channel("c", schema="toy.Counter", transport="rdma")


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
        with_epoch.add_native(
            "producer", library="libtoy_producer.dylib", publishes=["ticks"]
        )
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


class TestSleepPolicy:
    """The always-emit half of the #62 compatibility shape, for #52.

    The builder always states the policy and defaults to `reject`; the kernel
    loader reads an absent field as `immediate`. That asymmetry is deliberate:
    a Manifest written here can never land on the compatibility behavior
    silently, and one written before #52 keeps both its hash and its meaning.
    """

    def _shimmed(self, **kwargs):
        m = make_minimal()
        m.add_process(
            "vecu", command=["x"], step_period_ns=10_000_000, shim=True, **kwargs
        )
        return m

    def test_shimmed_participant_always_states_its_policy(self):
        entry = json.loads(self._shimmed().to_json())["participants"]["vecu"]
        assert entry["sleep"] == "reject"

    def test_immediate_is_expressible(self):
        entry = json.loads(
            self._shimmed(sleep="immediate").to_json()
        )["participants"]["vecu"]
        assert entry["sleep"] == "immediate"

    def test_the_two_policies_hash_differently(self):
        assert self._shimmed(sleep="reject").hash() != self._shimmed(
            sleep="immediate"
        ).hash()

    def test_unknown_policy_rejected(self):
        with pytest.raises(ManifestError, match="sleep must be"):
            self._shimmed(sleep="block")

    def test_policy_without_the_shim_is_rejected(self):
        # The policy only exists inside the shim, so an unshimmed participant
        # cannot declare one.
        m = make_minimal()
        with pytest.raises(ManifestError, match="sleep requires shim"):
            m.add_process(
                "vecu", command=["x"], step_period_ns=10_000_000,
                sleep="immediate",
            )

    def test_unshimmed_process_omits_the_policy_and_keeps_its_hash(self):
        # The default is exempt from the rule above: not asking for the shim is
        # not declaring a policy, so pre-#52 unshimmed manifests are untouched.
        plain = make_minimal()
        plain.add_process("vecu", command=["x"], step_period_ns=10_000_000)
        entry = json.loads(plain.to_json())["participants"]["vecu"]
        assert "sleep" not in entry

    def test_omitting_the_field_is_not_expressible_through_the_builder(self):
        # The compatibility behavior is reachable by an existing document, not
        # by authoring a new one: every shimmed participant the builder emits
        # carries an explicit policy.
        for policy in ("reject", "immediate"):
            entry = json.loads(
                self._shimmed(sleep=policy).to_json()
            )["participants"]["vecu"]
            assert "sleep" in entry


class TestArraySchemas:
    """Fixed-size array fields are declared with `count`. The builder rejects
    malformed declarations eagerly, mirroring the kernel's load-time rules."""

    ARRAY_SCHEMA = {
        "big.Payload": {
            "fields": [
                {"name": "id", "type": "u32"},
                {"name": "samples", "type": "f32", "count": 8},
            ]
        }
    }

    def test_array_schema_accepted_and_canonical(self):
        a = Manifest(duration_ns=1_000_000)
        a.add_schemas(self.ARRAY_SCHEMA)
        b = Manifest(duration_ns=1_000_000)
        b.add_schemas(self.ARRAY_SCHEMA)
        assert a.hash() == b.hash()
        doc = json.loads(a.to_json())["schemas"]["big.Payload"]
        assert doc["fields"][1] == {"name": "samples", "type": "f32", "count": 8}

    def test_zero_count_rejected(self):
        m = Manifest(duration_ns=1_000_000)
        with pytest.raises(ManifestError, match="count"):
            m.add_schemas(
                {"S": {"fields": [{"name": "a", "type": "u8", "count": 0}]}}
            )

    def test_negative_count_rejected(self):
        m = Manifest(duration_ns=1_000_000)
        with pytest.raises(ManifestError, match="count"):
            m.add_schemas(
                {"S": {"fields": [{"name": "a", "type": "u8", "count": -1}]}}
            )

    def test_non_integer_count_rejected(self):
        m = Manifest(duration_ns=1_000_000)
        with pytest.raises(ManifestError, match="count"):
            m.add_schemas(
                {"S": {"fields": [{"name": "a", "type": "u8", "count": 1.5}]}}
            )

    def test_unknown_element_type_rejected(self):
        m = Manifest(duration_ns=1_000_000)
        with pytest.raises(ManifestError, match="unknown type"):
            m.add_schemas(
                {"S": {"fields": [{"name": "a", "type": "vec3", "count": 4}]}}
            )

    def test_override_on_array_field_rejected(self):
        m = Manifest(duration_ns=1_000_000)
        m.add_schemas(self.ARRAY_SCHEMA)
        m.add_channel("c", schema="big.Payload")
        with pytest.raises(ManifestError, match="samples"):
            m.add_interceptor("c", kind="override", field="samples", value=1)

    def test_override_on_scalar_beside_array_still_works(self):
        m = Manifest(duration_ns=1_000_000)
        m.add_schemas(self.ARRAY_SCHEMA)
        m.add_channel("c", schema="big.Payload")
        m.add_interceptor("c", kind="override", field="id", value=42)
        [entry] = json.loads(m.to_json())["channels"]["c"]["interceptors"]
        assert entry["field"] == "id"


class TestReplayBuilder:
    def _recording(self, tmp_path) -> str:
        rec = tmp_path / "rec.mcap"
        rec.write_bytes(b"\x89MCAP0\r\n")  # bytes are irrelevant to the builder
        return str(rec)

    def _replayable(self) -> Manifest:
        """Like make_minimal, but nothing live publishes the replayed channel."""
        m = Manifest(duration_ns=100_000_000)
        m.add_schemas(TOY_SCHEMAS)
        m.add_channel("ticks", schema="toy.Counter")
        m.add_native(
            "consumer",
            library="x.dylib",
            subscribes=[SubscriberRoute("ticks", capacity=1)],
        )
        return m

    def test_hash_is_embedded_from_recording_bytes(self, tmp_path):
        rec = self._recording(tmp_path)
        m = self._replayable()
        m.add_replay("rep", recording=rec, channels=["ticks"])
        p = json.loads(m.to_json())["participants"]["rep"]
        assert p["type"] == "replay"
        assert p["recording_hash"] == hashlib.sha256(
            open(rec, "rb").read()
        ).hexdigest()
        assert p["channels"] == ["ticks"]

    def test_replay_declaration_is_canonical(self, tmp_path):
        rec = self._recording(tmp_path)
        a = self._replayable()
        a.add_replay("rep", recording=rec, channels=["ticks"])
        b = self._replayable()
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

    @pytest.mark.parametrize(
        ("field_type", "accepted", "rejected"),
        [
            ("u8", [0, 1, 254, 255], [-1, 256]),
            ("u16", [0, 1, 65534, 65535], [-1, 65536]),
            ("u32", [0, 1, 2**32 - 2, 2**32 - 1], [-1, 2**32]),
            ("u64", [0, 1, 2**64 - 2, 2**64 - 1], [-1, 2**64]),
            ("i8", [-(2**7), -(2**7) + 1, 0, 1, 2**7 - 2, 2**7 - 1],
             [-(2**7) - 1, 2**7]),
            ("i16", [-(2**15), -(2**15) + 1, 0, 1, 2**15 - 2, 2**15 - 1],
             [-(2**15) - 1, 2**15]),
            ("i32", [-(2**31), -(2**31) + 1, 0, 1, 2**31 - 2, 2**31 - 1],
             [-(2**31) - 1, 2**31]),
            ("i64", [-(2**63), -(2**63) + 1, 0, 1, 2**63 - 2, 2**63 - 1],
             [-(2**63) - 1, 2**63]),
        ],
    )
    def test_integer_override_boundaries_match_schema_range(
        self, field_type, accepted, rejected
    ):
        for value in accepted:
            m = make_scalar_manifest(field_type)
            m.add_interceptor("c", kind="override", field="value", value=value)

        for value in [*rejected, 1.5, True]:
            m = make_scalar_manifest(field_type)
            with pytest.raises(ManifestError, match="override value"):
                m.add_interceptor(
                    "c", kind="override", field="value", value=value
                )

    @pytest.mark.parametrize(
        ("field_type", "accepted", "rejected"),
        [
            ("f32", [0, 1, 1.5, 3.4028234663852886e38],
             [3.4028236e38, float("inf"), float("nan"), True]),
            ("f64", [0, 1, 1.5, 1.7976931348623157e308],
             [10**400, float("inf"), float("nan"), True]),
        ],
    )
    def test_float_override_policy_accepts_only_finite_representable_values(
        self, field_type, accepted, rejected
    ):
        for value in accepted:
            m = make_scalar_manifest(field_type)
            m.add_interceptor("c", kind="override", field="value", value=value)

        for value in rejected:
            m = make_scalar_manifest(field_type)
            with pytest.raises(ManifestError, match="override value"):
                m.add_interceptor(
                    "c", kind="override", field="value", value=value
                )


class TestNativeChannelContract:
    """A Native participant declares its Channel contract in the Manifest the
    same way a Process participant does, so the kernel knows every publisher
    before it loads participant code (issue #49)."""

    def _two_channel(self) -> Manifest:
        m = Manifest(duration_ns=100_000_000)
        m.add_schemas(TOY_SCHEMAS)
        m.add_channel("ticks", schema="toy.Counter")
        m.add_channel("sums", schema="toy.Counter")
        return m

    def test_declarations_are_emitted_in_the_canonical_doc(self):
        m = self._two_channel()
        m.add_native(
            "acc",
            library="x.dylib",
            subscribes=[SubscriberRoute("ticks", capacity=4)],
            publishes=["sums"],
        )
        entry = json.loads(m.to_json())["participants"]["acc"]
        assert entry["subscribes"] == [
            {"channel": "ticks", "capacity": 4, "overflow": "fail"}
        ]
        assert entry["publishes"] == ["sums"]

    def test_absent_declarations_are_emitted_as_empty_lists(self):
        m = self._two_channel()
        m.add_native("silent", library="x.dylib")
        entry = json.loads(m.to_json())["participants"]["silent"]
        assert entry["subscribes"] == []
        assert entry["publishes"] == []

    def test_declarations_are_covered_by_the_hash(self):
        undeclared = self._two_channel()
        undeclared.add_native("acc", library="x.dylib")
        declared = self._two_channel()
        declared.add_native("acc", library="x.dylib", publishes=["sums"])
        assert declared.hash() != undeclared.hash()

    def test_declared_order_is_preserved(self):
        m = self._two_channel()
        m.add_native(
            "acc",
            library="x.dylib",
            subscribes=[
                SubscriberRoute("sums", capacity=2),
                SubscriberRoute("ticks", capacity=3),
            ],
        )
        entry = json.loads(m.to_json())["participants"]["acc"]
        assert [route["channel"] for route in entry["subscribes"]] == [
            "sums",
            "ticks",
        ]

    def test_unknown_channel_rejected(self):
        m = self._two_channel()
        m.add_native(
            "acc",
            library="x.dylib",
            subscribes=[SubscriberRoute("nope", capacity=1)],
        )
        with pytest.raises(ManifestError, match="nope"):
            m.to_json()

    def test_wrong_element_types_rejected(self):
        m = self._two_channel()
        with pytest.raises(ManifestError, match=r"subscribes\[0\]"):
            m.add_native("bad", library="x.dylib", subscribes=[7])
        with pytest.raises(ManifestError, match=r"publishes\[0\]"):
            m.add_native("bad2", library="x.dylib", publishes=[7])

    def test_duplicate_channel_within_one_declaration_rejected(self):
        m = self._two_channel()
        m.add_native(
            "acc",
            library="x.dylib",
            subscribes=[
                SubscriberRoute("ticks", capacity=1),
                SubscriberRoute("ticks", capacity=2),
            ],
        )
        with pytest.raises(ManifestError, match="acc.*subscribes.*ticks"):
            m.to_json()

    def test_duplicate_channel_in_a_process_declaration_rejected(self):
        # One shared validation path: a duplicate is a defect wherever a
        # participant declares Channels, not only on Native declarations.
        m = self._two_channel()
        m.add_process(
            "vecu",
            command=["x"],
            step_period_ns=10_000_000,
            publishes=["sums", "sums"],
        )
        with pytest.raises(ManifestError, match="vecu.*publishes.*sums"):
            m.to_json()

    def test_subscribing_and_publishing_one_channel_is_allowed(self):
        m = self._two_channel()
        m.add_native(
            "loop",
            library="x.dylib",
            subscribes=[SubscriberRoute("ticks", capacity=1)],
            publishes=["ticks"],
        )
        assert json.loads(m.to_json())["participants"]["loop"]["publishes"] == [
            "ticks"
        ]

    def test_replayed_channel_also_published_by_a_native_rejected(self, tmp_path):
        rec = tmp_path / "rec.mcap"
        rec.write_bytes(b"\x89MCAP0\r\n")
        m = self._two_channel()
        m.add_native("prod", library="x.dylib", publishes=["ticks"])
        m.add_replay("rep", recording=str(rec), channels=["ticks"])
        with pytest.raises(ManifestError, match="ticks.*prod|prod.*ticks"):
            m.to_json()

    def test_multiple_live_publishers_stay_allowed(self):
        # Cardinality is decided in #64; declaring Native publishers must not
        # introduce a single-publisher restriction on the way.
        m = self._two_channel()
        m.add_native("prod", library="x.dylib", publishes=["ticks"])
        m.add_process(
            "vecu", command=["x"], step_period_ns=10_000_000, publishes=["ticks"]
        )
        assert json.loads(m.to_json())["participants"]["prod"]["publishes"] == [
            "ticks"
        ]
