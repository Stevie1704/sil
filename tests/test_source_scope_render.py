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
import tracemalloc

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


def test_maximum_scope_map_can_be_published_and_rendered(tmp_path, capsys,
                                                       monkeypatch):
    scopes = [{"id": i, "name": "x" * 63,
               "sites": [{"file": "x.c", "line": i}]}
              for i in range(1, 4097)]
    scope_map = {**MAP, "scopes": scopes}
    assert len(encode_map(scope_map)) <= 2 << 20

    report_path = capture_dir(tmp_path, "complete", scope_map=scope_map)
    assert report_path.stat().st_size <= 1 << 20
    report = json.loads(report_path.read_text())
    assert len(report["summary"]["scopes"]) == 4096
    assert report["summary"]["scopes"][-1]["attributed_calls"] == 0
    code, out, err = render([report_path], capsys, tmp_path, monkeypatch)
    assert code == 0, err
    assert "capture: complete" in out and "4096" in out


def test_nested_call_html_memory_is_bounded_by_the_display_limit(tmp_path):
    from sil.source_scope_html import render_scopes
    from sil.source_scope_set import load_scope_set, select_scope_activations
    from test_source_scope_decoding import raw, stream

    pairs = 10000
    # A valid recursive Activation, decoded from the actual raw layout.
    records = [(i, 42, 1, 0) for i in range(pairs)]
    records += [(pairs + i, 42, 1, 1) for i in range(pairs)]
    scopes = capture_dir(tmp_path, "complete", events=raw(records),
                         observations=stream((0, 2 * pairs)))
    scope_set = select_scope_activations(load_scope_set(scopes),
                                         participant="adas", task="control", index=0)
    # Measure presentation after loading; the bounded file reader can
    # reserve its full 256 MiB read limit independently of rendering.
    tracemalloc.start()
    try:
        source = render_scopes(scope_set)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    page = Page(source)
    assert "9500 more calls not shown" in page.text
    # Formatting all 10,000 depths before truncation consumes over 100 MiB.
    assert peak < 64 << 20


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


@pytest.mark.parametrize("side", ["base", "new"])
def test_diff_rejects_a_scope_report(tmp_path, capsys, side):
    scopes = capture_dir(tmp_path, "complete")
    other = tmp_path / "profile.json"
    other.write_text("{}")
    argv = [scopes, other] if side == "base" else [other, scopes]

    code = profile_diff.main([str(a) for a in argv])

    captured = capsys.readouterr()
    assert (code, captured.out) == (2, "")
    assert "source-scope" in captured.err


def html_page(tmp_path, capsys, monkeypatch, scopes: Path, *argv) -> Page:
    code, out, err = render([scopes, "--html", "out.html", *argv], capsys,
                            tmp_path, monkeypatch)
    assert code == 0, err
    assert "HTML report: out.html" in out
    return Page((tmp_path / "out.html").read_text())


EXPECTED_STATUS = {"complete": "complete", "crossing": "complete",
                   "identity-mismatch": "failed", "loss": "partial",
                   "recursion": "complete", "unattributed": "complete",
                   "unbalanced": "partial"}


@pytest.mark.parametrize("case", CASES)
def test_every_fixture_case_renders_offline_with_its_limitations(
        tmp_path, capsys, monkeypatch, case):
    page = html_page(tmp_path, capsys, monkeypatch, capture_dir(tmp_path, case))

    assert f"Capture {EXPECTED_STATUS[case]}" in page.text
    for limitation in LIMITATIONS:
        assert limitation in page.text
    policy = page.find("meta", **{"http-equiv": "Content-Security-Policy"})
    assert policy and "default-src 'none'" in policy[0]["content"]
    for _, attrs in page.elements:
        for key in ("src", "href"):
            assert "//" not in attrs.get(key, "").split(",")[0][:12]
    assert page.find("button", id="theme")
    assert all(a.get("tabindex") == "0"
               for a in page.find("div", **{"class": "table-wrap"}))


