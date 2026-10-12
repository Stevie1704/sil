"""`sil manifest validate`: every Manifest error at once, before a Run (#275)."""

import copy
import hashlib
import json
import os
import subprocess
import sys

import pytest

from conftest import BUILD_DIR, ROOT

from sil import manifest_validate

VALID = {
    "sil_manifest": 1,
    "duration_ns": 1_000_000,
    "schemas": {"toy.Counter": {"fields": [{"name": "seq", "type": "u64"}]}},
    "channels": {"ticks": {"schema": "toy.Counter"}},
    "participants": {},
}


def _validate(tmp_path, doc, capsys, *extra):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(doc) if not isinstance(doc, str) else doc)
    code = manifest_validate.main([str(path), "--json", *extra])
    return code, json.loads(capsys.readouterr().out)


def test_a_valid_manifest_exits_0_with_no_errors(tmp_path, capsys):
    code, report = _validate(tmp_path, VALID, capsys)

    assert code == 0
    assert report["sil_manifest_validation"] == 1
    assert report["valid"] is True
    assert report["errors"] == []


def test_every_error_is_listed_with_its_path_and_a_hint(tmp_path, capsys):
    doc = {k: v for k, v in VALID.items() if k != "sil_manifest"}
    doc["extra"] = 1
    doc["channels"] = {"ticks": {"schema": "toy.Missing"}}

    code, report = _validate(tmp_path, doc, capsys)

    assert code == 2
    assert report["valid"] is False
    paths = [error["path"] for error in report["errors"]]
    assert paths == ["/extra", "/sil_manifest", "/channels/ticks/schema"]
    assert all(error["message"] and error["hint"] for error in report["errors"])


def test_without_json_each_error_is_one_line_with_its_hint(tmp_path, capsys):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({**VALID, "duration_ns": 0}))

    assert manifest_validate.main([str(path)]) == 2

    err = capsys.readouterr().err
    assert err.startswith(f"{path}: /duration_ns: expected an integer >= 1")
    assert "\n  hint: " in err


def test_an_override_without_field_and_value_reports_both(tmp_path, capsys):
    doc = copy.deepcopy(VALID)
    doc["channels"]["ticks"]["interceptors"] = [{"kind": "override"}]

    code, report = _validate(tmp_path, doc, capsys)

    assert code == 2
    assert [error["path"] for error in report["errors"]] == [
        "/channels/ticks/interceptors/0/field",
        "/channels/ticks/interceptors/0/value",
    ]


# Parity with sil-run (#275): one corpus, two checkers, one verdict. Each case
# is one change to a valid base Manifest. sil-run rejects a Manifest with exit
# 2; validate must reject exactly the same cases, and accept the others.

ECHO = str(ROOT / "tests" / "participants" / "echo.py")
ACCUM_FIELDS = [{"name": "count", "type": "u64"}, {"name": "sum", "type": "i64"}]


def _base() -> dict:
    return {
        "sil_manifest": 1,
        "duration_ns": 20_000_000,
        "schemas": {
            "toy.Counter": {"fields": [{"name": "seq", "type": "u64"},
                                       {"name": "value", "type": "i64"}]},
            "toy.Accum": {"fields": copy.deepcopy(ACCUM_FIELDS)},
        },
        "channels": {"ticks": {"schema": "toy.Counter"},
                     "sums": {"schema": "toy.Accum"},
                     "echo": {"schema": "toy.Counter"}},
        "participants": {
            "producer": {
                "type": "native", "library": str(BUILD_DIR / "toy_producer.silp"),
                "config": {"channel": "ticks", "period_ns": 10_000_000},
                "subscribes": [], "publishes": ["ticks"]},
            "acc": {
                "type": "native",
                "library": str(BUILD_DIR / "toy_accumulator.silp"),
                "config": {"input": "ticks", "output": "sums"},
                "subscribes": [{"channel": "ticks", "capacity": 64,
                                "overflow": "fail"}],
                "publishes": ["sums"]},
            "echo": {
                "type": "process", "command": [sys.executable, ECHO],
                "step_period_ns": 10_000_000,
                "subscribes": [{"channel": "ticks", "capacity": 64,
                                "overflow": "fail"}],
                "publishes": ["echo"], "priority": 0},
        },
    }


