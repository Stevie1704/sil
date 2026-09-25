"""The shared-library adoption example at the run boundary (issue #184).

Recorded CSV speed values are replayed into two Process participants that each
load `speed_filter`, a library with its own C API and no SiL entry point,
through `examples/library/adapter.py`. The in-run Test participant computes every
output independently; these tests state the first outputs by hand from
`signals.csv` and the Manifest's parameters, and drive the failing paths with
the library's fault builds from `tests/fixtures/speed_filter_fault.c`.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest
from conftest import ROOT

from sil.csv_recording import convert
from sil.participant import ParticipantFailure
from sil.testing import run_simulation

EXAMPLE = ROOT / "examples" / "library"
MS = 1_000_000


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manifest = _load("library_manifest", EXAMPLE / "manifest.py")


def converted(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    recording = directory / "signals.mcap"
    convert(EXAMPLE / "mapping.json", EXAMPLE / "signals.csv", recording)
    return recording


def library(build_dir: Path, variant: str = "") -> Path:
    path = build_dir / f"speed_filter{'_' + variant if variant else ''}.so"
    assert path.is_file(), f"library build missing at {path}"
    return path


@pytest.fixture(scope="module")
def nominal(build_dir, sil_run, tmp_path_factory):
    workdir = tmp_path_factory.mktemp("library")
    return run_simulation(
        manifest.library_manifest(converted(workdir), library(build_dir)),
        runner=sil_run, workdir=workdir,
    )


def run_at_boundary(sil_run, build_dir, tmp_path, *, variant="",
                    instances=manifest.INSTANCES, timeout_ms=None):
    """One Run in its own invocation and region directory, so the cleanup
    of the Run working directory and the mapped regions is observable."""
    regions = tmp_path / "regions"
    regions.mkdir()
    ref = manifest.library_manifest(
        converted(tmp_path), library(build_dir, variant), instances,
    ).write(tmp_path / "library.json")
    command = [str(sil_run), str(ref.path), "-o", str(tmp_path / "run.mcap")]
    if timeout_ms is not None:
        command += ["--participant-timeout-ms", str(timeout_ms)]
    proc = subprocess.run(command, cwd=tmp_path, capture_output=True,
                          text=True, env={**os.environ, "TMPDIR": str(regions)})
    proc.regions = regions
    return proc


def assert_cleaned_up(proc, tmp_path: Path, library_path: Path) -> None:
    assert not list(tmp_path.glob(".sil-run-*"))
    assert not list(proc.regions.iterdir())
    survivors = subprocess.run(["pgrep", "-f", str(library_path)],
                               capture_output=True, text=True)
    assert survivors.stdout == ""


class TestNominal:
    def test_each_instance_starts_a_fresh_lifecycle_with_its_own_state(
        self, nominal
    ):
        fast = nominal.messages("filter.fast")
        slow = nominal.messages("filter.slow")
        steps = list(range(0, 110 * MS, 10 * MS))
        assert [t for t, _ in fast] == [t for t, _ in slow] == steps
        assert [m["cycles"] for _, m in fast] == list(range(1, 12))
        assert [m["cycles"] for _, m in slow] == list(range(1, 12))
        # Gain 1/3 from 0 and gain 1/6 from 9, both on the initial 8 m/s
        # until the first recorded Message, published at 20 ms, is visible.
        assert [m["filtered_speed_mps"] for _, m in fast[:4]] == pytest.approx(
            [8 / 3, 40 / 9, 152 / 27, 152 / 27 + (10 - 152 / 27) / 3])
        assert [m["filtered_speed_mps"] for _, m in slow[:2]] == pytest.approx(
            [9 - 1 / 6, (9 - 1 / 6) + (8 - (9 - 1 / 6)) / 6])

    def test_the_replayed_input_is_the_recording(self, nominal):
        assert nominal.messages("ego.speed") == [
            (t * MS, {"speed_mps": v}) for t, v in
            [(20, 10.0), (30, 11.0), (40, 12.5), (60, 13.0), (70, 13.0),
             (80, 12.0)]
        ]

    def test_library_stdout_does_not_reach_the_protocol(
        self, sil_run, build_dir, tmp_path
    ):
        proc = run_at_boundary(sil_run, build_dir, tmp_path)
        assert proc.returncode == 0, proc.stderr
        # Every line the library printed is still visible, on stderr.
        assert proc.stderr.count("speed_filter: init") == 2
        assert proc.stderr.count("speed_filter: cycle 11") == 2
        assert proc.stderr.count("speed_filter: terminate after 11") == 2
        assert_cleaned_up(proc, tmp_path, library(build_dir))

    def test_the_run_repeats_byte_identically(
        self, sil_run, build_dir, tmp_path
    ):
        recording = converted(tmp_path / "recording")
        runs = []
        for attempt in ("first", "second"):
            workdir = tmp_path / attempt
            workdir.mkdir()
            runs.append(run_simulation(
                manifest.library_manifest(recording, library(build_dir)),
                runner=sil_run, workdir=workdir))
        first, second = runs
        assert first.manifest_hash == second.manifest_hash
        assert first.mcap_path.read_bytes() == second.mcap_path.read_bytes()


class TestIncorrectOutput:
    def test_the_test_participant_fails_the_run_at_the_first_difference(
        self, sil_run, build_dir, tmp_path
    ):
        proc = run_at_boundary(sil_run, build_dir, tmp_path, variant="defect")
        assert proc.returncode == 1, proc.stderr
        # Forward-Euler gain 1/2 instead of 1/3 on the first cycle.
        assert ("filter.fast at t=0 ns: filtered_speed_mps 4.0 differs from "
                "the independently computed 2.666666666666667") in proc.stderr
        assert_cleaned_up(proc, tmp_path, library(build_dir, "defect"))


class TestStartup:
    def test_a_missing_symbol_is_a_manifest_error_naming_it(
        self, sil_run, build_dir, tmp_path
    ):
        proc = run_at_boundary(sil_run, build_dir, tmp_path,
                               variant="no_terminate")
        assert proc.returncode == 2, proc.stderr
        assert "does not export 'speed_filter_terminate'" in proc.stderr
        assert str(library(build_dir, "no_terminate")) in proc.stderr
        assert_cleaned_up(proc, tmp_path, library(build_dir, "no_terminate"))

    def test_an_invalid_configuration_is_a_manifest_error_with_the_reason(
        self, sil_run, build_dir, tmp_path
    ):
        instances = {
            **manifest.INSTANCES,
            "filter.slow": {"time_constant_s": -0.05,
                            "initial_speed_mps": 9.0},
        }
        proc = run_at_boundary(sil_run, build_dir, tmp_path,
                               instances=instances)
        assert proc.returncode == 2, proc.stderr
        assert "rejected its configuration" in proc.stderr
        assert ("speed_filter_init returned -2: time_constant_s must be "
                "finite and at least 0") in proc.stderr
        assert_cleaned_up(proc, tmp_path, library(build_dir))

    def test_a_library_that_does_not_load_is_a_manifest_error(
        self, sil_run, tmp_path
    ):
        missing = tmp_path / "absent.so"
        ref = manifest.library_manifest(
            converted(tmp_path), missing).write(tmp_path / "library.json")
        proc = subprocess.run([str(sil_run), str(ref.path), "--no-recording"],
                              cwd=tmp_path, capture_output=True, text=True)
        assert proc.returncode == 2, proc.stderr
        assert f"cannot load library {str(missing)!r}" in proc.stderr


class TestCrashAndHang:
    def test_a_library_crash_fails_the_run(
        self, sil_run, build_dir, tmp_path
    ):
        proc = run_at_boundary(sil_run, build_dir, tmp_path, variant="crash")
        assert proc.returncode == 1, proc.stderr
        assert "filter_fast" in proc.stderr
        assert_cleaned_up(proc, tmp_path, library(build_dir, "crash"))

    def test_a_library_hang_fails_the_run_at_the_response_deadline(
        self, sil_run, build_dir, tmp_path
    ):
        proc = run_at_boundary(sil_run, build_dir, tmp_path, variant="hang",
                               timeout_ms=500)
        assert proc.returncode == 1, proc.stderr
        assert "timeout" in proc.stderr
        assert "virtual time 20000000 ns" in proc.stderr
        assert_cleaned_up(proc, tmp_path, library(build_dir, "hang"))


def test_a_period_other_than_the_configured_one_fails_the_step(monkeypatch):
    monkeypatch.syspath_prepend(str(EXAMPLE))
    adapter = _load("library_adapter", EXAMPLE / "adapter.py")
    participant = adapter.LibraryParticipant(
        library_path="unused.so", input_channel="ego.speed",
        output_channel="filter.fast", period_ns=10 * MS,
        parameters={}, initial_inputs={},
    )
    with pytest.raises(ParticipantFailure, match="stepped every 20000000 ns"):
        participant.on_step(0, 20 * MS, [])

