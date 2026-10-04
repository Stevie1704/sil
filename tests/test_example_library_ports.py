"""The shared-library adapter's port binding at the run boundary (issue #259).

Recorded ego speeds and radar objects, at different rates and Latencies and
with one radar Burst, are replayed into `gap_monitor`, a library with two
input structs, two output structs and two cyclic entry points, through
`examples/library/adapter.py --binding gap_binding.py`. The in-run Test
participant computes every output independently; these tests state the
decisive outputs by hand from `gap_signals.csv` and the Manifest, and drive
the Manifest errors through invalid declarations and Schemas.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import ROOT

from sil.csv_recording import convert
from sil.participant import ManifestError, ParticipantFailure
from sil.testing import run_simulation

EXAMPLE = ROOT / "examples" / "library"
MS = 1_000_000


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manifest = _load("gap_manifest", EXAMPLE / "gap_manifest.py")
sys.path.insert(0, str(EXAMPLE))
adapter = _load("library_adapter", EXAMPLE / "adapter.py")
gap_binding = _load("gap_binding", EXAMPLE / "gap_binding.py")
ports = sys.modules["ports"]


def converted(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    recording = directory / "gap-signals.mcap"
    convert(EXAMPLE / "gap_mapping.json", EXAMPLE / "gap_signals.csv",
            recording)
    return recording


def library(build_dir: Path) -> Path:
    path = build_dir / "gap_monitor.so"
    assert path.is_file(), f"library build missing at {path}"
    return path


@pytest.fixture(scope="module")
def nominal(build_dir, sil_run, tmp_path_factory):
    workdir = tmp_path_factory.mktemp("gap")
    return run_simulation(
        manifest.gap_monitor_manifest(converted(workdir), library(build_dir)),
        runner=sil_run, workdir=workdir,
    )


def run_at_boundary(sil_run, build_dir, tmp_path, **options):
    ref = manifest.gap_monitor_manifest(
        converted(tmp_path), library(build_dir), **options,
    ).write(tmp_path / "gap.json")
    return subprocess.run(
        [str(sil_run), str(ref.path), "-o", str(tmp_path / "run.mcap"),
         "--participant-timeout-ms", "10000"],
        cwd=tmp_path, capture_output=True, text=True,
        env={**os.environ, "TMPDIR": str(tmp_path)},
    )


class TestNominal:
    def test_each_entry_point_publishes_its_outputs_at_its_own_steps(
        self, nominal
    ):
        gap = nominal.messages("monitor.gap")
        report = nominal.messages("monitor.report")
        assert [t for t, _ in gap] == list(range(0, 110 * MS, 10 * MS))
        assert [m["track_cycles"] for _, m in gap] == list(range(1, 12))
        # `report` has a 30 ms Period and a 10 ms offset.
        assert [t for t, _ in report] == [10 * MS, 40 * MS, 70 * MS, 100 * MS]
        assert [m["report_cycles"] for _, m in report] == [1, 2, 3, 4]

    def test_inputs_start_from_their_initial_values_and_are_held(
        self, nominal
    ):
        gap = dict(nominal.messages("monitor.gap"))
        # Before any Message: 50 m at 20 m/s. The ego speed of 0 ms is
        # visible at 10 ms, the radar object of 10 ms at 30 ms.
        assert gap[0] == {"time_gap_s": 2.5, "object_id": 0,
                          "track_cycles": 1}
        assert gap[10 * MS]["time_gap_s"] == 50 / 25
        assert gap[20 * MS]["time_gap_s"] == 50 / 25
        assert gap[30 * MS]["time_gap_s"] == 40 / 20
        assert gap[30 * MS]["object_id"] == 7
        # No new Message at 100 ms: both inputs keep their 90 ms values.
        assert gap[100 * MS]["time_gap_s"] == 36 / 24

    def test_the_last_message_of_a_burst_wins(self, nominal):
        radar = nominal.messages("radar.object")
        assert [(t, m["object_id"]) for t, m in radar if t == 40 * MS] == [
            (40 * MS, 7), (40 * MS, 8)]
        gap = dict(nominal.messages("monitor.gap"))
        # Both are visible at 60 ms; the second, 24 m to object 8, wins.
        assert gap[60 * MS]["time_gap_s"] == 24 / 16
        assert gap[60 * MS]["object_id"] == 8

    def test_simultaneous_entry_points_run_in_declared_order(self, nominal):
        report = dict(nominal.messages("monitor.report"))
        # `track` runs before `report` at 10 ms and 70 ms, so each report
        # includes the time gap of its own Step.
        assert report[10 * MS] == {"min_time_gap_s": 2.0, "warnings": 0,
                                   "report_cycles": 1}
        assert report[70 * MS] == {"min_time_gap_s": 24 / 30, "warnings": 1,
                                   "report_cycles": 3}

    def test_library_stdout_does_not_reach_the_protocol(
        self, sil_run, build_dir, tmp_path
    ):
        proc = run_at_boundary(sil_run, build_dir, tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert proc.stderr.count("gap_monitor: init") == 1
        assert ("gap_monitor: terminate after 11 track and 4 report cycles"
                in proc.stderr)
        assert not list(tmp_path.glob(".sil-run-*"))

    def test_the_run_repeats_byte_identically(
        self, sil_run, build_dir, tmp_path
    ):
        recording = converted(tmp_path / "recording")
        runs = []
        for attempt in ("first", "second"):
            workdir = tmp_path / attempt
            workdir.mkdir()
            runs.append(run_simulation(
                manifest.gap_monitor_manifest(recording, library(build_dir)),
                runner=sil_run, workdir=workdir))
        first, second = runs
        assert first.manifest_hash == second.manifest_hash
        assert first.mcap_path.read_bytes() == second.mcap_path.read_bytes()


GAP_FIELDS = manifest.OUTPUT_SCHEMAS["gap.TimeGap"]["fields"]


class TestManifestErrors:
    def test_an_entry_period_that_is_no_multiple_of_the_step_is_refused(
        self, sil_run, build_dir, tmp_path
    ):
        proc = run_at_boundary(sil_run, build_dir, tmp_path,
                               step_period_ns=20 * MS)
        assert proc.returncode == 2, proc.stderr
        assert ("entry point 'track': period_ns 10000000 is not a multiple "
                "of the Step Period 20000000 ns") in proc.stderr
        assert "gap_monitor: init" not in proc.stderr

    @pytest.mark.parametrize("fields, reason", [
        ([{**GAP_FIELDS[0], "type": "f32"}, *GAP_FIELDS[1:]], "type"),
        ([{**GAP_FIELDS[0], "count": 2}, *GAP_FIELDS[1:]], "count"),
        ([GAP_FIELDS[1], GAP_FIELDS[0], GAP_FIELDS[2]], "order"),
    ])
    def test_a_schema_that_does_not_match_the_port_is_refused(
        self, sil_run, build_dir, tmp_path, fields, reason
    ):
        proc = run_at_boundary(sil_run, build_dir, tmp_path,
                               schemas={"gap.TimeGap": {"fields": fields}})
        assert proc.returncode == 2, proc.stderr
        assert ("port 'gap' on channel 'monitor.gap': Schema 'gap.TimeGap' "
                "has fields") in proc.stderr
        assert "gap_monitor: init" not in proc.stderr


# --- the declarations, checked before the library loads -------------------

PORTS = {"ego": "ego.motion", "radar": "radar.object",
         "gap": "monitor.gap", "report": "monitor.report"}
INITIAL = {"ego.speed_mps": "20", "radar.range_m": "50",
           "radar.object_id": "0"}


def init_line(**changes) -> dict:
    schemas = {**manifest.MAPPING["schemas"], **manifest.OUTPUT_SCHEMAS}
    channels = {
        "ego.motion": {"direction": "in", "schema": "gap.Ego"},
        "radar.object": {"direction": "in", "schema": "gap.Radar"},
        "monitor.gap": {"direction": "out", "schema": "gap.TimeGap"},
        "monitor.report": {"direction": "out", "schema": "gap.Report"},
        **changes,
    }
    return {"channels": {c: v for c, v in channels.items() if v},
            "schemas": schemas}


def participant(binding=gap_binding.Binding, *, ports=PORTS,
                initial=INITIAL, period_ns=10 * MS):
    """A port participant over a library that is never loaded: every check
    here must fail before the adapter loads it."""
    module = type(sys)("binding_under_test")
    module.Binding = binding
    module.BindingError = gap_binding.BindingError
    return adapter.PortLibraryParticipant(
        library_path="unused.so", binding=module, ports=dict(ports),
        period_ns=period_ns, parameters={"warning_gap_s": 1.2},
        initial_inputs=dict(initial),
    )


def declaring(**declarations):
    return type("Declared", (gap_binding.Binding,), declarations)


def entry(name, period_ns, offset_ns=0):
    return ports.EntryPoint(name, period_ns, offset_ns)


TRACK = entry("track", 10 * MS)


class TestDeclarations:
    @pytest.mark.parametrize("entries, message", [
        ((entry("track", 0),), "period_ns must be greater than 0"),
        ((entry("track", 15 * MS),), "period_ns 15000000 is not a multiple"),
        ((TRACK, entry("report", 30 * MS, 30 * MS)),
         "offset_ns 30000000 must be at least 0 and less than period_ns"),
        ((TRACK, entry("report", 30 * MS, -10 * MS)),
         "offset_ns -10000000 must be at least 0"),
        ((TRACK, entry("report", 30 * MS, 5 * MS)),
         "offset_ns 5000000 is not a multiple of the Step Period"),
        ((TRACK, TRACK, entry("report", 30 * MS)),
         "entry point 'track' is declared twice"),
    ])
    def test_an_invalid_schedule_is_refused(self, entries, message):
        bad = participant(declaring(ENTRY_POINTS=entries))
        with pytest.raises(ManifestError, match=message):
            bad.on_init(init_line())

    def test_an_output_owned_by_an_unknown_entry_point_is_refused(self):
        outputs = (gap_binding.Binding.OUTPUT_PORTS[0],
                   gap_binding.Binding.OUTPUT_PORTS[1]._replace(entry="slow"))
        bad = participant(declaring(OUTPUT_PORTS=outputs))
        with pytest.raises(ManifestError, match=(
                "output port 'report' names entry point 'slow', which the "
                "binding does not declare")):
            bad.on_init(init_line())

    def test_a_port_name_declared_twice_is_refused(self):
        outputs = (*gap_binding.Binding.OUTPUT_PORTS,
                   gap_binding.Binding.OUTPUT_PORTS[0]._replace(name="ego"))
        bad = participant(declaring(OUTPUT_PORTS=outputs))
        with pytest.raises(ManifestError, match="port 'ego' is declared twice"):
            bad.on_init(init_line())

    @pytest.mark.parametrize("given, message", [
        ({k: v for k, v in PORTS.items() if k != "report"},
         r"the binding has ports \['ego', 'gap', 'radar', 'report'\], but "
         r"the command line binds \['ego', 'gap', 'radar'\]"),
        ({**PORTS, "report": "monitor.gap"},
         "ports 'gap' and 'report' are both bound to channel 'monitor.gap'"),
    ])
    def test_the_command_line_must_bind_every_port_once(self, given, message):
        with pytest.raises(ManifestError, match=message):
            participant(ports=given).on_init(init_line())

    def test_a_port_on_a_channel_of_the_wrong_direction_is_refused(self):
        line = init_line(**{"radar.object": {"direction": "out",
                                             "schema": "gap.Radar"}})
        with pytest.raises(ManifestError, match=(
                "input port 'radar' needs channel 'radar.object' declared "
                "'in' for this participant")):
            participant().on_init(line)

    def test_a_subscribed_channel_without_a_port_is_refused(self):
        line = init_line(**{"lane.info": {"direction": "in",
                                          "schema": "gap.Ego"}})
        with pytest.raises(ManifestError, match=(
                "channel 'lane.info' is declared for this participant, but "
                "no port binds it")):
            participant().on_init(line)

    @pytest.mark.parametrize("initial, message", [
        ({k: v for k, v in INITIAL.items() if k != "radar.object_id"},
         r"missing \['radar.object_id'\]"),
        ({**INITIAL, "radar.speed": "1"}, r"unknown \['radar.speed'\]"),
        ({**INITIAL, "radar.object_id": "0.5"},
         "initial value radar.object_id='0.5' is not a u32"),
    ])
    def test_every_input_field_needs_an_explicit_initial_value(
        self, initial, message
    ):
        with pytest.raises(ManifestError, match=message):
            participant(initial=initial).on_init(init_line())

    def test_a_valid_declaration_reaches_the_library_load(self):
        with pytest.raises(ManifestError,
                           match="cannot load library 'unused.so'"):
            participant().on_init(init_line())


@pytest.mark.parametrize("field, text, value", [
    (ports.Field("speeds", "f64", 2), "1.5,2", [1.5, 2.0]),
    (ports.Field("flags", "u8", 3), "0,1,255", bytes([0, 1, 255])),
])
def test_an_array_field_takes_count_initial_values(field, text, value):
    assert adapter._initial_value("p.x", field, text) == value


@pytest.mark.parametrize("field, text, message", [
    (ports.Field("speeds", "f64", 2), "1.5", "needs 2 comma-separated"),
    (ports.Field("flags", "u8", 2), "0,300", "is not a u8 array"),
])
def test_an_invalid_array_initial_value_is_a_manifest_error(
    field, text, message
):
    with pytest.raises(ManifestError, match=message):
        adapter._initial_value("p.x", field, text)


def test_a_period_other_than_the_configured_one_fails_the_step():
    with pytest.raises(ParticipantFailure, match="stepped every 20000000 ns"):
        participant().on_step(0, 20 * MS, [])