def _set(path, value):
    def mutate(doc):
        *parents, last = path
        for key in parents:
            doc = doc[key]
        doc[last] = value
    return mutate


def _drop(*path):
    def mutate(doc):
        *parents, last = path
        for key in parents:
            doc = doc[key]
        del doc[last]
    return mutate


P = "participants"
C = "channels"
S = "schemas"
TICKS_INTERCEPTORS = (C, "ticks", "interceptors")

VALID_CASES = {
    "base": lambda doc: None,
    "epoch": _set(("epoch_ns",), 5),
    "latency 0": _set((C, "ticks", "latency_ns"), 0),
    "inline transport written out": _set((C, "ticks", "transport"), "inline"),
    "shm transport with slots": _set((C, "ticks"), {"schema": "toy.Counter",
                                                     "transport": "shm",
                                                     "slots": 4}),
    "interceptors": _set(TICKS_INTERCEPTORS, [
        {"kind": "drop", "start_ns": 0, "end_ns": 5},
        {"kind": "drop_nth", "n": 3},
        {"kind": "delay", "delay_ns": 1_000_000},
        {"kind": "override", "field": "value", "value": -7}]),
    "subscribe by name": _set((P, "echo", "subscribes"), ["ticks"]),
    "unbounded route object": _set((P, "echo", "subscribes"),
                                   [{"channel": "ticks"}]),
    "process lists absent": _set((P, "echo"), {
        "type": "process", "command": [sys.executable, ECHO],
        "step_period_ns": 10_000_000}),
    "empty field name": _set((S, "toy.Accum", "fields"),
                             ACCUM_FIELDS + [{"name": "", "type": "u8"}]),
    "array field": _set((S, "toy.Accum", "fields"),
                        ACCUM_FIELDS + [{"name": "pad", "type": "u8",
                                         "count": 3}]),
    "no participants": _set((P,), {}),
}

