"""Build and package the ADAS reference FMU: compile, identify, archive.

The FMU is the reference application (`../adas_reference.c`, the source the
Native participant links) behind the FMI 3.0 Co-Simulation interface in
`fmi3_controller.c`. This script compiles both into this host's shared
library and writes an FMU archive whose bytes depend only on the inputs it
records:

- the compiler is named in the identity, and the flags are fixed here;
- the sources are copied into a scratch directory and compiled by relative
  path, so no checkout path reaches the binary;
- the instantiation token is the SHA-256 of the identity, so a change to a
  source, the compiler or a flag is a new token;
- every archive member is stored uncompressed, in name order, with one fixed
  timestamp and mode.

    python examples/adas-reference/fmu/package.py OUT_DIR [--cc cc]

writes `OUT_DIR/AdasReference.fmu` and `OUT_DIR/AdasReference.identity.json`
(the identity plus the archive's SHA-256).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from sil.fmi import library_suffix, platform_directory

FMU_DIR = Path(__file__).resolve().parent
EXAMPLE_DIR = FMU_DIR.parent
ROOT = EXAMPLE_DIR.parents[1]
MODEL_IDENTIFIER = "AdasReference"
PROFILE = "sil.adas-reference.radar-camera"
PROFILE_VERSION = 3

# Each source by the name it is compiled under, and where it comes from.
SOURCES = {
    "adas_reference.c": EXAMPLE_DIR / "adas_reference.c",
    "adas_reference.h": EXAMPLE_DIR / "adas_reference.h",
    "fmi3_controller.c": FMU_DIR / "fmi3_controller.c",
    "fmi3Functions.h": FMU_DIR / "fmi3" / "fmi3Functions.h",
    "fmi3FunctionTypes.h": FMU_DIR / "fmi3" / "fmi3FunctionTypes.h",
    "fmi3PlatformTypes.h": FMU_DIR / "fmi3" / "fmi3PlatformTypes.h",
    "modelDescription.xml": FMU_DIR / "modelDescription.xml",
}
LICENSES = {
    "documentation/licenses/LICENSE-SiL.txt": ROOT / "LICENSE",
    "documentation/licenses/LICENSE-FMI.txt": FMU_DIR / "fmi3" / "LICENSE.txt",
}
# The reference arithmetic is specified without contracted floating-point
# expressions, and only the FMI functions are exported.
FLAGS = ("-std=c11", "-O2", "-fPIC", "-shared", "-ffp-contract=off",
         "-fvisibility=hidden", "-Wall", "-Wextra")
TOKEN_PLACEHOLDER = b"@INSTANTIATION_TOKEN@"
# The earliest time a ZIP member can carry.
_FIXED_TIME = (1980, 1, 1, 0, 0, 0)
_UNIX = 3


class BuildError(RuntimeError):
    """The compiler is missing or rejected the sources."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _compiler_version(cc: str) -> str:
    try:
        result = subprocess.run([cc, "--version"], capture_output=True,
                                text=True, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise BuildError(f"cannot run the compiler {cc!r}: {error}") from error
    return result.stdout.splitlines()[0].strip()


def identity(cc: str, defines: tuple[str, ...] = ()) -> dict:
    """What the archive is built from; its digest is the token.

    `defines` selects a build variant of the sources, such as the
    wrong-sign control (`ADAS_REFERENCE_WRONG_SIGN`); the reference archive
    has none."""
    document = {
        "profile": PROFILE,
        "profile_version": PROFILE_VERSION,
        "model_identifier": MODEL_IDENTIFIER,
        "platform": platform_directory(),
        "compiler": _compiler_version(cc),
        "flags": list(FLAGS),
        "defines": list(defines),
        "sources": {name: _sha256(path.read_bytes())
                    for name, path in SOURCES.items()},
        "licenses": {name: _sha256(path.read_bytes())
                     for name, path in LICENSES.items()},
    }
    canonical = json.dumps(document, sort_keys=True).encode()
    return {**document, "instantiation_token": _sha256(canonical)}


def _compile(cc: str, token: str, defines: tuple[str, ...]) -> bytes:
    """Compile the sources in a scratch directory; return the library."""
    with tempfile.TemporaryDirectory() as scratch:
        work = Path(scratch)
        for name, path in SOURCES.items():
            shutil.copyfile(path, work / name)
        library = f"{MODEL_IDENTIFIER}{library_suffix()}"
        command = [cc, *FLAGS, *(f"-D{define}" for define in defines),
                   f'-DADAS_FMU_INSTANTIATION_TOKEN="{token}"',
                   "-I.", "-o", library, "adas_reference.c",
                   "fmi3_controller.c"]
        if platform.system() == "Linux":
            command.append("-lm")
        result = subprocess.run(command, cwd=work, capture_output=True,
                                text=True)
        if result.returncode != 0:
            raise BuildError(f"{' '.join(command)} failed:\n{result.stderr}")
        return (work / library).read_bytes()


def _members(binary: bytes, document: dict) -> dict[str, bytes]:
    description = SOURCES["modelDescription.xml"].read_bytes()
    if description.count(TOKEN_PLACEHOLDER) != 1:
        raise BuildError("modelDescription.xml must name the token once")
    token = document["instantiation_token"].encode()
    members = {
        "modelDescription.xml": description.replace(TOKEN_PLACEHOLDER, token),
        f"binaries/{platform_directory()}/{MODEL_IDENTIFIER}"
        f"{library_suffix()}": binary,
        "documentation/identity.json":
            (json.dumps(document, indent=2) + "\n").encode(),
    }
    members.update({name: path.read_bytes()
                    for name, path in LICENSES.items()})
    return members


def _write_archive(members: dict[str, bytes], out: Path) -> None:
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as archive:
        for name in sorted(members):
            member = zipfile.ZipInfo(name, date_time=_FIXED_TIME)
            member.create_system = _UNIX
            member.external_attr = 0o100644 << 16
            archive.writestr(member, members[name])


def build(out_dir: Path, cc: str = "cc",
          defines: tuple[str, ...] = ()) -> dict:
    """Write the FMU and its identity into `out_dir`; return the identity."""
    out_dir.mkdir(parents=True, exist_ok=True)
    document = identity(cc, defines)
    binary = _compile(cc, document["instantiation_token"], defines)
    fmu = out_dir / f"{MODEL_IDENTIFIER}.fmu"
    _write_archive(_members(binary, document), fmu)
    built = {**document, "archive_sha256": _sha256(fmu.read_bytes())}
    (out_dir / f"{MODEL_IDENTIFIER}.identity.json").write_text(
        json.dumps(built, indent=2) + "\n")
    return built


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out_dir", type=Path,
                        help="directory to write the FMU and identity to")
    parser.add_argument("--cc", default="cc", help="the C compiler")
    args = parser.parse_args()
    print(json.dumps(build(args.out_dir, args.cc), indent=2))
