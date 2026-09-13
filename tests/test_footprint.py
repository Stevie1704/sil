"""Worst-case declared payload memory of a manifest (issue #55's memory gate)."""

import io
import json

import pytest
from conftest import ROOT
from toys import add_producer, toy_manifest

from sil import footprint
from sil.manifest import SubscriberRoute

BENCH_SCHEMAS = json.loads((ROOT / "schemas" / "bench.json").read_text())
CAMERA_BYTES = 8 + 2_764_800


def camera_manifest(*, subscribers: int, capacity: int):
    """A camera channel with `subscribers` bounded native routes on it."""
    m = toy_manifest(duration_ns=10_000_000)
    m.add_schemas(BENCH_SCHEMAS)
    m.add_channel("camera", schema="bench.CameraFrame")
    add_producer(m, "publisher", channel="camera")
    for i in range(subscribers):
        m.add_native(
            f"sub{i}",
            library="unused.silp",
            subscribes=[SubscriberRoute("camera", capacity=capacity)],
            publishes=[],
        )
    return m.to_doc()


class TestRoutes:
    def test_bounded_route_holds_capacity_times_the_payload(self):
        doc = camera_manifest(subscribers=1, capacity=3)

        (route,) = footprint.routes(doc)

        assert route.participant == "sub0"
        assert route.channel == "camera"
        assert route.payload_bytes == CAMERA_BYTES
        assert route.capacity == 3
        assert route.total_bytes == 3 * CAMERA_BYTES

    def test_fan_out_scales_the_declared_total_linearly(self):
        one = footprint.routes(camera_manifest(subscribers=1, capacity=3))
        eight = footprint.routes(camera_manifest(subscribers=8, capacity=3))

        assert sum(r.total_bytes for r in eight) == 8 * one[0].total_bytes

    def test_a_route_object_without_capacity_is_unbounded(self):
        doc = camera_manifest(subscribers=1, capacity=3)
        doc["participants"]["sub0"]["subscribes"] = [{"channel": "camera"}]

        (route,) = footprint.routes(doc)

        assert route.capacity is None
        assert route.total_bytes is None

    def test_a_pre_75_string_entry_is_unbounded(self):
        doc = camera_manifest(subscribers=1, capacity=3)
        doc["participants"]["sub0"]["subscribes"] = ["camera"]

        (route,) = footprint.routes(doc)

        assert route.capacity is None
        assert route.total_bytes is None

    def test_an_unknown_channel_is_a_footprint_error(self):
        doc = camera_manifest(subscribers=1, capacity=3)
        doc["participants"]["sub0"]["subscribes"] = [{"channel": "nope"}]

        with pytest.raises(footprint.FootprintError, match="unknown channel"):
            footprint.routes(doc)

    def test_an_unknown_schema_is_a_footprint_error(self):
        doc = camera_manifest(subscribers=1, capacity=3)
        doc["channels"]["camera"]["schema"] = "bench.Missing"

        with pytest.raises(footprint.FootprintError, match="unknown schema"):
            footprint.routes(doc)


class TestArenas:
    def process_manifest(self, *, slots=None):
        m = toy_manifest(duration_ns=10_000_000)
        m.add_schemas(BENCH_SCHEMAS)
        m.add_channel("camera", schema="bench.CameraFrame",
                      transport="shm", slots=slots)
        m.add_process("consumer", command=["true"], step_period_ns=1_000_000,
                      subscribes=[SubscriberRoute("camera", capacity=1)])
        add_producer(m, "publisher", channel="camera")
        return m.to_doc()

    def test_an_arena_holds_one_payload_per_declared_slot(self):
        (arena,) = footprint.arenas(self.process_manifest(slots=4))

        assert arena.participant == "consumer"
        assert arena.channel == "camera"
        assert arena.slots == 4
        assert arena.total_bytes == 4 * CAMERA_BYTES

    def test_the_builder_default_maps_two_slots(self):
        (arena,) = footprint.arenas(self.process_manifest())

        assert arena.slots == 2

    def test_an_absent_slot_count_is_the_legacy_single_slot(self):
        doc = self.process_manifest(slots=4)
        del doc["channels"]["camera"]["slots"]

        (arena,) = footprint.arenas(doc)

        assert arena.slots == 1

    def test_an_inline_channel_maps_no_arena(self):
        doc = camera_manifest(subscribers=1, capacity=3)

        assert footprint.arenas(doc) == []

    def test_a_native_subscriber_maps_no_arena(self):
        """Native participants stay on the pointer-based C ABI data plane."""
        doc = self.process_manifest(slots=4)
        doc["participants"]["native_sub"] = {
            "type": "native", "library": "x.silp", "config": {},
            "subscribes": [{"channel": "camera", "capacity": 1,
                            "overflow": "fail"}],
            "publishes": [],
        }

        assert [a.participant for a in footprint.arenas(doc)] == ["consumer"]


class TestReport:
    def render(self, doc) -> str:
        out = io.StringIO()
        footprint.report(doc, out)
        return out.getvalue()

    def test_states_the_bounded_total_and_the_arena_total(self):
        text = self.render(camera_manifest(subscribers=8, capacity=3))

        expected = 8 * 3 * CAMERA_BYTES / (1024 * 1024)
        assert f"{expected:.2f} MiB" in text
        assert "bounded route queues" in text
        assert "shared-memory arenas" in text

    def test_names_every_unbounded_route_and_says_the_total_is_a_bound(self):
        doc = camera_manifest(subscribers=2, capacity=3)
        doc["participants"]["sub1"]["subscribes"] = ["camera"]

        text = self.render(doc)

        assert "unbounded" in text
        assert "lower bound" in text
        assert "sub1 <- camera" in text

    def test_a_manifest_with_no_routes_says_so(self):
        m = toy_manifest(duration_ns=10_000_000)
        m.add_schemas(BENCH_SCHEMAS)
        m.add_channel("camera", schema="bench.CameraFrame")
        add_producer(m, "publisher", channel="camera")

        assert "(none declared)" in self.render(m.to_doc())


class TestCli:
    def test_reports_a_written_manifest(self, tmp_path, capsys):
        path = tmp_path / "m.json"
        path.write_text(json.dumps(camera_manifest(subscribers=2, capacity=4)))

        assert footprint.main([str(path)]) == 0
        assert "declared payload total" in capsys.readouterr().out

    def test_a_missing_manifest_is_a_config_error(self, tmp_path, capsys):
        rc = footprint.main([str(tmp_path / "absent.json")])

        assert rc == footprint.EXIT_CONFIG_ERROR
        assert "cannot read manifest" in capsys.readouterr().err

    def test_invalid_json_is_a_config_error(self, tmp_path, capsys):
        path = tmp_path / "m.json"
        path.write_text("{not json")

        rc = footprint.main([str(path)])

        assert rc == footprint.EXIT_CONFIG_ERROR
        assert "not valid JSON" in capsys.readouterr().err

    def test_a_json_scalar_is_a_config_error(self, tmp_path, capsys):
        path = tmp_path / "m.json"
        path.write_text("42")

        rc = footprint.main([str(path)])

        assert rc == footprint.EXIT_CONFIG_ERROR
        assert "not a JSON object" in capsys.readouterr().err