INVALID_CASES = {
    "not an object": lambda doc: doc.clear() or doc.update({"x": []}),
    "missing sil_manifest": _drop("sil_manifest"),
    "version 2": _set(("sil_manifest",), 2),
    "version as string": _set(("sil_manifest",), "1"),
    "unknown top-level key": _set(("comment",), "x"),
    "duration 0": _set(("duration_ns",), 0),
    "duration negative": _set(("duration_ns",), -1),
    "duration not whole": _set(("duration_ns",), 1.5),
    "duration beyond u64": _set(("duration_ns",), 2**64),
    "epoch negative": _set(("epoch_ns",), -1),
    "schemas missing": _drop("schemas"),
    "sil_manifest null": _set(("sil_manifest",), None),
    "duration_ns null": _set(("duration_ns",), None),
    "schemas null": _set(("schemas",), None),
    "channels null": _set(("channels",), None),
    "participants null": _set(("participants",), None),
    "schemas an array": _set((S,), []),
    "schema unknown key": _set((S, "toy.Accum", "doc"), "x"),
    "schema without fields": _set((S, "toy.Accum", "fields"), []),
    "field unknown key": _set((S, "toy.Accum", "fields", 0, "unit"), "m"),
    "field without name": _drop(S, "toy.Accum", "fields", 0, "name"),
    "field unknown type": _set((S, "toy.Accum", "fields", 0, "type"), "u128"),
    "field count 0": _set((S, "toy.Accum", "fields", 0, "count"), 0),
    "field count string": _set((S, "toy.Accum", "fields", 0, "count"), "2"),
    "duplicate field": _set((S, "toy.Accum", "fields", 1, "name"), "count"),
    "schema overflows size_t": _set((S, "toy.Accum", "fields", 0, "count"),
                                    2**62),
    "channel unknown key": _set((C, "ticks", "rate"), 10),
    "channel without schema": _drop(C, "ticks", "schema"),
    "channel unknown schema": _set((C, "ticks", "schema"), "toy.Missing"),
    "latency negative": _set((C, "ticks", "latency_ns"), -1),
    "unknown transport": _set((C, "ticks", "transport"), "udp"),
    "slots without shm": _set((C, "ticks", "slots"), 2),
    "slots 0": _set((C, "ticks"), {"schema": "toy.Counter", "transport": "shm",
                                    "slots": 0}),
    "interceptors not an array": _set(TICKS_INTERCEPTORS, {}),
    "unknown interceptor kind": _set(TICKS_INTERCEPTORS, [{"kind": "dup"}]),
    "interceptor unknown key": _set(TICKS_INTERCEPTORS,
                                    [{"kind": "drop", "every": 2}]),
    "window end before start": _set(TICKS_INTERCEPTORS, [
        {"kind": "drop", "start_ns": 5, "end_ns": 5}]),
    "delay without delay_ns": _set(TICKS_INTERCEPTORS, [{"kind": "delay"}]),
    "drop_nth 0": _set(TICKS_INTERCEPTORS, [{"kind": "drop_nth", "n": 0}]),
    "override unknown field": _set(TICKS_INTERCEPTORS, [
        {"kind": "override", "field": "speed", "value": 1}]),
    "override out of range": _set(TICKS_INTERCEPTORS, [
        {"kind": "override", "field": "seq", "value": -1}]),
    "override not whole": _set(TICKS_INTERCEPTORS, [
        {"kind": "override", "field": "value", "value": 1.5}]),
    "override value null": _set(TICKS_INTERCEPTORS, [
        {"kind": "override", "field": "value", "value": None}]),
    "f64 override beyond double": lambda doc: (
        _set((S, "toy.Counter", "fields"), [
            {"name": "seq", "type": "u64"}, {"name": "value", "type": "i64"},
            {"name": "gain", "type": "f64"}])(doc),
        _set(TICKS_INTERCEPTORS, [
            {"kind": "override", "field": "gain", "value": 10**400}])(doc)),
    "participant not an object": _set((P, "echo"), []),
    "participant without type": _drop(P, "echo", "type"),
    "unknown participant type": _set((P, "echo", "type"), "remote"),
    "participant unknown key": _set((P, "echo", "rate"), 1),
    "native without library": _drop(P, "producer", "library"),
    "native library missing": _set((P, "producer", "library"), "missing.silp"),
    "native without publishes": _drop(P, "producer", "publishes"),
    "native config an array": _set((P, "producer", "config"), []),
    "native with shim": _set((P, "producer", "shim"), True),
    "command empty": _set((P, "echo", "command"), []),
    "command not on PATH": _set((P, "echo", "command"),
                                ["sil-no-such-command", ECHO]),
    "command path missing": _set((P, "echo", "command"), ["./missing", ECHO]),
    "step period 0": _set((P, "echo", "step_period_ns"), 0),
    "priority beyond i32": _set((P, "echo", "priority"), 2**31),
    "sleep without shim": _set((P, "echo", "sleep"), "reject"),
    "shim not boolean": _set((P, "echo", "shim"), 1),
    "subscribes unknown channel": _set((P, "echo", "subscribes"), ["tick"]),
    "subscribes twice": _set((P, "echo", "subscribes"), ["ticks", "ticks"]),
    "capacity without overflow": _set((P, "echo", "subscribes"),
                                      [{"channel": "ticks", "capacity": 4}]),
    "capacity 0": _set((P, "echo", "subscribes"), [
        {"channel": "ticks", "capacity": 0, "overflow": "fail"}]),
    "blocking overflow": _set((P, "echo", "subscribes"), [
        {"channel": "ticks", "capacity": 4, "overflow": "blocking"}]),
    "publishes unknown channel": _set((P, "echo", "publishes"), ["ech0"]),
    "two publishers": _set((P, "echo", "publishes"), ["echo", "sums"]),
    "shm channel subscribed and published": lambda doc: (
        _set((C, "echo", "transport"), "shm")(doc),
        _set((P, "echo", "subscribes"), ["ticks", "echo"])(doc)),
}

def _doc_text(mutate) -> str:
    doc = _base()
    mutate(doc)
    return json.dumps(doc)


TEXT_CASES = {
    "invalid JSON": '{"sil_manifest": 1,',
    "NaN": '{"sil_manifest": 1, "duration_ns": NaN}',
    "number overflow": '{"sil_manifest": 1, "duration_ns": 1e400}',
    # A field name may be any string, so only the escape makes this invalid.
    "lone surrogate": _doc_text(_set((S, "toy.Accum", "fields"), ACCUM_FIELDS + [
        {"name": "SURROGATE", "type": "u8"}])).replace('"SURROGATE"',
                                                        '"\\ud800"'),
}


