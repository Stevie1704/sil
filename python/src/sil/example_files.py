"""`sil examples`: list, locate or copy the examples of the installed release.

The exported directory keeps the layout used by `sil docs`: choose
`--dir examples` to run the guides' commands from its parent directory.
Bundle preparation needs sibling examples, so the export includes them all.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from importlib.resources import files
from pathlib import Path

PROG = "sil examples"
EXAMPLES = Path(str(files("sil").joinpath("_examples")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="List the examples of this SiL version. Copy them into "
                    "a new or empty directory with --dir, or locate the "
                    "installed originals with --path.",
        allow_abbrev=False,
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--dir", type=Path, metavar="DIR",
                        help="copy all examples into DIR; use 'examples' "
                             "for the paths in sil docs")
    action.add_argument("--path", action="store_true",
                        help="print the installed examples directory")
    args = parser.parse_args(argv)
    try:
        topics = json.loads((EXAMPLES / "topics.json").read_text())
    except OSError:
        sys.stderr.write(f"{PROG}: error: this installation has no examples; "
                         "a release wheel or the container image has them\n")
        return 1
    if args.path:
        print(EXAMPLES)
    elif args.dir is None:
        width = max(map(len, topics))
        sys.stdout.write("examples:\n" + "".join(
            f"  {name:<{width}}  {summary}\n"
            for name, summary in topics.items()))
        sys.stdout.write("\nCopy with 'sil examples --dir examples'; "
                         "read 'sil docs examples' for the workflows.\n")
    else:
        destination = args.dir
        try:
            if destination.resolve().is_relative_to(EXAMPLES.resolve()):
                raise ValueError("choose a directory outside the installed examples")
            if destination.exists() and (
                    not destination.is_dir() or any(destination.iterdir())):
                raise FileExistsError(
                    f"{destination} is not an empty directory")
            destination.mkdir(parents=True, exist_ok=True)
            for name in topics:
                shutil.copytree(EXAMPLES / name, destination / name,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        except (OSError, ValueError) as error:
            sys.stderr.write(f"{PROG}: error: {error}\n")
            return 2
        print(destination)
    return 0


if __name__ == "__main__":
    sys.exit(main())