def test_html_lists_exact_and_unavailable_activations(tmp_path, capsys,
                                                      monkeypatch):
    page = html_page(tmp_path, capsys, monkeypatch,
                     capture_dir(tmp_path, "unbalanced"))

    assert "unavailable (unbalanced_scopes)" in page.text
    assert "1 withheld call" in page.text
    assert "exact, 1 call" in page.text
    assert "Instrumented build" in page.text and ORIGINAL["sha256"] in page.text


def test_html_call_order_of_a_selected_exact_activation(tmp_path, capsys,
                                                        monkeypatch):
    page = html_page(tmp_path, capsys, monkeypatch,
                     capture_dir(tmp_path, "complete"), *SELECT_0)

    assert page.find("table", id="calls")
    assert "Participant adas, Task control, Activation index 0" in page.text
    text = page.text[page.text.index("Calls in entry order"):]
    assert text.index("select_lead") < text.index("confirmed") \
        < text.index("confirms")


def test_html_of_a_selected_unavailable_activation_has_no_counts(
        tmp_path, capsys, monkeypatch):
    page = html_page(tmp_path, capsys, monkeypatch,
                     capture_dir(tmp_path, "unbalanced"), *SELECT_0)

    assert not page.find("table", id="calls")
    assert "unavailable (unbalanced_scopes), 1 withheld call" in page.text


def test_malicious_names_are_text(tmp_path, capsys, monkeypatch):
    name = '</script><img src=x onerror=alert(1)>\x1b[31m'
    scope_map = {**MAP, "scopes": [{**MAP["scopes"][3], "name": name}]}
    scopes = capture_dir(tmp_path, "complete", scope_map=scope_map,
                         events=_only_scope_4())

    page = html_page(tmp_path, capsys, monkeypatch, scopes, *SELECT_0)
    code, out, _ = render([scopes, *SELECT_0], capsys, tmp_path, monkeypatch)

    assert not page.find("img")
    assert '</script><img src=x onerror=alert(1)>' in page.text
    assert code == 0 and "\x1b" not in out and "\\x1b[31m" in out


def _only_scope_4() -> bytes:
    """The complete case's raw file with only its scope-4 records kept."""
    import struct
    data = bytearray((FIXTURES / "complete/events.bin").read_bytes())
    record = struct.Struct("<QIHBB")
    kept = [record.unpack_from(data, 256 + 16 * i)
            for i in range(struct.unpack_from("<Q", data, 48)[0])]
    kept = [r for r in kept if r[2] == 4]
    body = b"".join(record.pack(*r) for r in kept)
    capacity = struct.unpack_from("<Q", data, 40)[0]
    struct.pack_into("<Q", data, 48, len(kept))
    return bytes(data[:256]) + body + bytes(16 * capacity - len(body))


def test_unattributed_rows_are_bounded_with_a_remainder(tmp_path, capsys,
                                                        monkeypatch):
    def crowded(calls):
        row = {"scope": 1, "tid": 43, "entry_ns": "1", "return_ns": "2",
               "reason": "worker_thread"}
        calls["unattributed"] = {"reasons": {"worker_thread": 1100},
                                 "rows": [row] * 1000, "rows_remainder": 100}
        calls["totals"]["unattributed"] = 1100
        return calls
    scopes = capture_dir(tmp_path, "complete", calls_hook=crowded)

    page = html_page(tmp_path, capsys, monkeypatch, scopes)

    rows = [a for t, a in page.elements if t == "tr"]
    assert "600 more rows not shown" in page.text
    assert len(rows) < 600


def test_a_large_integer_keeps_every_digit(tmp_path, capsys, monkeypatch):
    def large(calls):
        call = calls["activations"][0]["calls"][0]
        call["elapsed_ns"] = str(2 ** 64 - 1)
        return calls
    scopes = capture_dir(tmp_path, "complete", calls_hook=large)

    page = html_page(tmp_path, capsys, monkeypatch, scopes, *SELECT_0)

    assert str(2 ** 64 - 1) in page.text


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
