"""External behavior at the run boundary: invoke the runner with a manifest
and artifacts, assert on exit code and MCAP content only."""

import errno
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from mcap.reader import make_reader

from conftest import COMPAT_ROUTE_CAPACITY, ROOT
from toys import (
    TOY_SCHEMAS,
    accumulator_library,
    add_accumulator,
    add_producer,
    add_thrower,
    producer_library,
    toy_manifest,
)

from sil import schema
from sil.manifest import Manifest, SubscriberRoute

TYPES = schema.load(TOY_SCHEMAS)

U64_MAX = 2**64 - 1


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


def raw_manifest():
    """Return a valid hand-written document for loader rejection tests."""
    return {
        "sil_manifest": 1,
        "duration_ns": 1_000,
        "schemas": {
            "S": {"fields": [{"name": "value", "type": "u8"}]},
        },
        "channels": {"c": {"schema": "S"}},
        "participants": {},
    }


def set_raw_value(document, path, value):
    target = document
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


def write_raw_manifest(tmp_path, document):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")))
    return path


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

    def test_native_library_wrong_type_is_config_error(self, run_sil, tmp_path):
        # Hand-written JSON bypasses the Python builder: the loader must turn
        # a typed extraction failure into a contextual Manifest error.
        bad = tmp_path / "bad.json"
        bad.write_text(
            '{"sil_manifest":1,"duration_ns":1000,"schemas":{},'
            '"channels":{},"participants":{"native":{"type":"native",'
            '"library":42}}}'
        )
        proc = run_sil(bad)
        assert proc.returncode == 2
        assert "participant 'native'" in proc.stderr
        assert "library" in proc.stderr
        assert "number" in proc.stderr

    @pytest.mark.parametrize(
        "missing, present",
        [
            ("subscribes", {"publishes": []}),
            ("publishes", {"subscribes": []}),
        ],
    )
    def test_native_channel_lists_are_required_before_library_load(
        self, run_sil, tmp_path, missing, present
    ):
        document = raw_manifest()
        document["participants"] = {
            "native": {
                "type": "native",
                "library": str(tmp_path / "never-loaded.silp"),
                **present,
            }
        }
        proc = run_sil(write_raw_manifest(tmp_path, document))
        assert proc.returncode == 2
        assert "participant 'native'" in proc.stderr
        assert f"missing required key '{missing}'" in proc.stderr
        assert "never-loaded.silp" not in proc.stderr

    def test_process_channel_lists_remain_optional(self, run_sil, tmp_path):
        marker = tmp_path / "spawned"
        document = raw_manifest()
        document["participants"] = {
            "process": {
                "type": "process",
                "command": [
                    sys.executable,
                    str(ROOT / "tests" / "participants" / "spawn_marker.py"),
                    str(marker),
                ],
                "step_period_ns": 1_000,
            }
        }
        proc = run_sil(write_raw_manifest(tmp_path, document))
        assert proc.returncode == 0, proc.stderr
        assert marker.read_text() == "spawned"

    def test_blocking_subscriber_route_is_rejected_before_process_spawn(
        self, run_sil, tmp_path
    ):
        marker = tmp_path / "spawned"
        document = raw_manifest()
        document["participants"] = {
            "process": {
                "type": "process",
                "command": [
                    sys.executable,
                    str(ROOT / "tests" / "participants" / "spawn_marker.py"),
                    str(marker),
                ],
                "step_period_ns": 1_000,
                "subscribes": [
                    {"channel": "c", "capacity": 1, "overflow": "blocking"}
                ],
            }
        }

        proc = run_sil(write_raw_manifest(tmp_path, document))

        assert proc.returncode == 2
        assert "blocking" in proc.stderr
        assert "sequential scheduler" in proc.stderr
        assert not marker.exists()

    @pytest.mark.parametrize(
        "route,needle",
        [
            ({"channel": "c", "capacity": 0, "overflow": "fail"}, "capacity"),
            ({"channel": "c", "capacity": True, "overflow": "fail"}, "capacity"),
            ({"channel": "c", "overflow": "fail"}, "both be present"),
            ({"channel": "c", "capacity": 1}, "both be present"),
            (
                {"channel": "c", "capacity": 1, "overflow": "oldest"},
                "overflow",
            ),
            (
                {
                    "channel": "c",
                    "capacity": 1,
                    "overflow": "fail",
                    "extra": True,
                },
                "unknown key",
            ),
            (7, "expected an object"),
        ],
    )
    def test_malformed_subscriber_routes_are_load_errors(
        self, run_sil, tmp_path, route, needle
    ):
        document = raw_manifest()
        document["participants"] = {
            "process": {
                "type": "process",
                "command": ["true"],
                "step_period_ns": 1_000,
                "subscribes": [route],
            }
        }

        proc = run_sil(write_raw_manifest(tmp_path, document))

        assert proc.returncode == 2
        assert "participant 'process' key 'subscribes'[0]" in proc.stderr
        assert needle in proc.stderr

    @pytest.mark.parametrize(
        "path,value,needle",
        [
            (("sil_manifest",), True, "sil_manifest"),
            (("duration_ns",), True, "duration_ns"),
            (("duration_ns",), -1, "duration_ns"),
            (("duration_ns",), 1.5, "duration_ns"),
            (("epoch_ns",), -1, "epoch_ns"),
            (("epoch_ns",), 1.5, "epoch_ns"),
            (("schemas",), [], "schemas"),
            (("channels",), [], "channels"),
            (("participants",), [], "participants"),
        ],
    )
    def test_manifest_level_wrong_types_are_config_errors(
        self, run_sil, tmp_path, path, value, needle
    ):
        document = raw_manifest()
        set_raw_value(document, path, value)
        proc = run_sil(write_raw_manifest(tmp_path, document))
        assert proc.returncode == 2
        assert needle in proc.stderr
        assert "expected" in proc.stderr and "got" in proc.stderr

    @pytest.mark.parametrize(
        "path,value",
        [
            (("schemas", "S"), []),
            (("schemas", "S", "fields"), {}),
            (("schemas", "S", "fields", 0), "not an object"),
            (("schemas", "S", "fields", 0, "name"), 7),
            (("schemas", "S", "fields", 0, "type"), 7),
            (("schemas", "S", "fields", 0, "count"), True),
            (("schemas", "S", "fields", 0, "count"), 1.5),
            (("schemas", "S", "fields", 0, "count"), -1),
        ],
    )
    def test_schema_extraction_rejects_wrong_shapes(
        self, run_sil, tmp_path, path, value
    ):
        document = raw_manifest()
        set_raw_value(document, path, value)
        proc = run_sil(write_raw_manifest(tmp_path, document))
        assert proc.returncode == 2
        assert "schema 'S'" in proc.stderr
        assert "expected" in proc.stderr and "got" in proc.stderr

    @pytest.mark.parametrize(
        "path,value",
        [
            (("channels", "c"), []),
            (("channels", "c", "schema"), 7),
            (("channels", "c", "latency_ns"), True),
            (("channels", "c", "latency_ns"), -1),
            (("channels", "c", "latency_ns"), 1.5),
            (("channels", "c", "transport"), 7),
            (("channels", "c", "interceptors"), {}),
        ],
    )
    def test_channel_extraction_rejects_wrong_shapes(
        self, run_sil, tmp_path, path, value
    ):
        document = raw_manifest()
        set_raw_value(document, path, value)
        proc = run_sil(write_raw_manifest(tmp_path, document))
        assert proc.returncode == 2
        assert "channel 'c'" in proc.stderr
        assert "expected" in proc.stderr and "got" in proc.stderr

    @pytest.mark.parametrize("slots", [0, -1, True, 1.5, "2"])
    def test_shm_slots_reject_non_positive_or_unparsable_values(
        self, run_sil, tmp_path, slots
    ):
        document = raw_manifest()
        document["channels"]["c"].update(
            {"transport": "shm", "slots": slots}
        )

        proc = run_sil(write_raw_manifest(tmp_path, document))

        assert proc.returncode == 2
        assert "channel 'c' key 'slots'" in proc.stderr

    def test_slots_on_inline_channel_are_a_manifest_error(
        self, run_sil, tmp_path
    ):
        document = raw_manifest()
        document["channels"]["c"]["slots"] = 2

        proc = run_sil(write_raw_manifest(tmp_path, document))

        assert proc.returncode == 2
        assert "channel 'c'" in proc.stderr
        assert "slots" in proc.stderr and "shm" in proc.stderr

    @pytest.mark.parametrize(
        "path,value",
        [
            (("channels", "c", "interceptors", 0), "not an object"),
            (("channels", "c", "interceptors", 0, "kind"), 7),
            (("channels", "c", "interceptors", 0, "start_ns"), True),
            (("channels", "c", "interceptors", 0, "start_ns"), -1),
            (("channels", "c", "interceptors", 0, "start_ns"), 1.5),
            (("channels", "c", "interceptors", 0, "end_ns"), True),
            (("channels", "c", "interceptors", 0, "end_ns"), -1),
            (("channels", "c", "interceptors", 0, "end_ns"), 1.5),
            (("channels", "c", "interceptors", 0, "delay_ns"), -1),
            (("channels", "c", "interceptors", 0, "delay_ns"), 1.5),
            (("channels", "c", "interceptors", 0, "n"), True),
            (("channels", "c", "interceptors", 0, "n"), 0),
            (("channels", "c", "interceptors", 0, "field"), 7),
            (("channels", "c", "interceptors", 0, "value"), True),
            (("channels", "c", "interceptors", 0, "value"), 1.5),
            (("channels", "c", "interceptors", 0, "value"), 256),
        ],
    )
    def test_interceptor_extraction_rejects_wrong_shapes(
        self, run_sil, tmp_path, path, value
    ):
        document = raw_manifest()
        document["channels"]["c"]["interceptors"] = [{"kind": "drop"}]
        if path[-1] == "delay_ns":
            document["channels"]["c"]["interceptors"][0] = {
                "kind": "delay",
                "delay_ns": 1,
            }
        elif path[-1] == "n":
            document["channels"]["c"]["interceptors"][0] = {
                "kind": "drop_nth",
                "n": 1,
            }
        elif path[-1] in {"field", "value"}:
            document["channels"]["c"]["interceptors"][0] = {
                "kind": "override",
                "field": "value",
                "value": 1,
            }
        set_raw_value(document, path, value)
        proc = run_sil(write_raw_manifest(tmp_path, document))
        assert proc.returncode == 2
        assert "channel 'c'" in proc.stderr
        assert "expected" in proc.stderr and "got" in proc.stderr

    @pytest.mark.parametrize(
        "path,value",
        [
            (("participants",), []),
            (("participants", "p"), "not an object"),
            (("participants", "p", "type"), 7),
            (("participants", "p", "command"), {}),
            (("participants", "p", "command", 0), 7),
            (("participants", "p", "step_period_ns"), True),
            (("participants", "p", "step_period_ns"), -1),
            (("participants", "p", "step_period_ns"), 1.5),
            (("participants", "p", "subscribes"), {}),
            (("participants", "p", "publishes"), [7]),
            (("participants", "p", "priority"), True),
            (("participants", "p", "priority"), 1.5),
            (("participants", "p", "priority"), 2**31),
            (("participants", "p", "shim"), 1),
        ],
    )
    def test_process_extraction_rejects_wrong_shapes(
        self, run_sil, tmp_path, path, value
    ):
        document = raw_manifest()
        document["participants"] = {
            "p": {
                "type": "process",
                "command": ["true"],
                "step_period_ns": 1,
            }
        }
        set_raw_value(document, path, value)
        proc = run_sil(write_raw_manifest(tmp_path, document))
        assert proc.returncode == 2
        if path == ("participants",):
            assert "participants" in proc.stderr
        else:
            assert "participant 'p'" in proc.stderr
        assert "expected" in proc.stderr and "got" in proc.stderr

    @pytest.mark.parametrize(
        "path,value",
        [
            (("participants", "p", "recording"), 7),
            (("participants", "p", "recording_hash"), 7),
            (("participants", "p", "channels"), {}),
            (("participants", "p", "channels", 0), 7),
        ],
    )
    def test_replay_extraction_rejects_wrong_shapes(
        self, run_sil, tmp_path, path, value
    ):
        document = raw_manifest()
        document["participants"] = {
            "p": {
                "type": "replay",
                "recording": "recording.mcap",
                "recording_hash": "hash",
                "channels": ["c"],
            }
        }
        set_raw_value(document, path, value)
        proc = run_sil(write_raw_manifest(tmp_path, document))
        assert proc.returncode == 2
        assert "participant 'p'" in proc.stderr
        assert "expected" in proc.stderr and "got" in proc.stderr

    @pytest.mark.parametrize(
        "scope",
        ["manifest", "schema", "field", "channel", "interceptor", "participant"],
    )
    def test_unknown_keys_are_rejected_at_closed_manifest_levels(
        self, run_sil, tmp_path, scope
    ):
        document = raw_manifest()
        if scope == "manifest":
            document["unexpected"] = True
        elif scope == "schema":
            document["schemas"]["S"]["unexpected"] = True
        elif scope == "field":
            document["schemas"]["S"]["fields"][0]["unexpected"] = True
        elif scope == "channel":
            document["channels"]["c"]["unexpected"] = True
        elif scope == "interceptor":
            document["channels"]["c"]["interceptors"] = [
                {"kind": "drop", "unexpected": True}
            ]
        else:
            document["participants"] = {
                "p": {"type": "native", "library": "x", "unexpected": True}
            }

        proc = run_sil(write_raw_manifest(tmp_path, document))
        assert proc.returncode == 2
        assert "unknown key 'unexpected'" in proc.stderr

    def test_native_config_is_an_explicit_extension_object(
        self, run_sil, tmp_path
    ):
        document = raw_manifest()
        document["participants"] = {
            "native": {
                "type": "native",
                "library": "x",
                "config": {"vendor_option": {"mode": "opaque"}},
                "subscribes": [],
                "publishes": [],
            }
        }
        proc = run_sil(write_raw_manifest(tmp_path, document))
        assert proc.returncode == 2
        assert "unknown key" not in proc.stderr

    def test_native_config_must_still_be_an_object(self, run_sil, tmp_path):
        document = raw_manifest()
        document["participants"] = {
            "native": {
                "type": "native",
                "library": "x",
                "config": 42,
                "subscribes": [],
                "publishes": [],
            }
        }
        proc = run_sil(write_raw_manifest(tmp_path, document))
        assert proc.returncode == 2
        assert "config" in proc.stderr
        assert "expected" in proc.stderr and "got" in proc.stderr

    def test_f32_override_out_of_range_is_a_config_error(self, run_sil, tmp_path):
        document = raw_manifest()
        document["schemas"]["S"]["fields"][0]["type"] = "f32"
        document["channels"]["c"]["interceptors"] = [
            {"kind": "override", "field": "value", "value": 1e39}
        ]
        proc = run_sil(write_raw_manifest(tmp_path, document))
        assert proc.returncode == 2
        assert "f32" in proc.stderr


