"""`sil diagnostics render`: the HTML report of a saved report (#304).

Reads a `sil_run_diagnostics` or `sil_benchmark_report` file and writes its
standalone HTML page (sil.diagnostics_html). It executes nothing and needs
neither the Manifest, nor `sil-run`, nor any file next to the input.

Exit codes: 0 when the HTML report was written; 1 when it could not be
written, also because the output path exists or has no directory; 2 for
invalid usage or an input that is missing, corrupt or of an unknown version.
The input is checked first, so invalid input is always 2.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sil.diagnostics_html import render
from sil.diagnostics_report import ReportError, write_new_file
from sil.diagnostics_run import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, GivenOnce

PROG = "sil diagnostics render"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Write the standalone HTML report of a saved "
                    "sil_run_diagnostics or sil_benchmark_report JSON file. "
                    "Nothing is executed again.",
        allow_abbrev=False)
    parser.add_argument("report", type=Path, metavar="INPUT.json")
    parser.add_argument("--html", type=Path, action=GivenOnce, required=True,
                        metavar="OUTPUT.html",
                        help="the new HTML file; an existing file is never "
                             "replaced")
    return parser


def output_problem(path: Path) -> str | None:
    """Why `path` cannot be a new HTML report, or None."""
    if os.path.lexists(path):
        return (f"HTML report '{path}' already exists; a report never "
                "replaces an earlier file")
    if not Path(os.path.realpath(path)).parent.is_dir():
        return f"HTML report '{path}': its directory does not exist"
    return None


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        text = args.report.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return _error(f"cannot read report '{args.report}': {error}",
                      EXIT_USAGE)
    try:
        page = render(text)
    except ReportError as error:
        return _error(f"'{args.report}' is not a report this version can "
                      f"render: {error}", EXIT_USAGE)
    problem = output_problem(args.html)
    if problem is not None:
        return _error(problem, EXIT_FAILURE)
    try:
        write_new_file(args.html, page)
    except OSError as error:
        sys.stderr.write(f"{PROG}: cannot write the HTML report "
                         f"'{args.html}': {error}\n")
        return EXIT_FAILURE
    sys.stdout.write(f"HTML report: {args.html}\n")
    return EXIT_OK


def _error(message: str, code: int) -> int:
    sys.stderr.write(f"{PROG}: error: {message}\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
