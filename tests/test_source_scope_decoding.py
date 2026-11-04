"""Offline decoding of source-scope captures (#371).

docs/proposals/native-source-scopes.md is the contract. The fixtures in
tests/fixtures/source_scopes/ are hand-derived worked examples (Q22); the
other cases pin Q2, Q11, Q12 and Q23.
"""
import hashlib
import json
from pathlib import Path
import struct
import uuid

import pytest

from sil.source_scope_decode import CaptureFailed, decode
from sil.source_scope_report import (capture_record, encode_calls, encode_map,
                                     encode_report, map_document, scope_report)
from sil.source_scope_map import (MapError, file_identity, file_sha256, read_library,
                                  scope_entries)

FIXTURES = Path(__file__).resolve().parent / "fixtures/source_scopes"
SITE = struct.Struct("<4sHHI64s180s")


def site(scope, name, line=1, file=b"a.c", magic=b"SSC1", version=1):
    return SITE.pack(magic, version, scope, line, name, file)


def test_map_extractor_reproduces_the_fixture_map():
    section = (FIXTURES / "scope-section.bin").read_bytes()
    expected = json.loads((FIXTURES / "map.json").read_text())["scopes"]
    assert scope_entries(section) == expected


@pytest.mark.parametrize("section", [
    site(1, b"a") + site(1, b"b"),                 # conflicting duplicate
    site(0, b"a"),                                 # reserved ID
    site(1, b"a", magic=b"XXXX"),
    site(1, b"a", version=2),
    site(1, b"a")[:-1],                            # not a whole record
    site(1, b"a" * 64),                            # name without terminator
    site(1, b"a", file=b"f" * 180),                # file without terminator
    site(1, b""),                                  # empty name
    site(1, b"\xff"),                              # name is not UTF-8
    site(1, b"a\0b"),                              # bytes after the terminator
    b"",                                           # no site
], ids=["duplicate", "id0", "magic", "version", "size", "name", "file",
        "empty", "utf8", "padding", "empty-section"])
def test_invalid_sections_are_map_errors(section):
    with pytest.raises(MapError):
        scope_entries(section)


def test_the_map_holds_at_most_4096_sites():
    assert len(scope_entries(b"".join(site(1, b"a", line=n) for n in range(4096)))[0]
               ["sites"]) == 4096
    with pytest.raises(MapError):
        scope_entries(b"".join(site(1, b"a", line=n) for n in range(4097)))


def test_malicious_names_keep_their_bytes_for_json_escaping():
    name = '</script><b>"\\ '.encode()
    entries = scope_entries(site(7, name))
    assert entries[0]["name"] == name.decode()
    assert json.loads(json.dumps(entries)) == entries


def elf(sections):
    """A minimal ELF64 little-endian file with the named sections:
    {name: (type, body, link_name)}."""
    names = [""] + list(sections) + [".shstrtab"]
    shstrtab = b"\0".join(n.encode() for n in names) + b"\0"
    offsets = {n: shstrtab.index(b"\0" + n.encode() + b"\0") + 1 for n in names[1:]}
    bodies = [(n, *sections[n]) for n in sections] + [(".shstrtab", 3, shstrtab, None)]
    data, headers = bytearray(64), [bytes(64)]
    for name, kind, body, link in bodies:
        offset = len(data)
        data += body
        headers.append(struct.pack("<IIQQQQIIQQ", offsets[name], kind, 0, 0, offset,
                                   len(body), names.index(link) if link else 0, 0, 1, 0))
    shoff = len(data)
    data += b"".join(headers)
    data[:64] = (b"\x7fELF\x02\x01\x01" + bytes(9)
                 + struct.pack("<HHIQQQIHHHHHH", 3, 62, 1, 0, 0, shoff, 0, 64, 0, 0,
                               64, len(headers), len(headers) - 1))
    return bytes(data)


BUILD_ID = bytes(range(20))


