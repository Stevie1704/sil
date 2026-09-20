"""Compile the packed source-code FMUs for this platform and pack them back.

Upstream's packaging script produces a *source-code* FMU: sources,
descriptions and headers, with no `binaries/` in it. This is the second half
of the fixture build — compile each archive for the platform this runs on,
prove the result can actually be loaded, and write the archive back.

    python pack_fixture.py <directory-of-fmus>

Two compile-time definitions are applied, and no upstream file is edited.
Both are what this machine class needs to turn upstream's sources into a
loadable shared-object FMU; neither changes what the FMU computes:

- `FMU_IDENTIFIER_H` is the include guard of upstream's `FmuIdentifier.h`,
  which defines `FMI3_FUNCTION_PREFIX` unconditionally and would export
  `DemoCanNodeTriggeredOutput_fmi3InstantiateCoSimulation` instead of
  `fmi3InstantiateCoSimulation`. `fmi3Functions.h` reserves that prefix for
  source and static-library distribution: "For FMUs compiled in a
  DLL/sharedObject, the 'actual' function names are used and
  'FMI3_FUNCTION_PREFIX' must not be defined." Defining the guard makes that
  header a no-op, which is what a shared object needs.
- `_strdup` is MSVC's spelling of POSIX `strdup`. Upstream's `Fmu.c` calls it
  unconditionally, and a C compiler that is not MSVC links a shared object
  with `_strdup` undefined — it builds, and then cannot be loaded at all. The
  definition maps that one call onto the standard function.
"""

from __future__ import annotations

import ctypes
import os
import sys
import zipfile
from pathlib import Path

from fmpy import extract
from fmpy.build import build_platform_binary

# One timestamp for every member, so an FMU's own bytes depend on its contents
# rather than on when it was built.
FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

COMPILE_DEFINITIONS = {
    "FMI_DEFINITIONS:STRING": "FMU_IDENTIFIER_H;_strdup=strdup"
}

# The entry point every importer resolves first. Looking it up is what turns
# "the compiler said nothing" into "this FMU can be driven": a shared object
# links even with an undefined symbol in it, and a prefixed export satisfies
# no importer.
ENTRY_POINT = "fmi3InstantiateCoSimulation"


class BuildError(RuntimeError):
    """A built FMU that no importer could drive."""


def repack(source: Path, destination: Path) -> None:
    """Write one extracted FMU back into an archive, reproducibly."""
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(p for p in source.rglob("*") if p.is_file()):
            member = zipfile.ZipInfo(
                str(path.relative_to(source)), date_time=FIXED_TIMESTAMP
            )
            member.compress_type = zipfile.ZIP_DEFLATED
            executable = os.access(path, os.X_OK)
            member.external_attr = (0o755 if executable else 0o644) << 16
            archive.writestr(member, path.read_bytes())


def platform_binaries(extracted: Path) -> list[str]:
    """The binaries a build left in one extracted FMU, archive-relative."""
    return sorted(
        str(path.relative_to(extracted))
        for path in (extracted / "binaries").rglob("*") if path.is_file()
    )


def check_loadable(binary: Path) -> None:
    """Open the binary with every symbol resolved and find the entry point."""
    library = ctypes.CDLL(str(binary), mode=os.RTLD_NOW)
    if not hasattr(library, ENTRY_POINT):
        raise BuildError(f"{binary.name} exports no {ENTRY_POINT}")


def compile_fmu(fmu: Path) -> list[str]:
    """Compile one source-code FMU in place, and report its binaries."""
    print(f"=== compiling {fmu.name} ===", flush=True)
    extracted = Path(extract(fmu))
    build_platform_binary(extracted, cmake_options=dict(COMPILE_DEFINITIONS))
    binaries = platform_binaries(extracted)
    if not binaries:
        raise BuildError(f"{fmu.name} carries no compiled binary")
    check_loadable(extracted / binaries[0])
    repack(extracted, fmu)
    return binaries


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        raise SystemExit("usage: pack_fixture.py <directory-of-fmus>")
    for fmu in sorted(Path(argv[0]).glob("*.fmu")):
        print(f"{fmu.name} {' '.join(compile_fmu(fmu))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
