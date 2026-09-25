"""Stage the released FMU with its digests and build identity.

    python models/can/release.py FIRST.fmu REPEAT.fmu OUTDIR IMAGE_ID_FILE

FIRST and REPEAT are two controlled builds of one source tree. Any byte of
difference is unresolved build variability, so nothing is staged. OUTDIR
receives the versioned archive, `release.json` and `SHA256SUMS`. Standard
library only.
"""

import json
import shutil
import sys
import zipfile
from pathlib import Path

from build_support import PROFILE, digest

PLATFORM = "x86_64-linux"


def identity_of(archive):
    with zipfile.ZipFile(archive) as fmu:
        return json.loads(fmu.read("resources/identity.json"))


def release_record(archive, name, image):
    identity = identity_of(archive)
    return {
        "product": PROFILE["model_name"],
        "version": PROFILE["version"],
        "platform": PLATFORM,
        "specification": PROFILE["specification"],
        "spec_revision": PROFILE["upstream"]["spec"]["revision"],
        "fmu": {"file": name, "sha256": digest(archive)},
        "reproducibility": {"controlled_builds": 2, "byte_identical": True},
        "build": {
            "git_revision": identity["git_revision"],
            "source_dirty": identity["source_dirty"],
            "compiler": identity["compiler"],
            "flags": identity["flags"],
            "fmi_headers": f"FMPy {identity['fmpy']}",
            "image": image,
        },
        "sources": identity["sources"],
        # A build from uncommitted sources has no revision to reproduce it from.
        "releasable": not identity["source_dirty"],
    }


def stage(first, repeat, outdir, image_file):
    if first.read_bytes() != repeat.read_bytes():
        raise SystemExit(
            f"{first.name} and {repeat.name} differ: resolve the build "
            "variability before claiming a reproducible release"
        )
    name = f"{PROFILE['model_name']}-{PROFILE['version']}-{PLATFORM}.fmu"
    outdir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(first, outdir / name)
    record = release_record(first, name, image_file.read_text().strip())
    (outdir / "release.json").write_text(json.dumps(record, indent=2) + "\n")
    (outdir / "SHA256SUMS").write_text("".join(
        f"{digest(outdir / file)}  {file}\n" for file in (name, "release.json")
    ))


if __name__ == "__main__":
    stage(*(Path(argument) for argument in sys.argv[1:5]))