def note(build_id=BUILD_ID):
    return struct.pack("<III", 4, len(build_id), 3) + b"GNU\0" + build_id


def dynamic(*needed):
    strings = b"\0" + b"".join(n + b"\0" for n in needed)
    entries, position = b"", 1
    for name in needed:
        entries += struct.pack("<qQ", 1, position)
        position += len(name) + 1
    return strings, entries + struct.pack("<qQ", 0, 0)


def library(tmp_path, *, section=True, needed=(b"libc.so.6", b"libsil_source_scope.so.1"),
            build_id=BUILD_ID):
    strings, entries = dynamic(*needed)
    sections = {".note.gnu.build-id": (7, note(build_id) if build_id else b"", None),
                ".dynstr": (3, strings, None), ".dynamic": (6, entries, ".dynstr"),
                ".comment": (1, b"GCC: (Ubuntu 12.3.0) 12.3.0\0", None)}
    if section:
        sections["sil_source_scopes"] = (1, (FIXTURES / "scope-section.bin").read_bytes(),
                                         None)
    path = tmp_path / "libscoped.so"
    path.write_bytes(elf(sections))
    return path


def test_preflight_reads_identity_and_map_of_an_instrumented_library(tmp_path):
    path = library(tmp_path)
    scan = read_library(path)
    assert scan.identity == {"path": str(path), "build_id": BUILD_ID.hex(),
                             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    assert scan.compiler == "GCC: (Ubuntu 12.3.0) 12.3.0"
    assert scan.scopes == json.loads((FIXTURES / "map.json").read_text())["scopes"]


@pytest.mark.parametrize("options", [
    {"section": False}, {"needed": (b"libc.so.6",)}, {"build_id": b""}],
    ids=["no-section", "no-needed", "no-build-id"])
def test_uninstrumented_libraries_fail_preflight(tmp_path, options):
    with pytest.raises(MapError):
        read_library(library(tmp_path, **options))


def test_a_file_that_is_not_elf_fails_preflight(tmp_path):
    path = tmp_path / "lib.so"
    path.write_bytes(b"not an elf")
    with pytest.raises(MapError):
        read_library(path)
    with pytest.raises(MapError):
        read_library(tmp_path / "missing.so")


def test_the_original_library_is_identified_without_a_scope_section(tmp_path):
    path = library(tmp_path, section=False, needed=(b"libc.so.6",))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert file_identity(path) == {"path": str(path), "build_id": BUILD_ID.hex(),
                                   "sha256": digest}
    assert file_sha256(path) == digest
    assert file_sha256(tmp_path / "missing.so") is None


CASES = sorted(path.name for path in FIXTURES.iterdir() if path.is_dir())
CAPTURE_ID = "6c1f8e0a-2b7d-4f3e-9a51-0d2c4b6e8f10"
MAP = json.loads((FIXTURES / "map.json").read_text())


def fixture(case):
    folder = FIXTURES / case
    return ((folder / "events.bin").read_bytes(),
            (folder / "observations.jsonl").read_text(),
            json.loads((folder / "expected.json").read_text()))


def run_decode(events, observations, *, scope_map=MAP, capture_id=CAPTURE_ID,
               library_sha256=MAP["library"]["sha256"]):
    return decode(events, observations, scope_map, participant="adas",
                  capture_id=capture_id, library_sha256=library_sha256)


@pytest.mark.parametrize("case", CASES)
def test_decoder_reproduces_each_worked_example(case):
    events, observations, expected = fixture(case)
    if "error" in expected:
        with pytest.raises(CaptureFailed) as failure:
            run_decode(events, observations)
        assert {"error": failure.value.reason} == expected
    else:
        assert run_decode(events, observations) == expected


HEADER = struct.Struct("<8sHHHH16sIIQQQQQ3QQQB40s95x")
RECORD = struct.Struct("<QIHBB")
MAP_BUILD_ID = bytes.fromhex(MAP["library"]["build_id"])


def raw(records=(), *, capacity=None, state=2, pid=42, clock=(900, 901, 902),
        lost=(0, 0, 0), build_id=MAP_BUILD_ID, magic=b"SILSCOPE", kept=None,
        namespace=(4, 4026531836), capture_id=CAPTURE_ID):
    """An events.bin with (ns, tid, scope, edge[, reserved]) records."""
    capacity = len(records) if capacity is None else capacity
    head = HEADER.pack(magic, 1, 256, 16, state, uuid.UUID(capture_id).bytes, pid, 0,
                       capacity, len(records) if kept is None else kept, *lost, *clock,
                       *namespace, len(build_id), build_id)
    body = b"".join(RECORD.pack(*(tuple(r) + (0,))[:5]) for r in records)
    return head + body + bytes(16 * max(0, capacity - len(records)))


def stream(*activations, end=True, **header):
    lines = [{"sil_activation_observations": 1, "clock": "host_monotonic",
              "runner_pid": 42, "runner_tid": 42, "limit_bytes": 67108864,
              "pid_namespace": {"device": 4, "inode": 4026531836}, **header}]
    for index, (begin, finish, *rest) in enumerate(activations):
        lines.append({"kind": "activation", "participant": rest[0] if rest else "adas",
                      "task": "control", "task_kind": "native_task", "index": index,
                      "virtual_time_ns": index, "begin_ns": str(begin),
                      "end_ns": str(finish), "outcome": "completed", "tid": 42})
    if end is True:
        end = {"kind": "end", "records": len(activations), "dropped": 0,
               "truncated": False, "write_failed": False, "bytes": 0}
    if end:
        lines.append(end)
    return "\n".join(json.dumps(line) for line in lines) + "\n"


CALL = [(10, 42, 1, 0), (20, 42, 1, 1)]


def failure(events, observations=None, **options):
    with pytest.raises(CaptureFailed) as failed:
        run_decode(events, observations or stream((0, 100)), **options)
    return failed.value.reason


def test_a_matching_capture_decodes():
    assert run_decode(raw(CALL), stream((0, 100)))["totals"]["attributed_calls"] == 1


@pytest.mark.parametrize("events", [
    raw(CALL, magic=b"SILSCOPX"), raw(CALL, state=4), raw(CALL)[:-1],
    raw(CALL, kept=3), raw(CALL, capacity=0), raw([(10, 42, 9, 0)]),
    raw([(10, 42, 1, 2)]), raw([(10, 42, 1, 0, 1)]), raw([(20, 42, 1, 0), (10, 42, 1, 1)]),
    raw(CALL, state=0), b"SILSCOPE"],
    ids=["magic", "state", "size", "records", "capacity", "scope", "edge", "reserved",
         "time-order", "created-with-records", "short"])
def test_invalid_raw_files_fail_before_output(events):
    assert failure(events) == "raw_invalid"


def test_raw_file_states():
    assert failure(raw(CALL, state=1)) == "raw_incomplete"
    assert failure(raw(CALL, state=3)) == "observer_unavailable"
    created = run_decode(raw([], capacity=4, state=0, pid=0, build_id=b"", clock=(0, 0, 0)),
                         stream((0, 100)))
    assert (created["status"], created["totals"]["entries"]) == ("complete", 0)
    assert created["activations"][0]["status"] == "exact"


def test_capture_identity_checks():
    other = "00000000-0000-4000-8000-000000000000"
    assert failure(raw(CALL), capture_id=other) == "capture_mismatch"
    assert failure(raw(CALL, pid=43)) == "capture_mismatch"
    assert failure(raw(CALL, namespace=(4, 1))) == "capture_mismatch"
    assert failure(raw(CALL), stream((0, 100), pid_namespace=None)) == "capture_mismatch"
    assert failure(raw(CALL), "not json\n") == "capture_mismatch"


def test_a_rebuilt_or_changed_library_is_an_identity_mismatch():
    assert failure(raw(CALL, build_id=bytes(20))) == "identity_mismatch"
    assert failure(raw(CALL), library_sha256="0" * 64) == "identity_mismatch"
    assert failure(raw(CALL), library_sha256=None) == "identity_mismatch"


def test_unproven_clocks_fail():
    assert failure(raw(CALL), stream((0, 100), clock="host_realtime")) == "clock_unverified"
    assert failure(raw(CALL, clock=(902, 901, 903))) == "clock_unverified"


def test_an_invalid_map_fails():
    duplicate = {**MAP, "scopes": MAP["scopes"] + MAP["scopes"][:1]}
    assert failure(raw(CALL), scope_map=duplicate) == "map_invalid"
    crowded = {**MAP, "scopes": [{"id": 1, "name": "a", "sites": [
        {"file": "a.c", "line": n} for n in range(4097)]}]}
    assert failure(raw(CALL), scope_map=crowded) == "map_invalid"


def test_a_return_without_entry_is_a_missing_entry_row():
    result = run_decode(raw([(20, 42, 1, 1)]), stream((0, 100)))
    assert result["problems"] == ["unbalanced_scopes"]
    assert result["unattributed"]["rows"] == [
        {"scope": 1, "tid": 42, "entry_ns": None, "return_ns": "20",
         "reason": "missing_entry"}]
    assert result["activations"][0]["reason"] == "unbalanced_scopes"


def test_a_cut_stream_leaves_later_calls_unattributed():
    events = raw(CALL + [(110, 42, 1, 0), (120, 42, 1, 1),
                         (210, 42, 2, 0), (220, 42, 2, 1)])
    result = run_decode(events, stream((0, 100), (100, 150), end=False))
    assert (result["status"], result["problems"]) == ("partial", ["observations_incomplete"])
    assert [a["status"] for a in result["activations"]] == ["exact", "exact"]
    assert result["unattributed"]["rows"] == [
        {"scope": 2, "tid": 42, "entry_ns": "210", "return_ns": "220",
         "reason": "observations_incomplete"}]


def test_in_a_cut_stream_a_call_into_a_gap_is_observations_incomplete():
    events = raw([(90, 42, 1, 0), (120, 42, 1, 1)])
    result = run_decode(events, stream((0, 100), (130, 150), end=False))
    assert result["activations"][0]["reason"] == "observations_incomplete"
    assert result["unattributed"]["reasons"] == {"observations_incomplete": 1}


def test_large_integers_and_many_rows_are_bounded():
    top = 2 ** 64 - 1
    records = [(n, 43, 1, edge) for n in range(1100) for edge in (0, 1)]
    records += [(top - 1, 42, 1, 0), (top, 42, 1, 1)]
    result = run_decode(raw(records), stream((top - 1, top + 1)))
    unattributed = result["unattributed"]
    assert len(unattributed["rows"]) == 1000 and unattributed["rows_remainder"] == 100
    assert result["totals"]["unattributed"] == 1100
    assert unattributed["reasons"] == {"worker_thread": 1100}
    assert unattributed["rows"][-1]["entry_ns"] == "999"
    call = result["activations"][0]["calls"][0]
    assert (call["entry_ns"], call["elapsed_ns"]) == (str(top - 1), "1")



def test_map_document_binds_the_scopes_to_both_builds(tmp_path):
    scan = read_library(library(tmp_path))
    document = json.loads(encode_map(map_document(scan, None)))
    assert document == {"sil_source_scope_map": 1, "library": scan.identity,
                        "original": None, "scopes": MAP["scopes"]}


def test_calls_document_holds_the_maximum_capture():
    pairs = 131072
    records = [(2 * n + edge, 42, 1, edge) for n in range(pairs) for edge in (0, 1)]
    result = run_decode(raw(records), stream((0, 2 * pairs)))
    assert result["totals"]["attributed_calls"] == pairs
    encoded = encode_calls(result)
    assert len(encoded) <= 256 << 20 and json.loads(encoded) == result


def test_more_calls_than_the_budget_is_a_normalized_limit():
    calls = [{"scope": 1}] * 131073
    with pytest.raises(CaptureFailed) as failed:
        encode_calls({"activations": [{"calls": calls}]})
    assert failed.value.reason == "normalized_limit"


def report_for(events, calls, **capture):
    return scope_report(
        sil_version="0.2.1", configuration={"participant": "adas"},
        run={"exit_code": 0}, identities={"instrumented": MAP["library"]},
        artifacts=[], missing=[], scope_map=MAP, calls=calls,
        capture=capture_record(events, calls, **capture))


def test_report_summarizes_tasks_and_scopes():
    events, observations, _ = fixture("unattributed")
    report = report_for(events, run_decode(events, observations))
    assert report["sil_source_scope_report"] == 1
    assert report["producer"] == {"name": "sil profile", "sil_version": "0.2.1"}
    assert report["capture"] == {
        "status": "complete", "reasons": [], "capture_id": CAPTURE_ID,
        "loss": {"dropped_full": 0, "dropped_contended": 0, "clock_errors": 0},
        "records": 12}
    assert report["summary"]["tasks"] == [
        {"participant": "adas", "task": "control", "activations": 2, "exact": 1,
         "unavailable": {"not_completed": 1}}]
    check, confirmed = report["summary"]["scopes"][:2]
    assert check == {"id": 1, "name": "check_object", "attributed_calls": 1,
                     "per_exact_activation": {"min": 1, "median": 1, "max": 1}}
    assert confirmed["per_exact_activation"] == {"min": 0, "median": 0, "max": 0}
    assert len(report["limitations"]) == 4


def test_report_median_of_two_exact_activations():
    events, observations, _ = fixture("complete")
    report = report_for(events, run_decode(events, observations))
    check = report["summary"]["scopes"][0]
    assert check["per_exact_activation"] == {"min": 0, "median": 1, "max": 2}
    confirmed = report["summary"]["scopes"][1]
    assert confirmed["per_exact_activation"] == {"min": 0, "median": 0.5, "max": 1}


def test_a_failed_decode_reports_the_reason_and_kept_counters():
    events, observations, _ = fixture("identity-mismatch")
    with pytest.raises(CaptureFailed) as failed:
        run_decode(events, observations)
    report = report_for(events, None, failure=failed.value)
    assert report["capture"] == {
        "status": "failed", "reasons": ["identity_mismatch"], "capture_id": CAPTURE_ID,
        "loss": {"dropped_full": 0, "dropped_contended": 0, "clock_errors": 0},
        "records": 2}
    assert report["summary"] == {"tasks": [], "scopes": []}
    unread = report_for(b"short", None, failure=CaptureFailed("raw_invalid", "short"))
    assert unread["capture"]["loss"] is None and unread["capture"]["capture_id"] is None


def test_an_interrupted_capture_keeps_its_problems():
    events, observations, _ = fixture("loss")
    capture = report_for(events, run_decode(events, observations),
                         interrupted=True)["capture"]
    assert (capture["status"], capture["reasons"]) == ("interrupted",
                                                       ["interrupted", "events_lost"])


def test_report_is_escaped_ascii_within_one_mebibyte():
    name = '</script> "'
    scope_map = {**MAP, "scopes": [{"id": 1, "name": name, "sites": []}]}
    calls = run_decode(raw(CALL), stream((0, 100)), scope_map=scope_map)
    report = scope_report(
        sil_version="0.2.1", configuration={}, run={}, identities={}, artifacts=[],
        missing=[], scope_map=scope_map, calls=calls, capture=capture_record(raw(CALL), calls))
    encoded = encode_report(report)
    assert encoded.isascii() and b"</script>" in encoded
    assert json.loads(encoded)["summary"]["scopes"][0]["name"] == name
    with pytest.raises(ValueError):
        encode_report({**report, "configuration": {"pad": "x" * (1 << 20)}})