@pytest.fixture(scope="module")
def recording(sil_run, tmp_path_factory):
    """A Recording of the base Manifest, for the Replay participant cases."""
    directory = tmp_path_factory.mktemp("replay-source")
    manifest = directory / "base.json"
    manifest.write_text(json.dumps(_base()))
    out = directory / "base.mcap"
    subprocess.run([str(sil_run), str(manifest), "-o", str(out)], check=True,
                   capture_output=True)
    return out


def _replay(source, **change):
    def mutate(doc):
        del doc[P]["producer"]
        doc[P]["replay"] = {
            "type": "replay", "recording": str(source),
            "recording_hash": hashlib.sha256(source.read_bytes()).hexdigest(),
            "channels": ["ticks"], **change}
    return mutate


def _both_verdicts(sil_run, tmp_path, text):
    path = tmp_path / "manifest.json"
    path.write_text(text)
    problems = manifest_validate.validate(path)
    run = subprocess.run([str(sil_run), str(path), "-o", str(tmp_path / "o.mcap")],
                         capture_output=True, text=True)
    return problems, run


@pytest.mark.parametrize("case", VALID_CASES)
def test_validate_accepts_what_sil_run_accepts(case, sil_run, tmp_path):
    problems, run = _both_verdicts(sil_run, tmp_path,
                                   _doc_text(VALID_CASES[case]))

    assert run.returncode != 2, run.stderr
    assert problems == []


@pytest.mark.parametrize("case", list(INVALID_CASES) + list(TEXT_CASES))
def test_validate_rejects_what_sil_run_rejects(case, sil_run, tmp_path):
    text = TEXT_CASES.get(case) or _doc_text(INVALID_CASES[case])
    problems, run = _both_verdicts(sil_run, tmp_path, text)

    assert run.returncode == 2, run.stderr
    assert problems, run.stderr


@pytest.mark.parametrize("change, valid", [
    ({}, True),
    ({"priority": 1}, True),
    ({"recording_hash": "0" * 64}, False),
    ({"recording": "missing.mcap"}, False),
    ({"channels": []}, False),
    ({"channels": ["nope"]}, False),
    ({"channels": ["echo"]}, False),
])
def test_replay_participants_get_the_same_verdict(change, valid, recording,
                                                  sil_run, tmp_path):
    problems, run = _both_verdicts(sil_run, tmp_path,
                                   _doc_text(_replay(recording, **change)))

    assert (run.returncode != 2) == valid, run.stderr
    assert (problems == []) == valid, problems


def test_replay_with_a_schema_unlike_the_recorded_one_is_rejected_by_both(
        recording, sil_run, tmp_path):
    def mutate(doc):
        _replay(recording)(doc)
        doc[S]["toy.Counter"]["fields"][1]["type"] = "i32"

    problems, run = _both_verdicts(sil_run, tmp_path, _doc_text(mutate))

    assert run.returncode == 2, run.stderr
    assert [p.path for p in problems] == ["/participants/replay/channels/0"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
@pytest.mark.parametrize("artifact", ["library", "command file", "recording"])
def test_an_unreadable_artifact_is_rejected_by_both(artifact, recording,
                                                    sil_run, tmp_path):
    unreadable = tmp_path / "artifact"
    if artifact == "recording":
        unreadable.write_bytes(recording.read_bytes())
    else:
        unreadable.write_text("x")
    unreadable.chmod(0)

    def mutate(doc):
        if artifact == "library":
            doc[P]["producer"]["library"] = str(unreadable)
        elif artifact == "command file":
            doc[P]["echo"]["command"].append(str(unreadable))
        else:
            _replay(unreadable)(doc)

    try:
        doc = _base()
        if artifact == "recording":
            unreadable.chmod(0o600)
            mutate(doc)
            unreadable.chmod(0)
        else:
            mutate(doc)
        problems, run = _both_verdicts(sil_run, tmp_path, json.dumps(doc))
    finally:
        unreadable.chmod(0o600)

    assert run.returncode == 2, run.stderr
    assert problems
