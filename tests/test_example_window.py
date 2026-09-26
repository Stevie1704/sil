"""A selected replay window with a warm-up, into a stateful library (#185).

`examples/library/history.csv` is converted with `sil-csv`, and `sil-window`
selects a window of it. The `speed_filter` library is stateful: its output
depends on every input since its init. Its Run over the full history is the
reference; its Run over the window, warmed up by actual execution, must agree
with that reference over the evaluation interval. The negative control is the
same evaluation interval with no warm-up, which does not agree.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from conftest import ROOT

from sil.compare import compare, read_contract
from sil.csv_recording import convert
from sil.replay_window import prepare
from sil.testing import run_simulation

EXAMPLE = ROOT / "examples" / "library"
MS = 1_000_000


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manifest = _load("library_manifest", EXAMPLE / "manifest.py")
sys.modules.setdefault("manifest", manifest)
window_contract = _load("library_window_contract",
                        EXAMPLE / "window_contract.py")

WINDOW = json.loads((EXAMPLE / "window.json").read_text())


def library(build_dir: Path) -> Path:
    path = build_dir / "speed_filter.so"
    assert path.is_file(), f"library build missing at {path}"
    return path


def history(directory: Path) -> Path:
    recording = directory / "history.mcap"
    convert(EXAMPLE / "mapping.json", EXAMPLE / "history.csv", recording)
    return recording


def windowed(directory: Path, source: Path, name: str) -> tuple[dict, Path]:
    out = directory / f"{Path(name).stem}.mcap"
    return prepare(EXAMPLE / name, source, out), out


def run(sil_run, build_dir, directory: Path, recording: Path,
        duration_ns: int):
    """One Run; the in-run Test participant fails it at a wrong output."""
    directory.mkdir()
    return run_simulation(
        manifest.library_manifest(recording, library(build_dir),
                                  duration_ns=duration_ns),
        runner=sil_run, workdir=directory)


def compared(directory: Path, receipt: dict, actual, reference,
             from_ns: int | None = None) -> dict:
    """The example contract, optionally observed from an earlier time."""
    contract = window_contract.window_contract(receipt)
    if from_ns is not None:
        contract["evaluation"]["from_ns"] = from_ns
        for channel in contract["channels"].values():
            channel["observations"]["start_ns"] = from_ns
    path = directory / f"contract-{len(list(directory.glob('contract-*')))}.json"
    path.write_text(json.dumps(contract))
    return compare(read_contract(path), actual, reference)


@pytest.fixture(scope="module")
def runs(build_dir, sil_run, tmp_path_factory):
    workdir = tmp_path_factory.mktemp("window")
    source = history(workdir)
    receipt, recording = windowed(workdir, source, "window.json")
    bare_receipt, bare = windowed(workdir, source, "window-no-warm-up.json")
    return {
        "workdir": workdir,
        "receipt": receipt,
        "bare_receipt": bare_receipt,
        "full": run(sil_run, build_dir, workdir / "full", source,
                    WINDOW["end_ns"]),
        "window": run(sil_run, build_dir, workdir / "window", recording,
                      receipt["duration_ns"]),
        "bare": run(sil_run, build_dir, workdir / "bare", bare,
                    bare_receipt["duration_ns"]),
    }


def test_the_window_replays_the_history_rebased_to_its_origin(runs):
    origin = WINDOW["source_origin_ns"]
    expected = [(t - origin, m) for t, m in runs["full"].messages("ego.speed")
                if WINDOW["replay_start_ns"] <= t < WINDOW["end_ns"]]
    assert runs["window"].messages("ego.speed") == expected
    assert len(expected) == 200


def test_the_receipt_identifies_warm_up_and_evaluation(runs):
    receipt = runs["receipt"]
    assert receipt["intervals"]["warm_up"]["virtual"] == {
        "start_ns": 0, "end_ns": 1000 * MS}
    assert receipt["intervals"]["evaluation"]["source"] == {
        "start_ns": 1500 * MS, "end_ns": 2500 * MS}
    assert receipt["channels"]["ego.speed"]["warm_up"]["messages"] == 100
    assert receipt["channels"]["ego.speed"]["evaluation"]["messages"] == 100
    assert receipt["duration_ns"] == 2000 * MS


def test_a_warmed_up_window_agrees_with_the_full_history(runs):
    report = compared(runs["workdir"], runs["receipt"],
                      runs["window"].mcap_path, runs["full"].mcap_path)
    assert report["verdict"] == "pass", report["first_divergence"]
    for channel in manifest.INSTANCES:
        assert report["channels"][channel]["checked"] == 100


def test_the_warm_up_itself_does_not_agree(runs):
    """The warm-up is excluded for a reason: its outputs still carry the
    library's initial state, so a comparison that includes them fails."""
    report = compared(runs["workdir"], runs["receipt"],
                      runs["window"].mcap_path, runs["full"].mcap_path,
                      from_ns=0)
    contract = window_contract.window_contract(runs["receipt"])
    assert report["verdict"] == "fail"
    assert report["first_divergence"]["observation_ns"] < (
        contract["evaluation"]["from_ns"])


def test_no_warm_up_changes_the_result(runs):
    """The negative control: the same evaluation interval, entered cold."""
    report = compared(runs["workdir"], runs["bare_receipt"],
                      runs["bare"].mcap_path, runs["full"].mcap_path)
    assert report["verdict"] == "fail"
    first = report["first_divergence"]
    assert (first["channel"], first["observation_ns"]) == ("filter.fast", 0)
    assert first["abs_error"] > 1


def test_preparation_and_run_repeat_byte_identically(
    build_dir, sil_run, tmp_path
):
    """Each attempt prepares into the same path, so the two Manifests are the
    same bytes exactly when the two preparations are."""
    source = history(tmp_path)
    recordings, results = [], []
    for attempt in ("first", "second"):
        receipt, recording = windowed(tmp_path, source, "window.json")
        recordings.append(recording.read_bytes())
        results.append(run(sil_run, build_dir, tmp_path / attempt, recording,
                           receipt["duration_ns"]))
    first, second = results
    assert recordings[0] == recordings[1]
    assert first.manifest_hash == second.manifest_hash
    assert first.mcap_path.read_bytes() == second.mcap_path.read_bytes()


def test_a_changed_window_is_a_different_identified_input(runs):
    assert (runs["bare_receipt"]["recording"]["sha256"]
            != runs["receipt"]["recording"]["sha256"])
    assert runs["bare"].manifest_hash != runs["window"].manifest_hash
