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
