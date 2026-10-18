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
import sys
from pathlib import Path

from sil.diagnostics_html import render
from sil.diagnostics_report import (
    ReportError, new_path_problem, write_new_file,
)
from sil.diagnostics_run import (
    EXIT_FAILURE, EXIT_OK, EXIT_USAGE, GivenOnce, command_error,
)

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


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        text = args.report.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return command_error(
            PROG, f"cannot read report '{args.report}': {error}", EXIT_USAGE)
    try:
        page = render(text)
    except ReportError as error:
        return command_error(
            PROG, f"'{args.report}' is not a report this version can render: "
            f"{error}", EXIT_USAGE)
    problem = new_path_problem("HTML report", args.html)
    if problem is not None:
        return command_error(PROG, problem, EXIT_FAILURE)
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
