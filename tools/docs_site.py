"""Stage the documentation site from the canonical Markdown (#401).

    python3 tools/docs_site.py stage --output DIR --revision SHA

Copies the published documents of the checkout into DIR, at their checkout
paths, for MkDocs to build. The landing page `site/index.md` becomes the site
root. Nothing here is a maintained copy: run it before every build.
"""

from __future__ import annotations

import argparse
import posixpath
import re
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

LANDING = "site/index.md"
# Root documents the guides cite as contracts. README.md stays on GitHub:
# the landing page takes the site root.
ROOT_PAGES = ("GLOSSARY.md", "SUPPORT.md", "CONTRIBUTING.md", "SECURITY.md")
# Repository instructions for coding agents, not documentation for readers.
UNPUBLISHED = ("docs/agents/",)
PROSPECTIVE = ("docs/proposals/", "docs/research/")
PROSPECTIVE_NOTICE = (
    '!!! warning "Prospective material"\n'
    "    This page describes prospective work or an investigation,\n"
    "    not implemented or supported behavior.\n")
PAGE_SUFFIXES = {".md", ".html", ".json"}
REPOSITORY = "https://github.com/Stevie1704/sil"
MARKDOWN_LINK = re.compile(r"\]\(([^)\s]+)\)")
FENCE = re.compile(r" *(`{3,}|~{3,})")
URL_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")


class SiteError(Exception):
    """The checkout cannot be published as it is."""


def published(root: Path) -> dict[str, str]:
    """Map each published checkout path to its path in the site."""
    pages = {LANDING: "index.md"}
    pages.update({name: name for name in ROOT_PAGES if (root / name).is_file()})
    for path in sorted((root / "docs").rglob("*")):
        name = path.relative_to(root).as_posix()
        if (path.is_file() and path.suffix in PAGE_SUFFIXES
                and not name.startswith(UNPUBLISHED)):
            pages[name] = name
    return pages


def site_link(root: Path, pages: dict[str, str], source: str, target: str,
              revision: str) -> str:
    """Rewrite one link of `source` for the site.

    A published page gets a relative site link. Other checkout files get
    their GitHub source at `revision`.
    """
    if target.startswith("#") or URL_SCHEME.match(target):
        return target
    path, hash_sign, fragment = target.partition("#")
    linked = posixpath.normpath(posixpath.join(posixpath.dirname(source), path))
    if linked in pages:
        relative = posixpath.relpath(pages[linked],
                                     posixpath.dirname(pages[source]) or ".")
        return f"{relative}{hash_sign}{fragment}"
    if linked.startswith("../") or not (root / linked).exists():
        raise SiteError(f"{source}: link {target!r} names no file in the checkout")
    kind = "tree" if (root / linked).is_dir() else "blob"
    slash = "/" if path.endswith("/") else ""
    return f"{REPOSITORY}/{kind}/{revision}/{linked}{slash}{hash_sign}{fragment}"


def rewrite_links(text: str, rewrite: Callable[[str], str]) -> str:
    """Apply `rewrite` to each link target outside fenced code blocks."""
    lines, fence = [], ""
    for line in text.splitlines(keepends=True):
        marker = FENCE.match(line)
        if marker and (not fence or marker[1].startswith(fence)):
            fence = "" if fence else marker[1]
        elif not fence:
            line = MARKDOWN_LINK.sub(lambda link: f"]({rewrite(link[1])})", line)
        lines.append(line)
    return "".join(lines)


def mark_prospective(text: str) -> str:
    """Put the prospective-material notice below the page title."""
    title, newline, body = text.partition("\n")
    return f"{title}{newline}\n{PROSPECTIVE_NOTICE}{body}"


def stage(root: Path, out: Path, revision: str) -> None:
    """Write the site sources of the checkout `root` into `out`."""
    if out.exists():
        shutil.rmtree(out)
    pages = published(root)
    for source, target in pages.items():
        destination = out / target
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.endswith(".md"):
            text = rewrite_links(
                (root / source).read_text(),
                lambda target: site_link(root, pages, source, target, revision))
            if source.startswith(PROSPECTIVE):
                text = mark_prospective(text)
            destination.write_text(text)
        else:
            shutil.copyfile(root / source, destination)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    stage_command = commands.add_parser("stage", help="write the site sources")
    stage_command.add_argument("--output", type=Path, required=True)
    stage_command.add_argument("--revision", required=True)
    args = parser.parse_args(argv)
    try:
        stage(Path(__file__).resolve().parents[1], args.output, args.revision)
    except SiteError as error:
        print(f"docs_site: error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
