"""Module material and debug-file matching of a CPU profile (#315).

perf names each module by path and build ID. After the Run, `sil profile`
decides where the module bytes are (on the host, retained from the Run
working directory, or gone) and whether a debug file with the same build ID
exists. The ELF files here are small synthetic ones, so the tests run on any
host; the real compiler output is covered by the opt-in perf integration.
"""

from __future__ import annotations

import struct
from pathlib import Path

from sil.profile_cpu import ModuleFacts
from sil.profile_modules import ModuleLocator, elf_identity

BUILD_ID = "5f" * 20
OTHER_ID = "a0" * 20


def elf(path: Path, *, build_id: str | None, debug_lines: bool = False,
        debuglink: str | None = None) -> Path:
    """A little-endian ELF64 file with only the named sections."""
    sections: list[tuple[str, int, bytes]] = []  # name, type, data
    if build_id is not None:
        descriptor = bytes.fromhex(build_id)
        note = struct.pack("<III", 4, len(descriptor), 3) + b"GNU\0" \
            + descriptor
        sections.append((".note.gnu.build-id", 7, note))
    if debuglink is not None:
        name = debuglink.encode() + b"\0"
        name += b"\0" * (-len(name) % 4)
        sections.append((".gnu_debuglink", 1, name + struct.pack("<I", 0)))
    if debug_lines:
        sections.append((".debug_line", 1, b"\x01\x02"))
        sections.append((".debug_info", 1, b"\x03\x04"))
    names = b"\0" + b"".join(n.encode() + b"\0" for n, _, _ in sections) \
        + b".shstrtab\0"
    body = b""
    headers = [b"\0" * 64]
    offset = 64
    name_offset = 1
    for name, kind, data in [*sections, (".shstrtab", 3, names)]:
        headers.append(struct.pack("<IIQQQQIIQQ", name_offset, kind, 0, 0,
                                   offset + len(body), len(data), 0, 0, 1, 0))
        name_offset += len(name) + 1
        body += data
    section_offset = 64 + len(body)
    header = (b"\x7fELF" + bytes([2, 1, 1]) + b"\0" * 9
              + struct.pack("<HHIQQQIHHHHHH", 3, 62, 1, 0, 0, section_offset,
                            0, 64, 0, 0, 64, len(headers), len(headers) - 1))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + body + b"".join(headers))
    return path


def locator(tmp_path: Path, **kwargs) -> ModuleLocator:
    return ModuleLocator(debug_root=tmp_path / "usr-lib-debug", **kwargs)


def test_elf_identity_reads_build_id_debug_lines_and_debuglink(tmp_path):
    library = elf(tmp_path / "lib.so", build_id=BUILD_ID,
                  debuglink="lib.so.debug")

    identity = elf_identity(library)

    assert identity.build_id == BUILD_ID
    assert identity.debuglink == "lib.so.debug"
    assert not identity.has_debug_lines


def test_a_file_that_is_not_elf_has_no_identity(tmp_path):
    text = tmp_path / "script.py"
    text.write_text("print('no ELF')\n")

    assert elf_identity(text) is None
    assert elf_identity(tmp_path / "missing.so") is None


def test_a_module_with_its_own_debug_lines_is_embedded(tmp_path):
    library = elf(tmp_path / "lib.so", build_id=BUILD_ID, debug_lines=True)

    assert locator(tmp_path).facts(str(library), BUILD_ID) == ModuleFacts(
        "host_file", "embedded", None)


def test_a_separate_debug_file_beside_the_module_matches_by_build_id(
        tmp_path):
    library = elf(tmp_path / "lib.so", build_id=BUILD_ID,
                  debuglink="lib.so.debug")
    debug = elf(tmp_path / "lib.so.debug", build_id=BUILD_ID,
                debug_lines=True)

    assert locator(tmp_path).facts(str(library), BUILD_ID) == ModuleFacts(
        "host_file", "separate_matching", str(debug))


def test_the_build_id_directory_is_searched_first(tmp_path):
    library = elf(tmp_path / "lib.so", build_id=BUILD_ID)
    debug = elf(tmp_path / "usr-lib-debug" / ".build-id" / BUILD_ID[:2]
                / f"{BUILD_ID[2:]}.debug", build_id=BUILD_ID, debug_lines=True)

    assert locator(tmp_path).facts(str(library), BUILD_ID) == ModuleFacts(
        "host_file", "separate_matching", str(debug))


def test_a_debug_file_with_another_build_id_is_never_used(tmp_path):
    library = elf(tmp_path / "lib.so", build_id=BUILD_ID,
                  debuglink="lib.so.debug")
    elf(tmp_path / ".debug" / "lib.so.debug", build_id=OTHER_ID,
        debug_lines=True)

    assert locator(tmp_path).facts(str(library), BUILD_ID) == ModuleFacts(
        "host_file", "separate_mismatched", None)


def test_no_debug_file_is_not_found(tmp_path):
    library = elf(tmp_path / "lib.so", build_id=BUILD_ID,
                  debuglink="lib.so.debug")

    assert locator(tmp_path).facts(str(library), BUILD_ID) == ModuleFacts(
        "host_file", "not_found", None)


def test_without_a_build_id_a_separate_debug_file_is_unverifiable(tmp_path):
    library = elf(tmp_path / "lib.so", build_id=None,
                  debuglink="lib.so.debug")
    elf(tmp_path / "lib.so.debug", build_id=None, debug_lines=True)

    assert locator(tmp_path).facts(str(library), None) == ModuleFacts(
        "host_file", "unverifiable", None)


def test_a_host_file_with_another_build_id_is_not_the_module(tmp_path):
    library = elf(tmp_path / "lib.so", build_id=OTHER_ID, debug_lines=True)

    assert locator(tmp_path).facts(str(library), BUILD_ID) == ModuleFacts(
        "unavailable", "module_unavailable", None)


def test_a_module_from_the_run_directory_is_found_in_the_retained_tree(
        tmp_path):
    run_directory = tmp_path / "work" / ".sil-run-abc123"
    retained = tmp_path / "profile" / "cpu" / ".run-directory"
    copy = elf(retained / "participant-1" / "fmu.so", build_id=BUILD_ID,
               debug_lines=True)
    original = run_directory / "participant-1" / "fmu.so"
    found = locator(tmp_path, cwd=tmp_path / "work", retained=retained)

    assert found.facts(str(original), BUILD_ID) == ModuleFacts(
        "retained", "embedded", None)
    assert found.retained_files() == {str(original): copy}


def test_a_gone_module_without_a_retained_copy_is_unavailable(tmp_path):
    gone = tmp_path / "work" / ".sil-run-abc123" / "fmu.so"
    found = locator(tmp_path, cwd=tmp_path / "work",
                    retained=tmp_path / "absent")

    assert found.facts(str(gone), BUILD_ID) == ModuleFacts(
        "unavailable", "module_unavailable", None)
    assert found.retained_files() == {}


def test_a_pseudo_module_is_unavailable(tmp_path):
    assert locator(tmp_path).facts("[vdso]", None) == ModuleFacts(
        "unavailable", "module_unavailable", None)
