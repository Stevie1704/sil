"""`include/sil/source_scope.h`: the source API of source scopes (#372).

docs/proposals/native-source-scopes.md, "Source API", is the contract. The
header compiles to nothing without SIL_SOURCE_SCOPES=1 (Q19). With it, each
use is one 256-byte site record in the ELF section `sil_source_scopes` and
a link to the observer. The tests compile real sources with the host's cc.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import ROOT
from sil.source_scope_map import read_library

CC = os.environ.get("CC") or shutil.which("cc")
ELF_HOST = platform.system() == "Linux"
needs_cc = pytest.mark.skipif(CC is None, reason="needs a C compiler")
needs_elf = pytest.mark.skipif(not ELF_HOST or CC is None,
                               reason="source scopes need an ELF target")

SOURCE = """\
#include <sil/source_scope.h>

static int depth(int n) {
  SIL_SOURCE_SCOPE(7, "depth");
  if (n == 0) return 0;
  return 1 + depth(n - 1);
}

int run(int n) {
  SIL_SOURCE_SCOPE(1, "run");
  return depth(n);
}
"""


def compile_library(source: str, directory: Path, *flags: str,
                    link: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    """Compile `source` as directory/scoped.c into directory/libscoped.so;
    the same paths for every build, so only the source can differ."""
    (directory / "scoped.c").write_text(source)
    return subprocess.run(
        [CC, "-std=c11", "-O2", "-g", "-fPIC", "-shared",
         "-I", str(ROOT / "include"), *flags, "scoped.c", "-o", "libscoped.so",
         *link], cwd=directory, capture_output=True, text=True)


def without_scopes(source: str) -> str:
    """The same source with each scope line blank, so that line numbers and
    the include stay the same."""
    return "\n".join("" if "SIL_SOURCE_SCOPE(" in line else line
                     for line in source.splitlines()) + "\n"


def compile_object(source: str, directory: Path) -> bytes:
    """Without -g: clang's DWARF 5 line table holds the MD5 of the source
    text, so its debug data differs with any edit of the text."""
    (directory / "scoped.c").write_text(source)
    subprocess.run([CC, "-std=c11", "-O2", "-fPIC", "-c",
                    "-I", str(ROOT / "include"), "scoped.c", "-o", "scoped.o"],
                   cwd=directory, capture_output=True, check=True)
    return (directory / "scoped.o").read_bytes()


@needs_cc
def test_without_the_opt_in_a_scope_is_no_code_and_no_data(tmp_path):
    assert compile_object(SOURCE, tmp_path) == \
        compile_object(without_scopes(SOURCE), tmp_path)


def two_builds(tmp_path: Path, *flags: str) -> tuple[Path, Path]:
    """The library with the scope lines and the one with blank lines."""
    paths = []
    for name, source in (("with", SOURCE),
                         ("without", without_scopes(SOURCE))):
        built = compile_library(source, tmp_path, "-Wl,--build-id=sha1",
                                *flags)
        assert built.returncode == 0, built.stderr
        paths.append((tmp_path / "libscoped.so").rename(
            tmp_path / f"{name}.so"))
    return paths[0], paths[1]


@needs_elf
def test_without_the_opt_in_the_library_has_the_same_bytes(tmp_path):
    # GCC marks the start of each statement in the line table at -O2 -g, and
    # a scope line is a statement even when it declares nothing.
    with_scopes, without = two_builds(tmp_path, "-gno-statement-frontiers")
    assert with_scopes.read_bytes() == without.read_bytes(), \
        differing_sections(with_scopes, without)


@needs_elf
def test_with_statement_markers_only_the_line_table_differs(tmp_path):
    with_scopes, without = two_builds(tmp_path)
    # The build ID hashes the whole file, the line table included.
    assert set(differing_sections(with_scopes, without)) <= {
        ".debug_line", ".note.gnu.build-id"}


def differing_sections(a: Path, b: Path) -> list[str]:
    """The names of the ELF sections whose bytes differ."""
    names = re.findall(r"\]\s+(\S+)", subprocess.run(
        ["readelf", "-SW", str(a)], capture_output=True, text=True).stdout)
    differing = []
    for name in names:
        dumps = [subprocess.run(["readelf", "-x", name, str(f)],
                                capture_output=True).stdout for f in (a, b)]
        if dumps[0] != dumps[1]:
            differing.append(name)
    return differing


@needs_elf
def test_without_the_opt_in_the_library_does_not_link_the_observer(tmp_path):
    built = compile_library(SOURCE, tmp_path)
    assert built.returncode == 0, built.stderr
    dynamic = subprocess.run(["readelf", "-d", "libscoped.so"], cwd=tmp_path,
                             capture_output=True, text=True, check=True)
    assert "sil_source_scope" not in dynamic.stdout
    sections = subprocess.run(["readelf", "-S", "libscoped.so"], cwd=tmp_path,
                              capture_output=True, text=True, check=True)
    assert "sil_source_scopes" not in sections.stdout


INVALID = {
    "reserved-id": 'SIL_SOURCE_SCOPE(0, "zero");',
    "large-id": 'SIL_SOURCE_SCOPE(65536, "large");',
    "long-name": f'SIL_SOURCE_SCOPE(1, "{"n" * 64}");',
    "not-a-literal": "const char *name = \"x\"; SIL_SOURCE_SCOPE(1, name);",
    "twice-in-a-block": 'SIL_SOURCE_SCOPE(1, "a"); SIL_SOURCE_SCOPE(2, "b");',
}


@needs_cc
@pytest.mark.parametrize("opt_in", [False, True], ids=["opt-out", "opt-in"])
@pytest.mark.parametrize("body", INVALID.values(), ids=INVALID.keys())
def test_an_invalid_scope_is_a_compile_error(tmp_path, body, opt_in):
    if opt_in and not ELF_HOST:
        pytest.skip("source scopes need an ELF target")
    if body == INVALID["twice-in-a-block"] and not opt_in:
        pytest.skip("without the opt-in a scope declares nothing")
    source = f"#include <sil/source_scope.h>\nvoid f(void) {{ {body} }}\n"
    flags = ("-DSIL_SOURCE_SCOPES=1",) if opt_in else ()
    built = compile_library(source, tmp_path, *flags)
    assert built.returncode != 0


@needs_elf
def test_a_file_name_longer_than_179_bytes_is_a_compile_error(tmp_path):
    source = (f'#line 1 "{"f" * 180}"\n#include <sil/source_scope.h>\n'
              'void f(void) { SIL_SOURCE_SCOPE(1, "a"); }\n')
    built = compile_library(source, tmp_path, "-DSIL_SOURCE_SCOPES=1")
    assert built.returncode != 0


@needs_cc
@pytest.mark.skipif(ELF_HOST, reason="checks a host without ELF")
def test_the_opt_in_on_a_host_without_elf_is_a_compile_error(tmp_path):
    built = compile_library(SOURCE, tmp_path, "-DSIL_SOURCE_SCOPES=1")
    assert built.returncode != 0
    assert "ELF" in built.stderr


@pytest.fixture
def observer(build_dir) -> Path:
    library = build_dir / "libsil_source_scope.so.1"
    if not library.exists():
        pytest.skip("the observer library is built on Linux only")
    return build_dir


@needs_elf
def test_each_use_is_a_site_record_of_the_scope_map(tmp_path, observer):
    built = compile_library(
        SOURCE, tmp_path, "-DSIL_SOURCE_SCOPES=1",
        link=(f"-L{observer}", "-lsil_source_scope",
              "-Wl,--build-id=sha1"))
    assert built.returncode == 0, built.stderr
    scan = read_library(tmp_path / "libscoped.so")
    # Expected sites: the lines of SOURCE above.
    assert scan.scopes == [
        {"id": 1, "name": "run", "sites": [{"file": "scoped.c", "line": 10}]},
        {"id": 7, "name": "depth", "sites": [{"file": "scoped.c", "line": 4}]},
    ]
    dynamic = subprocess.run(["readelf", "-d", "libscoped.so"], cwd=tmp_path,
                             capture_output=True, text=True, check=True)
    assert "[libsil_source_scope.so.1]" in dynamic.stdout
    relocations = subprocess.run(
        ["readelf", "-r", "libscoped.so"], cwd=tmp_path, capture_output=True,
        text=True, check=True).stdout
    sections = subprocess.run(
        ["readelf", "-SW", "libscoped.so"], cwd=tmp_path,
        capture_output=True, text=True, check=True).stdout
    # The section is read-only data without relocations.
    flags = re.search(r"sil_source_scopes\s+PROGBITS(?:\s+\S+){4}\s+(\S+)",
                      sections)
    assert flags is not None and flags[1] == "A", sections
    assert "sil_source_scopes" not in relocations
