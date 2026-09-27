"""Package the example FMU: its description and this host's built binary.

An adopter is handed an FMU; this example has to make one. The archive holds
`modelDescription.xml` and the shared library under the `binaries/` directory
the importer looks in on this host. Every member carries the same fixed
timestamp, so packaging the same binary twice writes the same bytes.

    cc -shared -fPIC -O2 -o EgoMotion.so examples/fmu-replay/ego_motion.c
    python examples/fmu-replay/package.py EgoMotion.so -o EgoMotion.fmu
"""

import argparse
import zipfile
from pathlib import Path

from sil.fmi import library_suffix, platform_directory

EXAMPLE_DIR = Path(__file__).resolve().parent
MODEL_IDENTIFIER = "EgoMotion"
# The earliest time a ZIP member can carry.
_FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def package(binary: Path, out: Path) -> Path:
    """Write the FMU archive around `binary` to `out`."""
    members = {
        "modelDescription.xml":
            (EXAMPLE_DIR / "modelDescription.xml").read_bytes(),
        f"binaries/{platform_directory()}/{MODEL_IDENTIFIER}{library_suffix()}":
            Path(binary).read_bytes(),
    }
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            member = zipfile.ZipInfo(name, date_time=_FIXED_TIME)
            member.compress_type = zipfile.ZIP_DEFLATED
            member.external_attr = 0o644 << 16
            archive.writestr(member, data)
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("binary", type=Path,
                        help="the shared library built from ego_motion.c")
    parser.add_argument("-o", "--output", type=Path, required=True,
                        help="the .fmu archive to write")
    args = parser.parse_args()
    package(args.binary, args.output)
