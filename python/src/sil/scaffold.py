"""`sil new participant`: write a minimal Participant and its Manifest builder.

The generated files run as written: build the Manifest with `manifest.py`,
and `sil run` and `sil check` accept it. They are a starting point to edit,
not a framework: nothing in them is regenerated or read again by this tool.
"""

from __future__ import annotations

import argparse
import sys
from importlib.resources import files
from pathlib import Path
from string import Template

from ._schema_types import ident_problem

EXIT_USAGE = 2

FILES = {
    "process": ("participant.py", "manifest.py"),
    "native": ("participant.c", "manifest.py"),
}


def write_participant(kind: str, name: str, directory: Path) -> list[Path]:
    """Write the files of a `kind` Participant called `name` into `directory`.

    Refuses, and writes nothing, if one of the files exists already.
    """
    targets = [directory / filename for filename in FILES[kind]]
    existing = [str(path) for path in targets if path.exists()]
    if existing:
        raise FileExistsError("will not overwrite " + ", ".join(existing))
    templates = files("sil").joinpath("templates", kind)
    directory.mkdir(parents=True, exist_ok=True)
    for target in targets:
        text = templates.joinpath(target.name + ".tmpl").read_text()
        target.write_text(Template(text).substitute(name=name))
    return targets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sil new participant",
        description="Write a minimal Participant and a Manifest builder "
                    "script that runs it.",
        allow_abbrev=False,
    )
    kind = parser.add_mutually_exclusive_group(required=True)
    kind.add_argument("--process", metavar="NAME",
                      help="a Python Process participant")
    kind.add_argument("--native", metavar="NAME",
                      help="a C Native participant")
    parser.add_argument("--dir", type=Path, default=None,
                        help="where to write the files (default: ./NAME)")
    args = parser.parse_args(argv)
    kind_name = "process" if args.process else "native"
    name = args.process or args.native
    # The name becomes a Participant, Channel and Schema name, and appears in
    # C source, so it must be a valid identifier in both languages.
    if problem := ident_problem(name):
        sys.stderr.write(f"sil new participant: error: name {name!r} "
                         f"{problem}\n")
        return EXIT_USAGE
    directory = args.dir if args.dir is not None else Path(name)
    try:
        written = write_participant(kind_name, name, directory)
    except OSError as error:
        sys.stderr.write(f"sil new participant: error: {error}\n")
        return EXIT_USAGE
    for path in written:
        sys.stdout.write(f"wrote {path}\n")
    manifest = directory / "manifest.json"
    sys.stdout.write(
        "next:\n"
        f"  python3 {directory / 'manifest.py'} {manifest}\n"
        f"  sil manifest validate {manifest}\n"
        f"  sil check {manifest}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