class TestClockShimRejection:
    """Eager load-time validation of the clock-shim manifest surface. Each rule
    is a Manifest error (exit 2) with a diagnostic, distinguishable from a run
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

    def test_sleep_on_native_participant_is_config_error(self, run_sil, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text(
            '{"sil_manifest":1,"duration_ns":1000,"schemas":{},"channels":{},'
            '"participants":{"n":{"type":"native","library":"x",'
            '"sleep":"reject"}}}'
        )
        proc = run_sil(bad)
        assert proc.returncode == 2
        assert "sleep" in proc.stderr

    def test_sleep_without_shim_is_config_error(self, run_sil, tmp_path):
        # The policy only exists inside the shim; declaring one without it
        # would promise a rejection that never happens.
        bad = tmp_path / "bad.json"
        bad.write_text(
            '{"sil_manifest":1,"duration_ns":1000,"schemas":{},"channels":{},'
            '"participants":{"p":{"type":"process","command":["x"],'
            '"step_period_ns":1000,"sleep":"reject"}}}'
        )
        proc = run_sil(bad)
        assert proc.returncode == 2
        assert "sleep requires shim" in proc.stderr

    def test_unknown_sleep_policy_is_config_error(self, run_sil, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text(
            '{"sil_manifest":1,"duration_ns":1000,"schemas":{},"channels":{},'
            '"participants":{"p":{"type":"process","command":["x"],'
            '"step_period_ns":1000,"shim":true,"sleep":"block"}}}'
        )
        proc = run_sil(bad)
        assert proc.returncode == 2
        assert "unknown sleep policy" in proc.stderr
        assert "'reject' or 'immediate'" in proc.stderr

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

    def test_sil_check_passes_on_shimmed_manifest(self, sil_run, tmp_path):
        # The determinism proof runs through the real tool, not a hand-rolled
        # bit-compare: sil-check runs the shimmed manifest twice and must report
        # it deterministic unchanged (exit 0). Sleep between builder and check so
        # the two internal runs straddle a wall-clock advance; a virtual-time
        # leak would trip the check's own DETERMINISM VIOLATION path.
        import sys as _sys
        import time as _time

        ref = self._manifest(shim=True, epoch_ns=self.EPOCH).write(
            tmp_path / "m.json"
        )
        _time.sleep(0.05)
        proc = subprocess.run(
            [_sys.executable, "-m", "sil.check", str(ref.path),
             "--runner", str(sil_run)],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.startswith("deterministic: ")


class TestClockShimSleepPolicy:
    """End-to-end sleep policy at the run boundary (issue #52).

    A shimmed participant runs a bounded retry loop around ``nanosleep`` each
    step. Virtual time is frozen inside a step, so the loop can never make
    progress: under ``immediate`` every call succeeds and it runs to its cap,
    and under ``reject`` the first call fails with ENOSYS and it leaves. The
    assertions read only what the participant recorded — nothing about the
    shared region or the preload leaks in.
    """

    PERIOD = 10_000_000
    DURATION = 30_000_000  # steps at t = 0, 10ms, 20ms
    CAP = 200  # mirrors CAP in tests/participants/sleep_retry.py

    def _manifest(self, **kwargs):
        import sys as _sys

        from conftest import ROOT

        m = toy_manifest(duration_ns=self.DURATION)
        m.add_channel("readings", schema="toy.Counter")
        m.add_process(
            "vecu",
            command=[_sys.executable,
                     str(ROOT / "tests" / "participants" / "sleep_retry.py")],
            step_period_ns=self.PERIOD,
            publishes=["readings"],
            shim=True,
            **kwargs,
        )
        return m

    def _readings(self, mcap_path):
        _, msgs = read_mcap(mcap_path)
        return [
            TYPES["toy.Counter"].unpack(data)
            for topic, _, data in msgs
            if topic == "readings"
        ]

    def test_reject_ends_the_retry_loop_on_the_first_call(self, run_sil, tmp_path):
        m = self._manifest(sleep="reject")
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr
        readings = self._readings(proc.mcap_path)
        assert [r["seq"] for r in readings] == [1, 1, 1]
        assert [r["value"] for r in readings] == [errno.ENOSYS] * 3

    def test_builder_default_is_reject(self, run_sil, tmp_path):
        # Same run without naming a policy: a Manifest authored today must not
        # land on the compatibility behavior by accident.
        m = self._manifest()
        assert json.loads(m.to_json())["participants"]["vecu"]["sleep"] == "reject"
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr
        assert [r["seq"] for r in self._readings(proc.mcap_path)] == [1, 1, 1]

    def test_immediate_spins_the_retry_loop_to_its_cap(self, run_sil, tmp_path):
        m = self._manifest(sleep="immediate")
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr
        readings = self._readings(proc.mcap_path)
        assert [r["seq"] for r in readings] == [self.CAP] * 3
        assert [r["value"] for r in readings] == [0, 0, 0]

    def test_absent_sleep_field_keeps_the_pre_issue_behavior(
        self, run_sil, tmp_path
    ):
        # The compatibility half of the always-emit shape (#62): a Manifest
        # written before #52 has no `sleep` key, and must keep running exactly
        # as it did. Built here by removing the key the builder now emits.
        m = self._manifest(sleep="immediate")
        path = m.write(tmp_path / "m.json").path
        doc = json.loads(path.read_text())
        del doc["participants"]["vecu"]["sleep"]
        path.write_text(json.dumps(doc))

        proc = run_sil(path)
        assert proc.returncode == 0, proc.stderr
        assert [r["seq"] for r in self._readings(proc.mcap_path)] == [self.CAP] * 3

    def test_the_policy_does_not_advance_virtual_time(self, run_sil, tmp_path):
        # Neither policy may block or move the clock: the recorded timestamps
        # are the declared step grid under both.
        stamps = {}
        for policy in ("reject", "immediate"):
            m = self._manifest(sleep=policy)
            proc = run_sil(m.write(tmp_path / f"{policy}.json").path)
            assert proc.returncode == 0, proc.stderr
            _, msgs = read_mcap(proc.mcap_path)
            stamps[policy] = [t for topic, t, _ in msgs if topic == "readings"]
        assert stamps["reject"] == [0, self.PERIOD, 2 * self.PERIOD]
        assert stamps["immediate"] == stamps["reject"]

    def test_two_reject_runs_are_bit_identical(self, run_sil, tmp_path):
        m = self._manifest(sleep="reject")
        first = run_sil(m.write(tmp_path / "a.json").path)
        second = run_sil(m.write(tmp_path / "b.json").path)
        assert first.returncode == 0 and second.returncode == 0
        assert first.mcap_path.read_bytes() == second.mcap_path.read_bytes()


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
        add_producer(m, "producer", channel="ticks", period_ns=10_000_000)
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr

        _, msgs = read_mcap(proc.mcap_path)
        assert [t for _, t, _ in msgs] == [i * 10_000_000 for i in range(10)]
        counters = [TYPES["toy.Counter"].unpack(data) for _, _, data in msgs]
        assert counters[3] == {"seq": 3, "value": 9}

    def test_offset_delays_first_activation(self, run_sil, tmp_path):
        m = toy_manifest(duration_ns=50_000_000)
        m.add_channel("ticks", schema="toy.Counter")
        add_producer(
            m,
            "producer",
            channel="ticks",
            period_ns=20_000_000,
            offset_ns=5_000_000,
        )
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr
        _, msgs = read_mcap(proc.mcap_path)
        assert [t for _, t, _ in msgs] == [5_000_000, 25_000_000, 45_000_000]


def counters_on(mcap_path, channel):
    """The (t, toy.Counter) pairs recorded on one Channel, in stored order."""
    _, msgs = read_mcap(mcap_path)
    return [
        (t, TYPES["toy.Counter"].unpack(data))
        for topic, t, data in msgs
        if topic == channel
    ]


class TestOneLibraryTwoParticipants:
    """Two Manifest entries on one shared library, in one Run (issue #88).

    Each entry is its own Participant with its own `config`, so a Participant
    that keeps its state behind the `user` pointer it registers gets one
    independent set of that state per entry. Nothing diagnoses a library that
    keeps the state in a global instead: both Tasks would publish the second
    entry's Channel, and the first entry's declared output would never be
    published. The rule in include/sil/participant.h is worth only as much as
    this fixture.
    """

    def test_two_participants_on_one_library_stay_independent(
        self, run_sil, tmp_path
    ):
        m = toy_manifest(duration_ns=100_000_000)
        m.add_channel("fast", schema="toy.Counter")
        m.add_channel("slow", schema="toy.Counter")
        add_producer(m, "fast_producer", channel="fast", period_ns=10_000_000)
        add_producer(m, "slow_producer", channel="slow", period_ns=20_000_000)
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr

        fast = counters_on(proc.mcap_path, "fast")
        slow = counters_on(proc.mcap_path, "slow")

        # Each Participant publishes its own declared Channel on its own
        # period, both taken from the `config` of its own Manifest entry.
        assert [t for t, _ in fast] == [i * 10_000_000 for i in range(10)]
        assert [t for t, _ in slow] == [i * 20_000_000 for i in range(5)]
        # Each counts only its own Activations: one shared counter would
        # spread a single 0..14 sequence across the two Channels.
        assert [c["seq"] for _, c in fast] == list(range(10))
        assert [c["seq"] for _, c in slow] == list(range(5))
        assert [c["value"] for _, c in fast] == [seq * 3 for seq in range(10)]
        assert [c["value"] for _, c in slow] == [seq * 3 for seq in range(5)]


class TestActivationOverflow:
    """Virtual time is unsigned and only ever advances. A Task whose next
    Activation cannot be represented is complete, rather than wrapping the
    clock back to an earlier instant (issue #50)."""

    def test_period_zero_is_a_config_error(self, run_sil, tmp_path):
        # The registration itself is invalid, so it is rejected during setup,
        # before any Task runs.
        m = toy_manifest(duration_ns=30_000_000)
        m.add_channel("ticks", schema="toy.Counter")
        add_producer(m, "producer", channel="ticks", period_ns=0)
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 2
        assert "period must be positive" in proc.stderr

    def test_hostile_period_activates_once_and_completes(
        self, run_sil, tmp_path
    ):
        # Offset 5 with period UINT64_MAX, through the real C ABI: the next
        # Activation is unrepresentable, so the Task is complete after its
        # first one. Unchecked, the addition wrapped it to 4 and walked
        # virtual time down to 0.
        m = toy_manifest(duration_ns=U64_MAX)
        m.add_channel("ticks", schema="toy.Counter")
        add_producer(
            m, "producer", channel="ticks", period_ns=U64_MAX, offset_ns=5
        )
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr
        _, msgs = read_mcap(proc.mcap_path)
        assert [t for _, t, _ in msgs] == [5]

    def test_offset_and_period_near_max_activate_once(self, run_sil, tmp_path):
        m = toy_manifest(duration_ns=U64_MAX)
        m.add_channel("ticks", schema="toy.Counter")
        add_producer(
            m,
            "producer",
            channel="ticks",
            period_ns=U64_MAX - 1,
            offset_ns=U64_MAX - 1,
        )
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr
        _, msgs = read_mcap(proc.mcap_path)
        assert [t for _, t, _ in msgs] == [U64_MAX - 1]

    def test_activation_at_exactly_the_duration_is_outside_the_run(
        self, run_sil, tmp_path
    ):
        # The run covers the half-open [0, duration).
        m = toy_manifest(duration_ns=30_000_000)
        m.add_channel("ticks", schema="toy.Counter")
        add_producer(m, "producer", channel="ticks", period_ns=15_000_000)
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr
        _, msgs = read_mcap(proc.mcap_path)
        assert [t for _, t, _ in msgs] == [0, 15_000_000]

    def test_offset_at_the_duration_never_activates(self, run_sil, tmp_path):
        m = toy_manifest(duration_ns=30_000_000)
        m.add_channel("ticks", schema="toy.Counter")
        add_producer(
            m,
            "producer",
            channel="ticks",
            period_ns=10_000_000,
            offset_ns=30_000_000,
        )
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr
        _, msgs = read_mcap(proc.mcap_path)
        assert msgs == []

    def test_slots_stay_ordered_when_one_task_completes_early(
        self, run_sil, tmp_path
    ):
        # 'aprod' overflows after 2^63; 'acc' keeps activating past it. Slot
        # selection must stay monotonically non-decreasing across the two.
        m = toy_manifest(duration_ns=U64_MAX)
        m.add_channel("ticks", schema="toy.Counter")
        m.add_channel("sums", schema="toy.Accum")
        add_producer(m, "aprod", channel="ticks", period_ns=2**63)
        add_accumulator(m, "acc", period_ns=2**62)
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr

        _, msgs = read_mcap(proc.mcap_path)
        times = [t for _, t, _ in msgs]
        assert times == sorted(times)
        assert [t for topic, t, _ in msgs if topic == "ticks"] == [0, 2**63]
        assert [t for topic, t, _ in msgs if topic == "sums"] == [
            0,
            2**62,
            2**63,
            3 * 2**62,
        ]


class TestNativeChannelContract:
    """A native participant's Channel contract is declared in the manifest, so
    the kernel knows every publisher before it loads participant code. A call
    the declaration does not cover aborts the run with a diagnostic naming the
    participant, the channel, and the declared direction (issue #49)."""

    def test_subscribing_to_an_undeclared_input_is_a_config_error(
        self, run_sil, tmp_path
    ):
        # The toy reads its input channel from config; the declaration omits
        # it, so the participant is rejected during setup, before any run.
        m = toy_manifest(duration_ns=30_000_000)
        m.add_channel("ticks", schema="toy.Counter")
        m.add_channel("sums", schema="toy.Accum")
        m.add_native(
            "acc",
            library=accumulator_library(),
            config={"input": "ticks", "output": "sums", "period_ns": 10_000_000},
            publishes=["sums"],
        )
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 2
        assert "acc" in proc.stderr
        assert "ticks" in proc.stderr
        assert "input" in proc.stderr

    def test_publishing_an_undeclared_output_aborts_the_run(
        self, run_sil, tmp_path
    ):
        m = toy_manifest(duration_ns=30_000_000)
        m.add_channel("ticks", schema="toy.Counter")
        m.add_native(
            "producer",
            library=producer_library(),
            config={"channel": "ticks", "period_ns": 10_000_000},
        )
        path = m.write(tmp_path / "m.json").path
        first = run_sil(path, out=tmp_path / "a.mcap")
        assert first.returncode == 1
        assert "producer" in first.stderr
        assert "ticks" in first.stderr
        assert "output" in first.stderr
        # The abort is part of the deterministic world: same manifest, same
        # exit code and same diagnostic.
        second = run_sil(path, out=tmp_path / "b.mcap")
        assert (second.returncode, second.stderr) == (
            first.returncode,
            first.stderr,
        )

    def test_declaring_an_unknown_channel_is_a_config_error(
        self, run_sil, tmp_path
    ):
        m = toy_manifest(duration_ns=30_000_000)
        m.add_channel("ticks", schema="toy.Counter")
        m.add_native(
            "producer",
            library=producer_library(),
            config={"channel": "ticks", "period_ns": 10_000_000},
            publishes=["ticks"],
        )
        doc = m.to_doc()
        doc["participants"]["producer"]["publishes"] = ["nope"]
        proc = run_sil(_write_doc(tmp_path, doc))
        assert proc.returncode == 2
        assert "nope" in proc.stderr

    def test_duplicate_channel_in_one_declaration_is_a_config_error(
        self, run_sil, tmp_path
    ):
        # The builder rejects duplicates; a hand-written manifest bypasses
        # that, so prove the kernel rejects them too, at load.
        m = toy_manifest(duration_ns=30_000_000)
        m.add_channel("ticks", schema="toy.Counter")
        add_producer(m, "producer", channel="ticks", period_ns=10_000_000)
        doc = m.to_doc()
        doc["participants"]["producer"]["publishes"] = ["ticks", "ticks"]
        proc = run_sil(_write_doc(tmp_path, doc))
        assert proc.returncode == 2
        assert "producer" in proc.stderr
        assert "twice" in proc.stderr

    def test_a_native_and_a_process_publishing_one_channel_is_a_config_error(
        self, run_sil, tmp_path
    ):
        # A channel has at most one publisher (#64). The builder rejects this
        # too; here the kernel must reject it at load, before any participant
        # is loaded or spawned, and name both publishers with their kinds.
        import sys as _sys

        from conftest import ROOT

        m = toy_manifest(duration_ns=30_000_000)
        m.add_channel("ticks", schema="toy.Counter")
        m.add_channel("spare", schema="toy.Counter")
        add_producer(m, "aprod", channel="ticks", period_ns=10_000_000)
        m.add_process(
            "psource",
            command=[_sys.executable,
                     str(ROOT / "tests" / "participants" / "counter_source.py"),
                     "ticks"],
            step_period_ns=10_000_000,
            publishes=["spare"],
        )
        # The builder would reject the collision, so declare it elsewhere and
        # move it onto 'ticks' in the document the kernel actually loads.
        doc = m.to_doc()
        doc["participants"]["psource"]["publishes"] = ["ticks"]
        proc = run_sil(_write_doc(tmp_path, doc))
        assert proc.returncode == 2
        assert "'ticks' has more than one publisher" in proc.stderr
        assert "'aprod' (native)" in proc.stderr
        assert "'psource' (process)" in proc.stderr

    def test_two_replayers_of_one_channel_is_a_config_error(
        self, run_sil, tmp_path
    ):
        # Replay participants are publishers under the same rule, so the check
        # that used to compare replay against live only now covers this too.
        m = toy_manifest(duration_ns=30_000_000)
        m.add_channel("ticks", schema="toy.Counter")
        add_producer(m, "aprod", channel="ticks", period_ns=10_000_000)
        doc = m.to_doc()
        del doc["participants"]["aprod"]
        for name in ("r1", "r2"):
            doc["participants"][name] = {
                "type": "replay",
                "recording": "rec.mcap",
                "recording_hash": "0" * 64,
                "channels": ["ticks"],
            }
        proc = run_sil(_write_doc(tmp_path, doc))
        assert proc.returncode == 2
        assert "'ticks' has more than one publisher" in proc.stderr
        assert "'r1' (replay)" in proc.stderr
        assert "'r2' (replay)" in proc.stderr

    def test_a_channel_with_no_publisher_runs(self, run_sil, tmp_path):
        # The rule is at most one publisher, not exactly one: a declared but
        # undriven channel still loads and the run completes.
        m = toy_manifest(duration_ns=30_000_000)
        m.add_channel("ticks", schema="toy.Counter")
        m.add_channel("undriven", schema="toy.Counter")
        add_producer(m, "aprod", channel="ticks", period_ns=10_000_000)
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr

    def test_the_second_publisher_is_rejected_before_the_library_loads(
        self, run_sil, tmp_path
    ):
        # The library path does not exist, so a run that reaches loading fails
        # differently. Reaching the cardinality diagnostic proves the check
        # still runs ahead of every participant.
        m = toy_manifest(duration_ns=30_000_000)
        m.add_channel("ticks", schema="toy.Counter")
        add_producer(m, "aprod", channel="ticks", period_ns=10_000_000)
        doc = m.to_doc()
        doc["participants"]["bprod"] = dict(
            doc["participants"]["aprod"], library="does-not-exist.so"
        )
        proc = run_sil(_write_doc(tmp_path, doc))
        assert proc.returncode == 2
        assert "'ticks' has more than one publisher" in proc.stderr
        assert "does-not-exist" not in proc.stderr


class TestNativeExceptionContainment:
    """A native participant runs in the kernel's own process, so an escaping
    C++ exception would unwind kernel frames — and across the C ABI, where the
    participant may be built against a different runtime, that is undefined.
    Every kernel frame that calls into participant code therefore catches
    everything and turns it into the first failure of the setup or the run,
    naming the participant and, for a task, the activation (issue #65)."""

    @pytest.mark.parametrize(
        "kind, needle",
        [("std", "boom"), ("other", "unknown exception")],
    )
    def test_a_throw_from_init_is_a_config_error(
        self, run_sil, tmp_path, kind, needle
    ):
        # Setup has not finished, so this is a Manifest error like any other
        # unloadable participant, not a run that starts and then aborts.
        m = toy_manifest(duration_ns=30_000_000)
        add_thrower(m, throw_in="init", kind=kind)
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 2
        assert "thrower" in proc.stderr
        assert needle in proc.stderr
        # A non-std throw is the case no handler can describe, so it is the one
        # that would abort the process instead of reporting.
        assert "terminating" not in proc.stderr

    @pytest.mark.parametrize(
        "kind, needle",
        [("std", "boom"), ("other", "unknown exception")],
    )
    def test_a_throw_from_a_task_aborts_the_run(
        self, run_sil, tmp_path, kind, needle
    ):
        m = toy_manifest(duration_ns=30_000_000)
        add_thrower(m, throw_in="task", kind=kind, period_ns=10_000_000)
        path = m.write(tmp_path / "m.json").path
        first = run_sil(path, out=tmp_path / "a.mcap")
        assert first.returncode == 1
        assert "thrower" in first.stderr
        # The task is named, so the diagnostic points at one activation rather
        # than at the participant as a whole.
        assert "explode" in first.stderr
        assert needle in first.stderr
        assert "terminating" not in first.stderr
        # The abort is part of the deterministic world: same manifest, same
        # exit code and same diagnostic.
        second = run_sil(path, out=tmp_path / "b.mcap")
        assert (second.returncode, second.stderr) == (
            first.returncode,
            first.stderr,
        )

    @pytest.mark.parametrize(
        "throw_in, returncode", [("init", 2), ("task", 1)]
    )
    def test_a_reported_failure_survives_a_later_throw(
        self, run_sil, tmp_path, throw_in, returncode
    ):
        # Containment is a safety net, not a diagnostic. A participant that
        # said why it is failing keeps that reason; the throw only stops it.
        m = toy_manifest(duration_ns=30_000_000)
        add_thrower(
            m, throw_in=throw_in, fail_first=True, period_ns=10_000_000
        )
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == returncode
        assert "real reason" in proc.stderr
        assert "threw" not in proc.stderr


def _write_doc(tmp_path, doc):
    """Write a hand-edited manifest document the Python builder would reject."""
    path = tmp_path / "raw.json"
    path.write_text(json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n")
    return path


def pipeline_manifest(producer_name, *, latency_ns=None, consumer_priority=0):
    m = toy_manifest(duration_ns=30_000_000)
    m.add_channel("ticks", schema="toy.Counter", latency_ns=latency_ns)
    m.add_channel("sums", schema="toy.Accum")
    add_producer(m, producer_name, channel="ticks", period_ns=10_000_000)
    add_accumulator(
        m,
        "mid",
        input_channel="ticks",
        output_channel="sums",
        period_ns=10_000_000,
        priority=consumer_priority,
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


def near_saturated_manifest(latency_ns):
    """A Run where an Interceptor delay leaves a Message visible one
    nanosecond below UINT64_MAX, so an explicit Channel Latency composes on
    top of an almost-saturated visible time."""
    m = toy_manifest(duration_ns=U64_MAX)
    m.add_channel("ticks", schema="toy.Counter", latency_ns=latency_ns)
    m.add_channel("sums", schema="toy.Accum")
    add_producer(m, "aprod", channel="ticks", period_ns=U64_MAX - 1)
    # Ordered after the producer, so the last slot's publish is already in the
    # queue when the consumer runs.
    add_accumulator(m, "mid", period_ns=U64_MAX - 1, priority=10)
    m.add_interceptor("ticks", kind="delay", delay_ns=U64_MAX - 1)
    return m


class TestChannelLatencyOverflow:
    """Explicit Channel Latency is added to a Message's post-Interceptor
    visible time. That addition never wraps: a visibility virtual time cannot
    represent is never delivered (issue #50)."""

    def test_latency_of_one_nanosecond_delivers_at_the_next_slot(
        self, run_sil, tmp_path
    ):
        m = pipeline_manifest("aprod", latency_ns=1)
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr
        assert sums(proc.mcap_path) == [
            (0, {"count": 0, "sum": 0}),
            (10_000_000, {"count": 1, "sum": 0}),
            (20_000_000, {"count": 2, "sum": 3}),
        ]

    def test_visibility_at_exactly_the_duration_is_never_delivered(
        self, run_sil, tmp_path
    ):
        # Publish at 0 plus a latency of the whole run: no slot ever reaches
        # the visible time, because the run covers [0, duration).
        m = pipeline_manifest("aprod", latency_ns=30_000_000)
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr
        assert [count for _, count in sums(proc.mcap_path)] == [
            {"count": 0, "sum": 0}
        ] * 3

    def test_latency_uint64_max_is_never_delivered(self, run_sil, tmp_path):
        # A single publish away from zero, so the wrapped visible time would
        # land in the past rather than behind an already-blocked front: the
        # unchecked addition delivered it in the very slot it was published.
        m = toy_manifest(duration_ns=30_000_000)
        m.add_channel("ticks", schema="toy.Counter", latency_ns=U64_MAX)
        m.add_channel("sums", schema="toy.Accum")
        add_producer(
            m,
            "aprod",
            channel="ticks",
            period_ns=100_000_000,
            offset_ns=10_000_000,
        )
        add_accumulator(m, "mid", period_ns=10_000_000)
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr
        assert [count for _, count in sums(proc.mcap_path)] == [
            {"count": 0, "sum": 0}
        ] * 3

    def test_latency_just_below_max_is_delivered_at_the_last_slot(
        self, run_sil, tmp_path
    ):
        m = toy_manifest(duration_ns=U64_MAX)
        m.add_channel("ticks", schema="toy.Counter", latency_ns=U64_MAX - 1)
        m.add_channel("sums", schema="toy.Accum")
        add_producer(m, "aprod", channel="ticks", period_ns=U64_MAX - 1)
        add_accumulator(m, "mid", period_ns=U64_MAX - 1)
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr
        # The tick published at 0 becomes visible at exactly UINT64_MAX - 1;
        # the one published there would only be visible past the clock.
        assert sums(proc.mcap_path) == [
            (0, {"count": 0, "sum": 0}),
            (U64_MAX - 1, {"count": 1, "sum": 0}),
        ]

    def test_zero_latency_over_a_near_saturated_visible_time_is_delivered(
        self, run_sil, tmp_path
    ):
        proc = run_sil(
            near_saturated_manifest(0).write(tmp_path / "m.json").path
        )
        assert proc.returncode == 0, proc.stderr
        assert sums(proc.mcap_path) == [
            (0, {"count": 0, "sum": 0}),
            (U64_MAX - 1, {"count": 1, "sum": 0}),
        ]

    def test_one_nanosecond_over_a_near_saturated_visible_time_is_not_delivered(
        self, run_sil, tmp_path
    ):
        # Visible at UINT64_MAX: representable, but past every slot the run
        # can open.
        proc = run_sil(
            near_saturated_manifest(1).write(tmp_path / "m.json").path
        )
        assert proc.returncode == 0, proc.stderr
        assert [count for _, count in sums(proc.mcap_path)] == [
            {"count": 0, "sum": 0}
        ] * 2

    def test_latency_past_a_near_saturated_visible_time_is_not_delivered(
        self, run_sil, tmp_path
    ):
        # Visible time overflows; the message is never delivered, and the
        # recorded timestamp stays the post-Interceptor one.
        proc = run_sil(
            near_saturated_manifest(2).write(tmp_path / "m.json").path
        )
        assert proc.returncode == 0, proc.stderr
        assert [count for _, count in sums(proc.mcap_path)] == [
            {"count": 0, "sum": 0}
        ] * 2
        _, msgs = read_mcap(proc.mcap_path)
        assert [t for topic, t, _ in msgs if topic == "ticks"] == [U64_MAX - 1]


class TestProcessParticipant:
    def test_each_process_gets_a_cleaned_working_directory_under_the_run(
        self, sil_run, tmp_path
    ):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        probe = run_dir / "working_directory_probe.py"
        shutil.copy2(
            ROOT / "tests" / "participants" / "working_directory_probe.py", probe
        )
        probe.chmod(0o755)

        m = toy_manifest(duration_ns=1)
        observers = []
        for name in ("first", "second"):
            observer = run_dir / f"{name}-observer"
            observer.mkdir()
            observers.append(observer)
            m.add_process(
                name,
                command=["./working_directory_probe.py", observer.name],
                step_period_ns=1,
            )
        manifest = m.write(run_dir / "m.json").path

        proc = subprocess.run(
            [str(sil_run), str(manifest), "--no-recording"],
            cwd=run_dir,
            capture_output=True,
            text=True,
        )

        assert proc.returncode == 0, proc.stderr
        working_directories = [
            observer.joinpath("cwd").read_text() for observer in observers
        ]
        assert len(set(working_directories)) == 2
        paths = list(map(Path, working_directories))
        run_working_directories = {path.parent for path in paths}
        assert len(run_working_directories) == 1
        run_working_directory = run_working_directories.pop()
        assert run_working_directory.parent == run_dir
        assert all(not path.exists() for path in paths)
        assert not run_working_directory.exists()

    def test_sigkill_leaves_no_process_working_directory(
        self, sil_run, tmp_path
    ):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        probe = run_dir / "working_directory_probe.py"
        shutil.copy2(
            ROOT / "tests" / "participants" / "working_directory_probe.py", probe
        )
        probe.chmod(0o755)
        observer = run_dir / "observer"
        observer.mkdir()

        m = toy_manifest(duration_ns=2)
        m.add_process(
            "killed",
            command=["./working_directory_probe.py", observer.name, "sigkill"],
            step_period_ns=1,
        )
        manifest = m.write(run_dir / "m.json").path

        proc = subprocess.run(
            [str(sil_run), str(manifest), "--no-recording"],
            cwd=run_dir,
            capture_output=True,
            text=True,
        )

        assert proc.returncode == 1
        working_directory = Path(observer.joinpath("cwd").read_text())
        assert working_directory.parent.parent == run_dir
        assert not working_directory.exists()
        assert not working_directory.parent.exists()

    def test_bare_executable_still_uses_path(self, sil_run, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        shutil.copy2(
            ROOT / "tests" / "participants" / "working_directory_probe.py",
            run_dir / "working_directory_probe.py",
        )
        observer = run_dir / "observer"
        observer.mkdir()
        # An existing invocation-relative entry with the same name must not
        # capture a bare executable; only command[0] values containing a path
        # component resolve against the invocation directory.
        run_dir.joinpath("python3").write_text("not an executable")

        m = toy_manifest(duration_ns=1)
        m.add_process(
            "probe",
            command=["python3", "working_directory_probe.py", observer.name],
            step_period_ns=1,
        )
        manifest = m.write(run_dir / "m.json").path

        proc = subprocess.run(
            [str(sil_run), str(manifest), "--no-recording"],
            cwd=run_dir,
            capture_output=True,
            text=True,
        )

        assert proc.returncode == 0, proc.stderr

    def test_python_step_participant_transforms_messages(self, run_sil, tmp_path):
        import sys as _sys

        from conftest import ROOT

        m = toy_manifest(duration_ns=30_000_000)
        m.add_channel("ticks", schema="toy.Counter")
        m.add_channel("echo", schema="toy.Counter")
        add_producer(m, "aprod", channel="ticks", period_ns=10_000_000)
        m.add_process(
            "pecho",
            command=[_sys.executable,
                     str(ROOT / "tests" / "participants" / "echo.py")],
            step_period_ns=10_000_000,
            subscribes=[SubscriberRoute("ticks", capacity=COMPAT_ROUTE_CAPACITY)],
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
    add_producer(m, "producer", channel=channel, period_ns=10_000_000)
    doc = m.to_doc()
    doc["channels"][channel]["interceptors"] = [entry]
    path = tmp_path / "m.json"
    path.write_text(json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n")
    return path


class TestInterceptorRejection:
    """The kernel mirrors the builder's eager checks at load: a malformed
    interceptor is a Manifest error (exit 2), distinguishable from a test
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

    @pytest.mark.parametrize(
        ("field_type", "values"),
        [
            ("u8", [0, 1, 254, 255]),
            ("u16", [0, 1, 65534, 65535]),
            ("u32", [0, 1, 2**32 - 2, 2**32 - 1]),
            ("u64", [0, 1, 2**64 - 2, 2**64 - 1]),
            ("i8", [-(2**7), -(2**7) + 1, 0, 1, 2**7 - 2, 2**7 - 1]),
            ("i16", [-(2**15), -(2**15) + 1, 0, 1, 2**15 - 2, 2**15 - 1]),
            ("i32", [-(2**31), -(2**31) + 1, 0, 1, 2**31 - 2, 2**31 - 1]),
            ("i64", [-(2**63), -(2**63) + 1, 0, 1, 2**63 - 2, 2**63 - 1]),
        ],
    )
    def test_integer_override_boundaries_load(self, run_sil, tmp_path,
                                              field_type, values):
        document = raw_manifest()
        document["schemas"]["S"]["fields"][0]["type"] = field_type
        document["channels"] = {
            f"c{index}": {
                "schema": "S",
                "interceptors": [
                    {"kind": "override", "field": "value", "value": value}
                ],
            }
            for index, value in enumerate(values)
        }
        proc = run_sil(write_raw_manifest(tmp_path, document))
        assert proc.returncode == 0, proc.stderr

    @pytest.mark.parametrize(
        ("field_type", "value"),
        [
            ("u8", -1), ("u8", 256),
            ("u16", -1), ("u16", 65536),
            ("u32", -1), ("u32", 2**32),
            ("u64", -1), ("u64", 2**64),
            ("i8", -(2**7) - 1), ("i8", 2**7),
            ("i16", -(2**15) - 1), ("i16", 2**15),
            ("i32", -(2**31) - 1), ("i32", 2**31),
            ("i64", -(2**63) - 1), ("i64", 2**63),
        ],
    )
    def test_adjacent_out_of_range_integer_overrides_are_config_errors(
        self, run_sil, tmp_path, field_type, value
    ):
        document = raw_manifest()
        document["schemas"]["S"]["fields"][0]["type"] = field_type
        document["channels"]["c"]["interceptors"] = [
            {"kind": "override", "field": "value", "value": value}
        ]
        proc = run_sil(write_raw_manifest(tmp_path, document))
        assert proc.returncode == 2
        assert "expected" in proc.stderr and "got" in proc.stderr

    @pytest.mark.parametrize(
        ("field_type", "value", "accepted"),
        [
            ("f32", 0, True),
            ("f32", 1, True),
            ("f32", 1.5, True),
            ("f32", 3.4028234663852886e38, True),
            ("f32", 3.4028236e38, False),
            ("f32", float("inf"), False),
            ("f64", 0, True),
            ("f64", 1, True),
            ("f64", 1.5, True),
            ("f64", 1.7976931348623157e308, True),
            ("f64", float("inf"), False),
        ],
    )
    def test_float_override_policy_matches_builder(
        self, run_sil, tmp_path, field_type, value, accepted
    ):
        document = raw_manifest()
        document["schemas"]["S"]["fields"][0]["type"] = field_type
        document["channels"]["c"]["interceptors"] = [
            {"kind": "override", "field": "value", "value": value}
        ]
        proc = run_sil(write_raw_manifest(tmp_path, document))
        assert proc.returncode == (0 if accepted else 2)


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
        add_producer(m, "producer", channel="ticks", period_ns=10_000_000)
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
        add_producer(m, "producer", channel="ticks", period_ns=10_000_000)
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
        add_producer(m, "aprod", channel="ticks", period_ns=10_000_000)
        add_accumulator(
            m,
            "mid",
            input_channel="ticks",
            output_channel="sums",
            period_ns=10_000_000,
            priority=0,
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
        add_producer(base, "aprod", channel="ticks", period_ns=10_000_000)
        add_accumulator(
            base,
            "mid",
            input_channel="ticks",
            output_channel="sums",
            period_ns=10_000_000,
            priority=0,
        )
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
        add_producer(m, "producer", channel="ticks", period_ns=10_000_000)
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
            add_producer(m, "aprod", channel="ticks", period_ns=10_000_000)
            add_accumulator(
                m,
                "mid",
                input_channel="ticks",
                output_channel="sums",
                period_ns=10_000_000,
                priority=0,
            )
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

    @pytest.mark.parametrize("transport", ["inline", "shm"])
    def test_u64_override_reaches_recording_and_subscriber_exactly(
        self, run_sil, tmp_path, transport
    ):
        import sys as _sys

        from conftest import ROOT

        exact_value = 18_446_744_073_709_551_614
        m = toy_manifest(duration_ns=30_000_000)
        m.add_channel("ticks", schema="toy.Counter", transport=transport)
        m.add_channel("echo", schema="toy.Counter")
        add_producer(m, "producer", channel="ticks", period_ns=10_000_000)
        m.add_process(
            "echo",
            command=[_sys.executable, str(ROOT / "tests/participants/echo.py")],
            step_period_ns=10_000_000,
            subscribes=[SubscriberRoute("ticks", capacity=COMPAT_ROUTE_CAPACITY)],
            publishes=["echo"],
        )
        m.add_interceptor(
            "ticks", kind="override", field="seq", value=exact_value
        )

        proc = run_sil(m.write(tmp_path / f"{transport}.json").path)
        assert proc.returncode == 0, proc.stderr
        _, messages = read_mcap(proc.mcap_path)
        recorded = [
            TYPES["toy.Counter"].unpack(data)["seq"]
            for channel, _, data in messages
            if channel == "ticks"
        ]
        observed = [
            TYPES["toy.Counter"].unpack(data)["seq"]
            for channel, _, data in messages
            if channel == "echo"
        ]
        assert recorded == [exact_value] * 3
        assert observed == [exact_value] * 2

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("seq", 2**53 + 1),
            ("seq", 2**64 - 1),
            ("value", -(2**63)),
            ("value", 2**63 - 1),
            ("value", -(2**53 + 1)),
        ],
    )
    def test_64_bit_integer_override_roundtrips_exactly(
        self, run_sil, tmp_path, field, value
    ):
        m = self._producer(duration_ns=10_000_000)
        m.add_interceptor("ticks", kind="override", field=field, value=value)
        proc = run_sil(m.write(tmp_path / f"{field}-{value}.json").path)
        assert proc.returncode == 0, proc.stderr
        _, messages = read_mcap(proc.mcap_path)
        [message] = [
            TYPES["toy.Counter"].unpack(data)
            for channel, _, data in messages
            if channel == "ticks"
        ]
        assert message[field] == value

    def test_f64_integer_override_uses_documented_binary64_rounding(
        self, run_sil, tmp_path
    ):
        integer_value = 2**53 + 1
        schemas = {
            "FloatCounter": {
                "fields": [
                    {"name": "seq", "type": "u64"},
                    {"name": "value", "type": "f64"},
                ]
            }
        }
        types = schema.load(schemas)
        m = Manifest(duration_ns=10_000_000)
        m.add_schemas(schemas)
        m.add_channel("ticks", schema="FloatCounter")
        add_producer(m, "producer", channel="ticks", period_ns=10_000_000)
        m.add_interceptor(
            "ticks", kind="override", field="value", value=integer_value
        )
        [entry] = m.to_doc()["channels"]["ticks"]["interceptors"]
        assert entry["value"] == integer_value

        proc = run_sil(m.write(tmp_path / "f64-integer.json").path)
        assert proc.returncode == 0, proc.stderr
        _, messages = read_mcap(proc.mcap_path)
        [message] = [
            types["FloatCounter"].unpack(data)
            for channel, _, data in messages
            if channel == "ticks"
        ]
        assert message["value"] == 9_007_199_254_740_992.0

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
        add_producer(m, "producer", channel="ticks", period_ns=10_000_000)
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
            add_producer(m, "aprod", channel="ticks", period_ns=10_000_000)
            add_accumulator(
                m,
                "mid",
                input_channel="ticks",
                output_channel="sums",
                period_ns=10_000_000,
                priority=0,
            )
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
        add_producer(m, "producer", channel="ticks", period_ns=10_000_000)
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
    boundary (req #24). An unrecognized extension is a Manifest error, caught
    before any participant starts; the sole v1 format, .mcap, is unchanged."""

    def _producer_with_marker(self, tmp_path):
        """A manifest whose process participant writes a marker file the moment
        the kernel spawns it, so a missing marker proves no participant ran."""
        import sys as _sys

        from conftest import ROOT

        marker = tmp_path / "spawned.marker"
        m = toy_manifest(duration_ns=30_000_000)
        m.add_channel("ticks", schema="toy.Counter")
        add_producer(m, "aprod", channel="ticks", period_ns=10_000_000)
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
        add_producer(m, "producer", channel="ticks", period_ns=10_000_000)
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
        add_producer(m, "aprod", channel="ticks", period_ns=10_000_000)
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


ARRAY_SCHEMAS = {
    "big.Payload": {
        "fields": [
            {"name": "id", "type": "u64"},
            {"name": "blob", "type": "u8", "count": 4},
            {"name": "samples", "type": "f32", "count": 8},
        ]
    }
}
ARRAY_TYPES = schema.load(ARRAY_SCHEMAS)


def write_with_raw_array_schema(tmp_path, schemas):
    """Build a producer manifest but swap in a raw (possibly malformed) schema
    set, so the kernel's load-time array validation can be exercised with
    declarations the Python builder would itself reject."""
    m = toy_manifest(duration_ns=100_000_000)
    m.add_channel("ticks", schema="toy.Counter")
    add_producer(m, "producer", channel="ticks", period_ns=10_000_000)
    doc = m.to_doc()
    doc["schemas"] = schemas
    path = tmp_path / "m.json"
    path.write_text(json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n")
    return path


class TestArrayPayload:
    """Fixed-size array fields round-trip bit-for-bit over the inline
    transport, and the kernel mirrors the builder's array validation at load."""

    def test_array_payload_roundtrips_bit_for_bit(self, run_sil, tmp_path):
        import sys as _sys

        from conftest import ROOT

        m = Manifest(duration_ns=30_000_000)
        m.add_schemas(ARRAY_SCHEMAS)
        m.add_channel("payload", schema="big.Payload")
        m.add_process(
            "source",
            command=[_sys.executable,
                     str(ROOT / "tests" / "participants" / "array_source.py")],
            step_period_ns=10_000_000,
            publishes=["payload"],
        )
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr

        _, msgs = read_mcap(proc.mcap_path)
        payloads = [
            ARRAY_TYPES["big.Payload"].unpack(data)
            for topic, _, data in msgs
            if topic == "payload"
        ]
        # One message per step; each payload is a pure function of the step
        # index, reproduced here to assert the array bytes survive intact.
        expected = []
        for step in range(3):
            expected.append({
                "id": step,
                "blob": bytes((step + k) % 256 for k in range(4)),
                "samples": [float(step * 10 + k) for k in range(8)],
            })
        assert payloads == expected

    def test_zero_count_array_is_config_error(self, run_sil, tmp_path):
        bad = {"S": {"fields": [{"name": "a", "type": "u8", "count": 0}]}}
        proc = run_sil(write_with_raw_array_schema(tmp_path, bad))
        assert proc.returncode == 2
        assert "count" in proc.stderr

    def test_unknown_array_element_type_is_config_error(self, run_sil, tmp_path):
        bad = {"S": {"fields": [{"name": "a", "type": "vec3", "count": 4}]}}
        proc = run_sil(write_with_raw_array_schema(tmp_path, bad))
        assert proc.returncode == 2
        assert "vec3" in proc.stderr


class TestShmTransport:
    """A channel over the shared-memory transport moves its payload through a
    per-channel arena instead of base64/JSON, with no participant-visible
    difference from inline (the same fixture, unchanged)."""

    def _array_manifest(self, transport):
        import sys as _sys

        from conftest import ROOT

        m = Manifest(duration_ns=30_000_000)
        m.add_schemas(ARRAY_SCHEMAS)
        m.add_channel("payload", schema="big.Payload", transport=transport)
        m.add_process(
            "source",
            command=[_sys.executable,
                     str(ROOT / "tests" / "participants" / "array_source.py")],
            step_period_ns=10_000_000,
            publishes=["payload"],
        )
        return m

    def _expected_payloads(self):
        return [
            {
                "id": step,
                "blob": bytes((step + k) % 256 for k in range(4)),
                "samples": [float(step * 10 + k) for k in range(8)],
            }
            for step in range(3)
        ]

    def _payloads(self, mcap_path):
        """
        Extract decoded payload messages from an MCAP recording.
        
        Parameters:
        	mcap_path: Path to the MCAP recording.
        
        Returns:
        	A list of unpacked payload values from messages on the `payload` topic.
        """
        _, msgs = read_mcap(mcap_path)
        return [
            ARRAY_TYPES["big.Payload"].unpack(data)
            for topic, _, data in msgs
            if topic == "payload"
        ]

    def _interceptor_manifest(self, transport, interceptor):
        """Build a process-to-process array route with one channel interceptor."""
        import sys as _sys

        from conftest import ROOT

        m = Manifest(duration_ns=60_000_000)
        m.add_schemas(ARRAY_SCHEMAS)
        m.add_channel("payload", schema="big.Payload", transport=transport)
        m.add_channel("mirror", schema="big.Payload")
        m.add_process(
            "source",
            command=[_sys.executable,
                     str(ROOT / "tests" / "participants" / "array_source.py")],
            step_period_ns=10_000_000,
            publishes=["payload"],
        )
        m.add_process(
            "sink",
            command=[_sys.executable,
                     str(ROOT / "tests" / "participants" / "array_echo.py")],
            step_period_ns=10_000_000,
            subscribes=[SubscriberRoute("payload", capacity=COMPAT_ROUTE_CAPACITY)],
            publishes=["mirror"],
        )
        m.add_interceptor("payload", **interceptor)
        return m

    def _message_ids(self, mcap_path, channel):
        """
        Extract message visibility times and array IDs for a channel.
        
        Parameters:
            mcap_path: Path to the MCAP recording.
            channel: Channel name to inspect.
        
        Returns:
            A list of ``(visibility_time, array_id)`` tuples in recording order.
        """
        _, msgs = read_mcap(mcap_path)
        return [
            (t, ARRAY_TYPES["big.Payload"].unpack(data)["id"])
            for name, t, data in msgs
            if name == channel
        ]

    def test_shm_channel_roundtrips_array_payload(self, run_sil, tmp_path):
        m = self._array_manifest("shm")
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr
        assert self._payloads(proc.mcap_path) == self._expected_payloads()

    @pytest.mark.parametrize(
        ("interceptor", "payload_messages", "mirror_messages"),
        [
            (
                {"kind": "drop", "start_ns": 20_000_000, "end_ns": 40_000_000},
                [(0, 0), (10_000_000, 1), (40_000_000, 4), (50_000_000, 5)],
                [(10_000_000, 0), (20_000_000, 1), (50_000_000, 4)],
            ),
            (
                {"kind": "drop_nth", "start_ns": 10_000_000,
                 "end_ns": 50_000_000, "n": 2},
                [(0, 0), (10_000_000, 1), (30_000_000, 3), (50_000_000, 5)],
                [(10_000_000, 0), (20_000_000, 1), (40_000_000, 3)],
            ),
            (
                {"kind": "delay", "start_ns": 0, "end_ns": 30_000_000,
                 "delay_ns": 15_000_000},
                [(15_000_000, 0), (25_000_000, 1), (30_000_000, 3),
                 (35_000_000, 2), (40_000_000, 4), (50_000_000, 5)],
                [(20_000_000, 0), (30_000_000, 1), (40_000_000, 2),
                 (40_000_000, 3), (50_000_000, 4)],
            ),
            (
                {"kind": "override", "start_ns": 10_000_000,
                 "end_ns": 30_000_000, "field": "id", "value": 99},
                [(0, 0), (10_000_000, 99), (20_000_000, 99),
                 (30_000_000, 3), (40_000_000, 4), (50_000_000, 5)],
                [(10_000_000, 0), (20_000_000, 99), (30_000_000, 99),
                 (40_000_000, 3), (50_000_000, 4)],
            ),
            (
                # Open-ended window (no end_ns): silences the channel from
                # 30ms to the end of the 60ms run, over the arena transport.
                {"kind": "drop", "start_ns": 30_000_000},
                [(0, 0), (10_000_000, 1), (20_000_000, 2)],
                [(10_000_000, 0), (20_000_000, 1), (30_000_000, 2)],
            ),
            (
                # n=1 drops every in-window message: equivalent to a plain
                # drop over [10ms, 40ms), but exercised through drop_nth.
                {"kind": "drop_nth", "start_ns": 10_000_000,
                 "end_ns": 40_000_000, "n": 1},
                [(0, 0), (40_000_000, 4), (50_000_000, 5)],
                [(10_000_000, 0), (50_000_000, 4)],
            ),
        ],
        ids=["drop", "drop_nth", "delay", "override", "drop_open_ended",
             "drop_nth_one"],
    )
    def test_interceptors_have_the_same_effect_over_inline_and_shm(
        self, run_sil, tmp_path, interceptor, payload_messages, mirror_messages
    ):
        """Every interceptor keeps its inline semantics on an arena channel."""
        for transport in ("inline", "shm"):
            manifest = self._interceptor_manifest(transport, interceptor)
            proc = run_sil(
                manifest.write(tmp_path / f"{transport}-{interceptor['kind']}.json").path,
                out=tmp_path / f"{transport}-{interceptor['kind']}.mcap",
            )
            assert proc.returncode == 0, proc.stderr
            assert self._message_ids(proc.mcap_path, "payload") == payload_messages
            assert self._message_ids(proc.mcap_path, "mirror") == mirror_messages

    def test_shm_override_preserves_the_rest_of_the_payload(
        self, run_sil, tmp_path
    ):
        """Override changes only the selected scalar before arena delivery."""
        m = self._interceptor_manifest(
            "shm",
            {"kind": "override", "start_ns": 10_000_000,
             "end_ns": 30_000_000, "field": "id", "value": 99},
        )
        proc = run_sil(m.write(tmp_path / "override.json").path)
        assert proc.returncode == 0, proc.stderr

        payloads = self._payloads(proc.mcap_path)
        expected = [
            {
                "id": step,
                "blob": bytes((step + k) % 256 for k in range(4)),
                "samples": [float(step * 10 + k) for k in range(8)],
            }
            for step in range(6)
        ]
        expected[1] = {**expected[1], "id": 99}
        expected[2] = {**expected[2], "id": 99}
        assert payloads == expected

        _, msgs = read_mcap(proc.mcap_path)
        mirrored = [
            ARRAY_TYPES["big.Payload"].unpack(data)
            for channel, _, data in msgs
            if channel == "mirror"
        ]
        assert mirrored == expected[:5]

    def test_shm_interceptor_run_is_deterministic_across_two_runs(
        self, run_sil, tmp_path
    ):
        """An interceptor composed with the shm transport stays deterministic."""
        m = self._interceptor_manifest(
            "shm",
            {"kind": "override", "start_ns": 10_000_000,
             "end_ns": 30_000_000, "field": "id", "value": 99},
        )
        manifest = m.write(tmp_path / "det.json").path
        a = run_sil(manifest, out=tmp_path / "det-a.mcap")
        b = run_sil(manifest, out=tmp_path / "det-b.mcap")
        assert a.returncode == 0, a.stderr
        assert b.returncode == 0, b.stderr
        assert a.mcap_path.read_bytes() == b.mcap_path.read_bytes()

    def test_shm_recording_matches_inline_byte_for_byte(self, run_sil, tmp_path):
        # The transport is a delivery detail, not a semantic one: an shm run and
        # an inline run of the same fixture must record the identical payload
        # bytes. Proves the arena path carries the exact bytes base64 would and
        # that the participant API never changed.
        inline = run_sil(
            self._array_manifest("inline").write(tmp_path / "inline.json").path,
            out=tmp_path / "inline.mcap",
        )
        shm = run_sil(
            self._array_manifest("shm").write(tmp_path / "shm.json").path,
            out=tmp_path / "shm.mcap",
        )
        assert inline.returncode == 0, inline.stderr
        assert shm.returncode == 0, shm.stderr
        assert self._payloads(shm.mcap_path) == self._payloads(inline.mcap_path)

    def test_two_shm_runs_are_bit_identical(self, run_sil, tmp_path):
        ref = self._array_manifest("shm").write(tmp_path / "m.json")
        a = run_sil(ref.path, out=tmp_path / "a.mcap")
        b = run_sil(ref.path, out=tmp_path / "b.mcap")
        assert a.returncode == 0, a.stderr
        assert b.returncode == 0, b.stderr
        assert a.mcap_path.read_bytes() == b.mcap_path.read_bytes()

    def test_unmappable_arena_is_startup_config_error(self, sil_run, tmp_path):
        # An arena the kernel cannot create is an environment problem, not a test
        # failure: it must fail at startup with exit 2 (distinct from a run's
        # exit 1). The kernel creates the arena itself, before it forks the
        # child, so pointing TMPDIR at a path that cannot hold the arena file
        # makes its mkstemp fail before any participant starts.
        ref = self._array_manifest("shm").write(tmp_path / "m.json")
        no_such_dir = tmp_path / "does-not-exist"
        env = {**os.environ, "TMPDIR": str(no_such_dir)}
        proc = subprocess.run(
            [str(sil_run), str(ref.path), "-o", str(tmp_path / "out.mcap")],
            capture_output=True, text=True, env=env,
        )
        assert proc.returncode == 2, proc.stderr
        assert "arena" in proc.stderr

    def test_shm_input_path_delivers_payload_to_subscriber(self, run_sil, tmp_path):
        # array_source only publishes (child->kernel arena writes). Add a sink
        # that subscribes to the shm channel and republishes onto an inline
        # mirror, exercising the kernel->child arena-write + Python read path.
        # The mirror payloads must equal what the source produced.
        import sys as _sys

        from conftest import ROOT

        m = Manifest(duration_ns=60_000_000)
        m.add_schemas(ARRAY_SCHEMAS)
        m.add_channel("payload", schema="big.Payload", transport="shm")
        m.add_channel("mirror", schema="big.Payload")
        m.add_process(
            "source",
            command=[_sys.executable,
                     str(ROOT / "tests" / "participants" / "array_source.py")],
            step_period_ns=10_000_000,
            publishes=["payload"],
        )
        m.add_process(
            "sink",
            command=[_sys.executable,
                     str(ROOT / "tests" / "participants" / "array_echo.py")],
            step_period_ns=10_000_000,
            subscribes=[SubscriberRoute("payload", capacity=COMPAT_ROUTE_CAPACITY)],
            publishes=["mirror"],
        )
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 0, proc.stderr

        _, msgs = read_mcap(proc.mcap_path)
        mirrored = [
            ARRAY_TYPES["big.Payload"].unpack(data)
            for topic, _, data in msgs
            if topic == "mirror"
        ]
        # The sink republishes what it received; a non-empty mirror stream whose
        # every payload is one the source actually produced (by the source's
        # pure step function) proves the shm input path carried real bytes.
        assert mirrored, "sink never received a shm input"
        valid = [
            {
                "id": step,
                "blob": bytes((step + k) % 256 for k in range(4)),
                "samples": [float(step * 10 + k) for k in range(8)],
            }
            for step in range(6)  # 60ms / 10ms step, enough to cover all seen
        ]
        for payload in mirrored:
            assert payload in valid, f"sink saw a payload the source never sent: {payload}"

    def test_arena_files_are_cleaned_up_at_run_end(self, sil_run, tmp_path):
        # Arenas are temp files under TMPDIR; scope TMPDIR to an empty dir and
        # assert nothing is left behind once the run's process tree exits.
        arena_dir = tmp_path / "arenas"
        arena_dir.mkdir()
        ref = self._array_manifest("shm").write(tmp_path / "m.json")
        env = {**os.environ, "TMPDIR": str(arena_dir)}
        proc = subprocess.run(
            [str(sil_run), str(ref.path), "-o", str(tmp_path / "out.mcap")],
            capture_output=True, text=True, env=env,
        )
        assert proc.returncode == 0, proc.stderr
        leftover = list(arena_dir.glob("sil_arena_*"))
        assert leftover == [], f"arena files leaked: {leftover}"

    def test_bidirectional_shm_channel_is_config_error(self, run_sil, tmp_path):
        # An Arena mapping has one writer direction; a participant that both
        # subscribes and publishes the same shm Channel is rejected at startup
        # until the protocol provides direction-separated mappings.
        import sys as _sys

        from conftest import ROOT

        m = Manifest(duration_ns=30_000_000)
        m.add_schemas(ARRAY_SCHEMAS)
        m.add_channel("payload", schema="big.Payload", transport="shm")
        m.add_process(
            "loop",
            command=[_sys.executable,
                     str(ROOT / "tests" / "participants" / "array_echo.py")],
            step_period_ns=10_000_000,
            subscribes=[SubscriberRoute("payload", capacity=COMPAT_ROUTE_CAPACITY)],
            publishes=["payload"],
        )
        proc = run_sil(m.write(tmp_path / "m.json").path)
        assert proc.returncode == 2, proc.stderr
        assert "shm" in proc.stderr

    def _burst_manifest(self, transport):
        # A sink stepping 3x slower than the source sees three messages on the
        # channel in one step, and republishes all three in one step_done — an
        # input burst and an output burst through a bounded Arena.
        import sys as _sys

        from conftest import ROOT

        m = Manifest(duration_ns=60_000_000)
        m.add_schemas(ARRAY_SCHEMAS)
        m.add_channel("payload", schema="big.Payload", transport=transport)
        m.add_channel("mirror", schema="big.Payload", transport=transport)
        m.add_process(
            "source",
            command=[_sys.executable,
                     str(ROOT / "tests" / "participants" / "array_source.py")],
            step_period_ns=10_000_000,
            publishes=["payload"],
        )
        m.add_process(
            "sink",
            command=[_sys.executable,
                     str(ROOT / "tests" / "participants" / "array_echo.py")],
            step_period_ns=30_000_000,
            subscribes=[SubscriberRoute("payload", capacity=COMPAT_ROUTE_CAPACITY)],
            publishes=["mirror"],
        )
        return m

    def _mirrored(self, mcap_path):
        _, msgs = read_mcap(mcap_path)
        return [
            ARRAY_TYPES["big.Payload"].unpack(data)
            for topic, _, data in msgs
            if topic == "mirror"
        ]

    def test_message_burst_matches_inline_exactly(self, run_sil, tmp_path):
        # A burst deeper than the declared Arena must fall back to inline for
        # only its excess Messages instead of overwriting a slot. A Manifest
        # that runs inline must run identically over shm —
        # otherwise the transport has leaked into participant code.
        inline = run_sil(
            self._burst_manifest("inline").write(tmp_path / "inline.json").path,
            out=tmp_path / "inline.mcap",
        )
        shm = run_sil(
            self._burst_manifest("shm").write(tmp_path / "shm.json").path,
            out=tmp_path / "shm.mcap",
        )
        assert inline.returncode == 0, inline.stderr
        assert shm.returncode == 0, shm.stderr
        expected = self._mirrored(inline.mcap_path)
        # Three messages per sink step is what makes this a burst; a run that
        # silently delivered one would pass the equality check vacuously.
        assert len(expected) >= 3, f"fixture stopped bursting: {expected}"
        assert self._mirrored(shm.mcap_path) == expected

    def test_kernel_rejects_unknown_transport(self, run_sil, tmp_path):
        # The kernel mirrors the builder's transport validation (defense in
        # depth): a hand-written manifest with a bogus transport is a config
        # error at load, before any participant starts.
        m = toy_manifest(duration_ns=100_000_000)
        m.add_channel("ticks", schema="toy.Counter")
        add_producer(m, "producer", channel="ticks", period_ns=10_000_000)
        doc = m.to_doc()
        doc["channels"]["ticks"]["transport"] = "rdma"
        path = tmp_path / "m.json"
        path.write_text(json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n")
        proc = run_sil(path)
        assert proc.returncode == 2
        assert "transport" in proc.stderr
