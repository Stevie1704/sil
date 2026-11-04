"""Offline decoding of source-scope captures (#371).

docs/proposals/native-source-scopes.md is the contract. The fixtures in
tests/fixtures/source_scopes/ are hand-derived worked examples (Q22); the
other cases pin Q2, Q11, Q12 and Q23.
"""
import hashlib
import json
from pathlib import Path
import struct

import pytest

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
