"""`sil profile render` of a source-scope capture (#373).

docs/proposals/native-source-scopes.md, "Presentation", is the contract.
Each test makes a capture directory from a worked example in
tests/fixtures/source_scopes/ with the #371 decoder and document
functions, as the capture command (#372) writes it, and runs the command
as a user does. The HTML is read with the standard library's parser, as a
browser without JavaScript would (Q23, Q24).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from sil import profile_diff, profile_render
from sil.source_scope_decode import CaptureFailed, decode
from sil.source_scope_report import (
    LIMITATIONS, capture_record, encode_calls, encode_map, encode_report,
    scope_report,
)
from test_diagnostics_html import Page

FIXTURES = Path(__file__).resolve().parent / "fixtures/source_scopes"
CASES = sorted(path.name for path in FIXTURES.iterdir() if path.is_dir())
MAP = json.loads((FIXTURES / "map.json").read_text())
CAPTURE_ID = "6c1f8e0a-2b7d-4f3e-9a51-0d2c4b6e8f10"
ORIGINAL = {"path": "/work/build/libadas_reference.so",
            "build_id": "00112233445566778899aabbccddeeff00112233",
            "sha256": "ab" * 32}


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def capture_dir(root: Path, case: str, *, scope_map: dict = MAP,
                events: bytes | None = None, observations: str | None = None,
                calls_hook=None) -> Path:
    """`DIR` of one fixture case, as the capture command writes it; returns
    the path of its `scopes.json`."""
    folder = FIXTURES / case
    events = (folder / "events.bin").read_bytes() if events is None else events
    if observations is None:
        observations = (folder / "observations.jsonl").read_text()
    out = root / case
    files = {"scope_map": ("scopes/map.json", encode_map(scope_map)),
             "scope_events": ("scopes/events.bin", events),
             "activation_observations": ("activation/observations.jsonl",
                                         observations.encode())}
    failure = calls = None
    try:
        calls = decode(events, observations, scope_map, participant="adas",
                       capture_id=CAPTURE_ID,
                       library_sha256=scope_map["library"]["sha256"])
    except CaptureFailed as failed:
        failure = failed
    missing = [{"role": "recording", "path": "run.mcap",
                "reason": "not_requested"}]
    if calls is not None:
        if calls_hook is not None:
            calls = calls_hook(calls)
        files["scope_calls"] = ("scopes/calls.json", encode_calls(calls))
    else:
        missing.append({"role": "scope_calls", "path": "scopes/calls.json",
                        "reason": "not_written"})
    artifacts = []
    for role, (path, data) in files.items():
        _write(out / path, data)
        artifacts.append({"role": role, "path": path, "bytes": len(data),
                          "sha256": hashlib.sha256(data).hexdigest()})
    report = scope_report(
        sil_version="0.2.1",
        configuration={"manifest_path": "adas.yaml", "manifest_sha256": "cd" * 32,
                       "participant": "adas", "capacity": 65536,
                       "guards": {}, "provenance_mode": "full"},
        run={"status": "succeeded", "exit_code": 0, "signal": None,
             "failure": None, "diagnostics_report": None,
             "supervisor_signal": None},
        capture=capture_record(events, calls, failure=failure),
        identities={"instrumented": scope_map["library"], "original": ORIGINAL,
                    "observer_version": 1,
                    "compiler": "GCC: (Ubuntu 12.3.0) 12.3.0"},
        artifacts=artifacts, missing=missing, scope_map=scope_map,
        calls=calls)
    _write(out / "scopes.json", encode_report(report))
    return out / "scopes.json"


def render(argv: list, capsys, cwd: Path, monkeypatch) -> tuple[int, str, str]:
    monkeypatch.chdir(cwd)
    code = profile_render.main([str(a) for a in argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_render_prints_statuses_identities_counts_and_limitations(
        tmp_path, capsys, monkeypatch):
    scopes = capture_dir(tmp_path, "complete")

    code, out, err = render([scopes], capsys, tmp_path, monkeypatch)

    assert code == 0, err
    assert "run: succeeded, exit code 0" in out
    assert "capture: complete" in out
    assert MAP["library"]["sha256"] in out and ORIGINAL["sha256"] in out
    assert "loss: dropped_full 0, dropped_contended 0, clock_errors 0" in out
    assert "adas/control: 2 Activations, 2 exact, 0 unavailable" in out
    assert "select_lead" in out and "check_object" in out
    for limitation in LIMITATIONS:
        assert limitation in out


def test_a_failed_capture_shows_its_reason_and_unavailable_files(
        tmp_path, capsys, monkeypatch):
    scopes = capture_dir(tmp_path, "identity-mismatch")

    code, out, err = render([scopes], capsys, tmp_path, monkeypatch)

    assert code == 0, err
    assert "capture: failed (identity_mismatch)" in out
    assert "scopes/calls.json: unavailable (not_written)" in out
    assert "run.mcap: unavailable (not_requested)" in out
    for limitation in LIMITATIONS:
        assert limitation in out


def test_unattributed_calls_are_counted_by_reason(tmp_path, capsys,
                                                  monkeypatch):
    scopes = capture_dir(tmp_path, "unattributed")

    code, out, err = render([scopes], capsys, tmp_path, monkeypatch)

    assert code == 0, err
    assert ("unattributed: 4 (outside_activations 1, worker_thread 1, "
            "crossing_interval 1, other_participant 1)") in out


def _relist(scopes: Path, name: str, data: bytes) -> None:
    """Replace one artifact and record its new size and SHA-256, so only
    its content is wrong."""
    (scopes.parent / name).write_bytes(data)
    report = json.loads(scopes.read_text())
    for artifact in report["artifacts"]:
        if artifact["path"] == name:
            artifact.update(bytes=len(data),
                            sha256=hashlib.sha256(data).hexdigest())
    scopes.write_text(json.dumps(report))


@pytest.mark.parametrize("name", ["scopes/events.bin", "scopes/map.json",
                                  "scopes/calls.json",
                                  "activation/observations.jsonl"])
def test_a_changed_artifact_fails_before_any_output(tmp_path, capsys,
                                                    monkeypatch, name):
    scopes = capture_dir(tmp_path, "complete")
    path = scopes.parent / name
    path.write_bytes(path.read_bytes()[:-1] + b" ")

    code, out, err = render([scopes, "--html", "out.html"], capsys, tmp_path,
                            monkeypatch)

    assert (code, out) == (2, "")
    assert name in err and "SHA-256" in err
    assert not (tmp_path / "out.html").exists()


def test_a_missing_artifact_fails_before_any_output(tmp_path, capsys,
                                                    monkeypatch):
    scopes = capture_dir(tmp_path, "complete")
    (scopes.parent / "scopes/events.bin").unlink()

    code, out, _ = render([scopes], capsys, tmp_path, monkeypatch)

    assert (code, out) == (2, "")


@pytest.mark.parametrize("name, data", [
    ("scopes/calls.json", b'{"sil_source_scope_calls": 2}'),
    ("scopes/map.json", b'{"sil_source_scope_map": 2}'),
    ("scopes/events.bin", b"SILSCOPE\x02\x00" + bytes(246)),
    ("activation/observations.jsonl", b'{"sil_activation_observations": 2}\n'),
], ids=["calls", "map", "events", "observations"])
def test_an_unknown_artifact_version_fails(tmp_path, capsys, monkeypatch,
                                           name, data):
    scopes = capture_dir(tmp_path, "complete")
    _relist(scopes, name, data)

    code, out, err = render([scopes], capsys, tmp_path, monkeypatch)

    assert (code, out) == (2, "")
    assert name in err


@pytest.mark.parametrize("change", [
    lambda r: r.update(sil_source_scope_report=2),
    lambda r: r.pop("summary"),
    lambda r: r["capture"].update(status=7),
    lambda r: r["artifacts"].append({"role": "x", "path": "../outside",
                                     "bytes": 0, "sha256": "0" * 64}),
], ids=["version", "summary", "status", "outside"])
def test_an_invalid_report_fails(tmp_path, capsys, monkeypatch, change):
    scopes = capture_dir(tmp_path, "complete")
    report = json.loads(scopes.read_text())
    change(report)
    scopes.write_text(json.dumps(report))

    code, out, _ = render([scopes], capsys, tmp_path, monkeypatch)

    assert (code, out) == (2, "")


def test_calls_of_another_capture_fail(tmp_path, capsys, monkeypatch):
    def other(calls):
        return {**calls, "capture_id": "00000000-0000-4000-8000-000000000000"}
    scopes = capture_dir(tmp_path, "complete", calls_hook=other)

    code, out, err = render([scopes], capsys, tmp_path, monkeypatch)

    assert (code, out) == (2, "")
    assert "capture" in err


SELECT_0 = ["--participant", "adas", "--task", "control",
            "--activation-index", "0"]


def test_a_selected_exact_activation_lists_its_calls_by_depth(
        tmp_path, capsys, monkeypatch):
    scopes = capture_dir(tmp_path, "complete")

    code, out, err = render([scopes, *SELECT_0], capsys, tmp_path, monkeypatch)

    assert code == 0, err
    assert "Participant adas, Task control, Activation index 0" in out
    assert "adas/control index 0, Virtual time 0 ns: exact, 3 calls" in out
    lines = out.splitlines()
    start = lines.index("  calls in entry order (depth, scope, elapsed host ns):")
    assert lines[start + 1:start + 4] == [
        "    0 select_lead (4) 90",
        "      1 confirmed (2) 30",
        "        2 confirms (3) 10"]


def test_a_selected_unavailable_activation_shows_reason_not_counts(
        tmp_path, capsys, monkeypatch):
    scopes = capture_dir(tmp_path, "unbalanced")

    code, out, err = render([scopes, *SELECT_0], capsys, tmp_path, monkeypatch)

    assert code == 0, err
    assert ("adas/control index 0, Virtual time 0 ns: unavailable "
            "(unbalanced_scopes), 1 withheld call") in out
    assert "calls in entry order" not in out


def test_the_call_order_shows_at_most_500_rows_and_a_remainder(
        tmp_path, capsys, monkeypatch):
    def many(calls):
        first = calls["activations"][0]
        call = {**first["calls"][0], "depth": 0, "parent": None}
        first.update(calls=[call] * 600, counts={"4": 600})
        return calls
    scopes = capture_dir(tmp_path, "complete", calls_hook=many)

    code, out, err = render([scopes, *SELECT_0], capsys, tmp_path, monkeypatch)

    assert code == 0, err
    assert out.count("    0 select_lead (4) 90") == 500
    assert "  100 more calls not shown" in out


def test_a_time_range_lists_the_selected_activations(
        tmp_path, capsys, monkeypatch):
    scopes = capture_dir(tmp_path, "complete")

    code, out, err = render([scopes, "--from", "0", "--to", "20000000"],
                            capsys, tmp_path, monkeypatch)

    assert code == 0, err
    assert "selected Activations: 2" in out
    assert "adas/control index 1, Virtual time 10000000 ns: exact, 2 calls" in out
    assert "calls in entry order" not in out


@pytest.mark.parametrize("argv", [
    ["--task", "control"],
    ["--participant", "adas", "--activation-index", "0"],
    ["--participant", "other"],
    ["--participant", "adas", "--task", "control", "--activation-index", "0",
     "--from", "0"],
    ["--from", "5", "--to", "5"],
    ["--diagnostics", "run.diagnostics.json"],
], ids=["task-alone", "index-without-task", "absent", "index-and-range",
        "empty-range", "diagnostics"])
def test_invalid_selections_exit_2(tmp_path, capsys, monkeypatch, argv):
    scopes = capture_dir(tmp_path, "complete")

    code, out, _ = render([scopes, *argv], capsys, tmp_path, monkeypatch)

    assert (code, out) == (2, "")


def test_selection_needs_decoded_calls(tmp_path, capsys, monkeypatch):
    scopes = capture_dir(tmp_path, "identity-mismatch")

    code, out, err = render([scopes, *SELECT_0], capsys, tmp_path, monkeypatch)

    assert (code, out) == (2, "")
    assert "calls" in err
