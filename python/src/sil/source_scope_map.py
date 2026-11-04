"""The scope map of a source-scope library (#371).

docs/proposals/native-source-scopes.md is the contract. The map is in the
bytes of the instrumented library: one 256-byte site record per
`SIL_SOURCE_SCOPE` use in the ELF section `sil_source_scopes`. This module
reads that section and groups its sites by scope ID into the `scopes`
member of `sil_source_scope_map` v1. The capture preflight (#372) and the
decoder use it.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from pathlib import Path

SECTION = "sil_source_scopes"
OBSERVER_SONAME = "libsil_source_scope.so.1"
SITE = struct.Struct("<4sHHI64s180s")
SITE_MAGIC = b"SSC1"
SITE_VERSION = 1
MAX_SITES = 4096
MAX_LIBRARY_BYTES = 256 << 20
SHT_DYNAMIC, SHT_NOTE, SHT_NOBITS = 6, 7, 8
DT_NULL, DT_NEEDED = 0, 1
NT_GNU_BUILD_ID = 3


class MapError(ValueError):
    """The scope map is not valid (`map_invalid`)."""


@dataclass(frozen=True)
class LibraryScan:
    """What the capture preflight records of the instrumented library."""

    identity: dict  # {path, build_id, sha256}
    compiler: str | None  # the `.comment` section, or None
    scopes: list[dict]  # the `scopes` member of the map


def read_library(path: Path) -> LibraryScan:
    """The identity and scope map of an instrumented Native library.

    MapError when the file is not a readable ELF64 little-endian file with
    a GNU build ID, a valid `sil_source_scopes` section and the observer in
    `DT_NEEDED`: a preflight failure (exit 2)."""
    data = _read(path)
    elf = _elf(path, data)
    if elf.build_id is None:
        raise MapError(f"'{path}' has no GNU build ID")
    if SECTION not in elf.sections:
        raise MapError(f"'{path}' has no {SECTION} section: build it with "
                       "SIL_SOURCE_SCOPES=1")
    if OBSERVER_SONAME not in elf.needed:
        raise MapError(f"'{path}' does not link {OBSERVER_SONAME}")
    comment = elf.sections.get(".comment", b"").split(b"\0", 1)[0]
    return LibraryScan(
        {"path": str(path), "build_id": elf.build_id,
         "sha256": hashlib.sha256(data).hexdigest()},
        comment.decode(errors="replace") or None,
        scope_entries(elf.sections[SECTION]))


def file_identity(path: Path) -> dict:
    """`{path, build_id, sha256}` of an ELF file that is never loaded, such
    as `--original-library`; `build_id` is None when it has none."""
    data = _read(path)
    return {"path": str(path), "build_id": _elf(path, data).build_id,
            "sha256": hashlib.sha256(data).hexdigest()}


def file_sha256(path: Path) -> str | None:
    """The SHA-256 of a file, or None when it cannot be read. The decode
    compares it with the preflight value."""
    try:
        return hashlib.sha256(_read(path)).hexdigest()
    except MapError:
        return None


@dataclass(frozen=True)
class _Elf:
    sections: dict[str, bytes]  # the first section of each name
    build_id: str | None
    needed: set[str]


def _read(path: Path) -> bytes:
    try:
        with open(path, "rb") as stream:
            data = stream.read(MAX_LIBRARY_BYTES + 1)
    except OSError as error:
        raise MapError(f"cannot read '{path}': {error.strerror}") from None
    if len(data) > MAX_LIBRARY_BYTES:
        raise MapError(f"'{path}' is larger than {MAX_LIBRARY_BYTES} bytes")
    return data


def _elf(path: Path, data: bytes) -> _Elf:
    """The sections, build ID and `DT_NEEDED` names of an ELF64 LE file."""
    try:
        return _parse(data)
    except (struct.error, IndexError, ValueError):
        raise MapError(f"'{path}' is not a valid ELF64 little-endian "
                       "file") from None


def _parse(data: bytes) -> _Elf:
    if data[:6] != b"\x7fELF\x02\x01":
        raise ValueError("not ELF64 little-endian")
    shoff, = struct.unpack_from("<Q", data, 0x28)
    entsize, count, names = struct.unpack_from("<HHH", data, 0x3A)
    rows = [struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * entsize)
            for i in range(count)]

    def body(row) -> bytes:
        kind, offset, size = row[1], row[4], row[5]
        if kind == SHT_NOBITS:
            return b""
        if offset + size > len(data):
            raise ValueError("a section is past the end of the file")
        return data[offset:offset + size]

    name_table = body(rows[names])
    sections: dict[str, bytes] = {}
    build_id, needed = None, set()
    for row in rows:
        end = name_table.index(b"\0", row[0])
        sections.setdefault(name_table[row[0]:end].decode(errors="replace"),
                            body(row))
        if row[1] == SHT_NOTE and build_id is None:
            build_id = _note_build_id(body(row))
        elif row[1] == SHT_DYNAMIC:
            needed |= _needed(body(row), body(rows[row[6]]))
    return _Elf(sections, build_id, needed)


def _needed(dynamic: bytes, strings: bytes) -> set[str]:
    needed = set()
    for offset in range(0, len(dynamic) - 15, 16):
        tag, value = struct.unpack_from("<qQ", dynamic, offset)
        if tag == DT_NULL:
            break
        if tag == DT_NEEDED:
            end = strings.index(b"\0", value)
            needed.add(strings[value:end].decode(errors="replace"))
    return needed


def _note_build_id(body: bytes) -> str | None:
    position = 0
    while position + 12 <= len(body):
        name_size, desc_size, kind = struct.unpack_from("<III", body, position)
        name_end = position + 12 + name_size
        desc_start = name_end + (-name_size % 4)
        if kind == NT_GNU_BUILD_ID and desc_size and \
                body[position + 12:name_end] == b"GNU\0":
            return body[desc_start:desc_start + desc_size].hex()
        position = desc_start + desc_size + (-desc_size % 4)
    return None


def scope_entries(section: bytes) -> list[dict]:
    """One `{id, name, sites}` per scope ID, sorted by ID; each ID's
    distinct sites sorted by file, then line."""
    if not section or len(section) % SITE.size:
        raise MapError(f"the section has {len(section)} bytes, "
                       f"not a positive multiple of {SITE.size}")
    count = len(section) // SITE.size
    if count > MAX_SITES:
        raise MapError(f"the section has {count} sites, more than "
                       f"{MAX_SITES}")
    scopes: dict[int, tuple[str, set]] = {}
    for offset in range(0, len(section), SITE.size):
        magic, version, scope, line, name, file = SITE.unpack_from(
            section, offset)
        if (magic, version) != (SITE_MAGIC, SITE_VERSION):
            raise MapError(f"site {offset // SITE.size} has no "
                           f"version-{SITE_VERSION} site magic")
        if scope == 0:
            raise MapError("scope ID 0 is reserved")
        name_text, file_text = _text(name, "name"), _text(file, "file")
        if not name_text:
            raise MapError(f"scope {scope} has an empty name")
        known = scopes.setdefault(scope, (name_text, set()))
        if known[0] != name_text:
            raise MapError(f"scope {scope} has two names: {known[0]!r} "
                           f"and {name_text!r}")
        known[1].add((file_text, line))
    return [{"id": scope, "name": name,
             "sites": [{"file": f, "line": n} for f, n in sorted(sites)]}
            for scope, (name, sites) in sorted(scopes.items())]


def _text(field: bytes, label: str) -> str:
    """A zero-terminated, zero-padded UTF-8 field."""
    end = field.find(b"\0")
    if end < 0 or field[end:].count(0) != len(field) - end:
        raise MapError(f"a site {label} is not zero-terminated and "
                       "zero-padded")
    try:
        return field[:end].decode()
    except UnicodeDecodeError:
        raise MapError(f"a site {label} is not UTF-8") from None
