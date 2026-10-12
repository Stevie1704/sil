"""`sil skill install`: give a coding agent the SiL skill of this version.

The release build (`tools/release.py stamp-python`) writes the skill into the
package, so it always describes the installed version. The install copies it
into `.agents/skills/sil/` of the project, or of the home directory with
`--user`, and links `.claude/skills/sil` to that copy for Claude Code. A source
checkout that was not stamped has no skill.
"""

from __future__ import annotations

import argparse
import os
import sys
from importlib.resources import files
from pathlib import Path

PROG = "sil skill install"
EXIT_FAIL = 1
EXIT_USAGE = 2
SKILL = Path(str(files("sil").joinpath("_skill", "SKILL.md")))
SKILL_DIR = Path(".agents", "skills", "sil")
CLAUDE_LINK = Path(".claude", "skills", "sil")


def install(skill: str, directory: Path) -> Path:
    """Write `skill` into `directory`, over an older copy, and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "SKILL.md"
    target.write_text(skill)
    return target


def link(link_path: Path, directory: Path) -> None:
    """Point `link_path` at `directory` with a relative link, over an older link."""
    link_path.parent.mkdir(parents=True, exist_ok=True)
    if link_path.is_symlink():
        link_path.unlink()
    link_path.symlink_to(os.path.relpath(directory, link_path.parent))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Copy the SiL agent skill of this version into "
                    f"{SKILL_DIR}/ of the current directory, and link "
                    f"{CLAUDE_LINK} to it. Run it again after an upgrade.",
        allow_abbrev=False,
    )
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--dir", type=Path, default=None,
                       help="write the skill into this directory instead, "
                            "and make no link")
    where.add_argument("--user", action="store_true",
                       help="install into the home directory, for every project")
    args = parser.parse_args(argv)
    try:
        skill = SKILL.read_text()
    except OSError:
        sys.stderr.write(f"{PROG}: error: this installation has no skill; "
                         "a release wheel or the container image has it\n")
        return EXIT_FAIL
    if args.dir is not None:
        directory, link_path = args.dir.absolute(), None
    else:
        base = Path.home() if args.user else Path.cwd()
        directory, link_path = base / SKILL_DIR, base / CLAUDE_LINK
    if link_path is not None and link_path.exists() and not link_path.is_symlink():
        sys.stderr.write(f"{PROG}: error: will not replace {link_path}: it is "
                         "not a link; remove it, or use --dir\n")
        return EXIT_USAGE
    try:
        print(install(skill, directory))
        if link_path is not None:
            link(link_path, directory)
            print(link_path)
    except OSError as error:
        sys.stderr.write(f"{PROG}: error: {error}\n")
        return EXIT_FAIL
    if args.user:
        sys.stderr.write(f"{PROG}: a project that pinned another SiL version "
                         "needs its own copy: run it without --user there\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
