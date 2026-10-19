"""Where a profiled module's bytes are, and whether its debug file matches.

perf identifies each module by the path it was loaded from and its GNU build
ID. After the Run, `sil profile` (#315) checks the file at that path, or the
copy that the diagnostics runner retained from the Run working directory,
and looks for a separate debug file as GDB and perf do: in the build-ID
directory, then through `.gnu_debuglink` beside the module, in its `.debug`
directory and under the global debug directory. A debug file is used only
when its build ID equals the module's.

This reads ELF section headers and notes only. It is not a DWARF parser:
symbolization stays with perf.
"""

from __future__ import annotations

import mmap
import os
import struct
from dataclasses import dataclass
from pathlib import Path

from sil.profile_cpu import ModuleFacts

NT_GNU_BUILD_ID = 3
SHT_NOBITS = 8
DEBUG_ROOT = Path("/usr/lib/debug")
RUN_DIRECTORY_PREFIX = ".sil-run-"
UNAVAILABLE = ModuleFacts("unavailable", "module_unavailable", None)


@dataclass(frozen=True)
class ElfIdentity:
    build_id: str | None
    has_debug_lines: bool
    debuglink: str | None


def elf_identity(path: Path) -> ElfIdentity | None:
    """The identity of an ELF file; None when it is not a readable ELF."""
    try:
        with open(path, "rb") as stream, mmap.mmap(
                stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            return _parse_elf(data)
    except (OSError, struct.error, IndexError, ValueError):
        return None


def _parse_elf(data) -> ElfIdentity | None:
    if data[:4] != b"\x7fELF" or data[5] not in (1, 2):
        return None
    wide = data[4] == 2
    order = "<" if data[5] == 1 else ">"
    if wide:
        shoff, = struct.unpack_from(order + "Q", data, 0x28)
        entsize, count, names = struct.unpack_from(order + "HHH", data, 0x3A)
        layout = order + "IIQQQQIIQQ"
    else:
        shoff, = struct.unpack_from(order + "I", data, 0x20)
        entsize, count, names = struct.unpack_from(order + "HHH", data, 0x2E)
        layout = order + "IIIIIIIIII"
    sections = [struct.unpack_from(layout, data, shoff + i * entsize)
                for i in range(count)]
    name_base = sections[names][4]

    def name(section) -> str:
        end = data.find(b"\0", name_base + section[0])
        return data[name_base + section[0]:end].decode(errors="replace")

    build_id = debuglink = None
    debug_lines = False
    for section in sections:
        kind, offset, size = section[1], section[4], section[5]
        body = data[offset:offset + size] if kind != SHT_NOBITS else b""
        label = name(section)
        if kind == 7 and build_id is None:
            build_id = _note_build_id(body, order)
        elif label == ".gnu_debuglink" and body:
            debuglink = body.split(b"\0", 1)[0].decode(errors="replace")
        elif label == ".debug_line" and kind != SHT_NOBITS and size:
            debug_lines = True
    return ElfIdentity(build_id, debug_lines, debuglink)


def _note_build_id(body: bytes, order: str) -> str | None:
    position = 0
    while position + 12 <= len(body):
        name_size, desc_size, kind = struct.unpack_from(order + "III", body,
                                                        position)
        name_end = position + 12 + name_size
        desc_start = name_end + (-name_size % 4)
        if kind == NT_GNU_BUILD_ID and body[position + 12:name_end] == \
                b"GNU\0":
            return body[desc_start:desc_start + desc_size].hex()
        position = desc_start + desc_size + (-desc_size % 4)
    return None


class ModuleLocator:
    """Facts about the modules of one capture, after its Run ended."""

    def __init__(self, *, cwd: Path | None = None,
                 retained: Path | None = None,
                 debug_root: Path = DEBUG_ROOT):
        self.cwd = Path(os.path.realpath(cwd or Path.cwd()))
        self.retained = retained
        self.debug_root = debug_root
        self._retained: dict[str, Path] = {}

    def facts(self, path: str, build_id: str | None) -> ModuleFacts:
        if path.startswith("["):  # [vdso], [anon] and the like
            return UNAVAILABLE
        material, file = self._material(path, build_id)
        if file is None:
            return UNAVAILABLE
        identity = elf_identity(file)
        if identity.has_debug_lines:
            return ModuleFacts(material, "embedded", None)
        status, debug = self._debug(file, identity, build_id)
        return ModuleFacts(material, status, debug)

    def retained_files(self) -> dict[str, Path]:
        """Each module path whose retained copy has its build ID."""
        return dict(self._retained)

    def _material(self, path: str, build_id: str | None):
        identity = elf_identity(Path(path))
        if identity is not None and (build_id is None
                                     or identity.build_id == build_id):
            return "host_file", Path(path)
        copy = self._retained_copy(path)
        if copy is not None and build_id is not None:
            copied = elf_identity(copy)
            if copied is not None and copied.build_id == build_id:
                self._retained[path] = copy
                return "retained", copy
        return "unavailable", None

    def _retained_copy(self, path: str) -> Path | None:
        """The path under the retained tree of a Run-directory module."""
        if self.retained is None:
            return None
        try:
            relative = Path(path).relative_to(self.cwd)
        except ValueError:
            return None
        if not relative.parts or not relative.parts[0].startswith(
                RUN_DIRECTORY_PREFIX):
            return None
        return self.retained.joinpath(*relative.parts[1:])

    def _debug(self, file: Path, identity: ElfIdentity,
               build_id: str | None) -> tuple[str, str | None]:
        candidates = []
        if build_id:
            candidates.append(self.debug_root / ".build-id" / build_id[:2]
                              / f"{build_id[2:]}.debug")
        if identity.debuglink:
            directory = file.parent
            candidates += [directory / identity.debuglink,
                           directory / ".debug" / identity.debuglink,
                           self.debug_root.joinpath(
                               *directory.parts[1:], identity.debuglink)]
        found = [(c, elf_identity(c)) for c in candidates if c.is_file()]
        found = [(c, i) for c, i in found if i is not None and c != file]
        if not found:
            return "not_found", None
        if build_id is None:
            return "unverifiable", None
        for candidate, debug in found:
            if debug.build_id == build_id:
                return "separate_matching", str(candidate)
        return "separate_mismatched", None
