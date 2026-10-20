"""`sil profile render`: present a saved profile offline (#318).

Reads `profile.json` and the normalized files it names (sil.profile_set),
prints the terminal summary of `sil profile` and, with `--html`, writes the
standalone HTML report (sil.profile_html). It executes nothing: no Run, no
profiled binary, no perf or Heaptrack.

Exit codes: 0 when the summary was printed and the HTML report, if asked
for, was written; 1 when the HTML report cannot be written; 2 for invalid
usage, an output path that exists or has no directory, or a set that is
missing, corrupt, inconsistent or of an unknown version. The output path is
checked before the set is read or rendered.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sil.diagnostics_report import new_path_problem, write_new_file
from sil.diagnostics_run import (
    EXIT_FAILURE, EXIT_OK, EXIT_USAGE, GivenOnce, command_error,
)
from sil.profile_html import render_profile
from sil.profile_set import ProfileSetError, load_profile_set
from sil.profile_summary import format_summary

PROG = "sil profile render"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Print the summary of a saved profile and optionally "
                    "write its standalone HTML report. Reads profile.json "
                    "and the normalized files it names; executes nothing.",
        allow_abbrev=False)
    parser.add_argument("profile", type=Path, metavar="DIR/profile.json")
    parser.add_argument("--html", type=Path, action=GivenOnce,
                        metavar="OUTPUT.html",
                        help="the new HTML file; an existing file is never "
                             "replaced")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    if args.html is not None:
        problem = new_path_problem("HTML report", args.html)
        if problem is not None:
            return command_error(PROG, problem, EXIT_USAGE)
    try:
        profile = load_profile_set(args.profile)
    except ProfileSetError as error:
        return command_error(
            PROG, f"'{args.profile}' is not a profile this version can "
            f"present: {error}", EXIT_USAGE)
    page = None
    if args.html is not None:
        source = args.profile.read_text(encoding="utf-8")
        page = render_profile(profile, source)
    sys.stdout.write(format_summary(profile.report, args.profile))
    if page is not None:
        try:
            write_new_file(args.html, page)
        except OSError as error:
            sys.stderr.write(f"{PROG}: cannot write the HTML report "
                             f"'{args.html}': {error}\n")
            return EXIT_FAILURE
        sys.stdout.write(f"HTML report: {args.html}\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
