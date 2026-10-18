"""The standalone HTML report of a saved diagnostics report (#304).

`render(text)` turns the text of a `sil_run_diagnostics` or a
`sil_benchmark_report` document into one HTML file that opens offline, from
`file://`, with no other file. The HTML presents the saved JSON and nothing
else: every value is a value of the report, formatted as the terminal
summary formats it (sil.diagnostics_format), and no statistic is computed
again.

Python writes the complete page, so its status, cards, tables and charts
(inline SVG) need no JavaScript. The inline script only adds conveniences:
sorting and filtering tables, the Run selector, the theme switch and a Blob
download. The source JSON is embedded once, base64-encoded in the download
link, so report content never reaches a parser as markup or script, and the
download is the source byte for byte. A Content-Security-Policy that allows
only the page's own style and script blocks any network request.

This module builds the page around a body from sil.diagnostics_html_run or
sil.diagnostics_html_benchmark; sil.diagnostics_html_markup holds the
escaping element builders and sil.diagnostics_html_charts the SVG charts.
"""

from __future__ import annotations

import base64
import hashlib
from importlib.resources import files
from pathlib import Path

from sil.diagnostics_html_benchmark import benchmark_page
from sil.diagnostics_html_markup import Html, tag, void
from sil.diagnostics_html_run import run_page
from sil.diagnostics_report import (
    BENCHMARK_REPORT_KEY, ReportError, parse_report, write_new_file,
)


ASSETS = files("sil").joinpath("report_assets")


def render(text: str) -> str:
    """The HTML page of a report text; raise ReportError if it is invalid."""
    document = parse_report(text)
    if BENCHMARK_REPORT_KEY in document:
        title, body = "SiL benchmark report", benchmark_page(document)
    else:
        title, body = "SiL Run diagnostics", run_page(document)
    return _page(title, body, text)


def write_html(text: str, path: Path) -> str | None:
    """Render `text` into the new file `path`; return why not, or None."""
    try:
        write_new_file(path, render(text))
    except OSError as error:
        return str(error)
    except ReportError as error:
        return f"the report cannot be rendered: {error}"
    except Exception as error:  # noqa: BLE001
        # A renderer bug must not replace the Run's exit code, which the
        # caller still has to return; name it as a bug instead of hiding it.
        return (f"internal error while rendering ({type(error).__name__}: "
                f"{error}); please report it")
    return None


def _asset(name: str) -> str:
    return ASSETS.joinpath(name).read_text(encoding="utf-8")


def _digest(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()


def _page(title: str, body: Html, source: str) -> str:
    style, script = _asset("report.css"), _asset("report.js")
    policy = ("default-src 'none'; img-src data:; "
              f"style-src 'sha256-{_digest(style)}'; "
              f"script-src 'sha256-{_digest(script)}'")
    download = "data:application/json;base64," + base64.b64encode(
        source.encode("utf-8")).decode("ascii")
    toolbar = tag(
        "div",
        tag("a", "Download JSON", href=download, download="report.json",
            class_="button", id="download"),
        tag("button", "Theme: auto", type="button", id="theme", hidden=True,
            aria_label="Switch between automatic, light and dark theme"),
        tag("button", "Print", type="button", id="print", hidden=True),
        class_="toolbar")
    return ("<!doctype html>\n" + tag(
        "html",
        tag("head",
            void("meta", charset="utf-8"),
            void("meta", name="viewport",
                 content="width=device-width, initial-scale=1"),
            void("meta", http_equiv="Content-Security-Policy",
                 content=policy),
            tag("title", title),
            Html(f"<style>{style}</style>")),
        tag("body",
            tag("a", "Skip to content", href="#main", class_="skip"),
            tag("header", tag("div", tag("p", "SiL", class_="brand"),
                              toolbar, class_="topbar"),
                class_="site"),
            tag("main", body, id="main"),
            Html(f"<script>{script}</script>")),
        lang="en") + "\n")
