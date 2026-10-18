"""Browser checks of the HTML report in an isolated headless Chrome (#304).

Each page opens from `file://` with network requests failed and counted. The
checks cover layout (no sideways page scroll at phone and desktop width),
light and dark themes with readable contrast, keyboard use, the page with
JavaScript disabled, and print. They skip when no Chrome is installed
(set SIL_TEST_CHROME to a browser executable to choose one).
"""

import json
from pathlib import Path

import pytest

from headless_chrome import Chrome, find_chrome
from sil.diagnostics_html import render
from test_diagnostics_html import (
    EVIL, fixture_text, hostile, many_participants, one_sample,
)

CHROME = find_chrome()
pytestmark = pytest.mark.skipif(CHROME is None,
                                reason="no Chrome or Chromium installed")

PAGES = {
    "successful": lambda: fixture_text("run-success.json"),
    "failed": lambda: fixture_text("run-failed-partial.json"),
    "empty": lambda: fixture_text("run-zero-activations.json"),
    "unsupported": lambda: fixture_text("run-unsupported-metric.json"),
    "one-sample": one_sample,
    "many-participants": many_participants,
    "benchmark": lambda: fixture_text("benchmark-complete.json"),
    "incomplete": lambda: fixture_text("benchmark-partial.json"),
    "no-success": lambda: fixture_text("benchmark-no-successful-run.json"),
    "hostile": lambda: hostile("benchmark-partial.json"),
}
STATUS = {
    "successful": "Succeeded", "failed": "Run failure",
    "empty": "Succeeded", "unsupported": "Succeeded",
    "one-sample": "Succeeded", "many-participants": "Succeeded",
    "benchmark": "Complete benchmark",
    "incomplete": "Incomplete benchmark: run failed",
    "no-success": "Incomplete benchmark: warmup failed",
    "hostile": "Incomplete benchmark: run failed",
}

# WCAG 2 contrast of the text colour of each selector on its page colour.
CONTRAST = """(() => {
  function rgb(text) { return text.match(/[\\d.]+/g).slice(0, 4).map(Number); }
  function lum(c) {
    const v = c.slice(0, 3).map(x => { x /= 255;
      return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); });
    return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2];
  }
  function background(el) {
    for (; el; el = el.parentElement) {
      const c = rgb(getComputedStyle(el).backgroundColor);
      if (c.length < 4 || c[3] > 0) return c;
    }
    return rgb(getComputedStyle(document.body).backgroundColor);
  }
  const worst = {};
  for (const sel of ["body", "h2", ".note", ".card-detail", ".card-label",
                     "th", "td", ".na", ".banner-detail", "caption",
                     ".facts dt", ".pill", "a"]) {
    for (const el of document.querySelectorAll(sel)) {
      if (!el.offsetParent && sel !== "body") continue;
      const fg = rgb(getComputedStyle(el).color), bg = background(el);
      const [a, b] = [lum(fg), lum(bg)].sort((x, y) => y - x);
      const ratio = (a + 0.05) / (b + 0.05);
      worst[sel] = Math.min(worst[sel] || 99, ratio);
    }
  }
  return worst;
})()"""


@pytest.fixture(scope="module")
def chrome():
    browser = Chrome(CHROME)
    yield browser
    browser.close()


@pytest.fixture(scope="module")
def pages(tmp_path_factory):
    directory = tmp_path_factory.mktemp("html reports")
    paths = {}
    for name, source in PAGES.items():
        paths[name] = directory / f"{name}.html"
        paths[name].write_text(render(source()))
    return paths


def opened(chrome: Chrome, path: Path, **options) -> Chrome:
    chrome.blocked_requests.clear()
    chrome.console_errors.clear()
    chrome.open(path, **options)
    return chrome


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("name", list(PAGES))
def test_layout_and_contrast(chrome, pages, name, scheme, width):
    chrome.resize(width)
    opened(chrome, pages[name], scheme=scheme)

    assert chrome.blocked_requests == []
    assert chrome.console_errors == []
    assert "pwned" not in json.dumps(chrome.evaluate("Object.keys(window)"))
    assert chrome.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth"), \
        "the page scrolls sideways"
    banner = chrome.evaluate("document.querySelector('.banner h1').innerText")
    assert STATUS[name] in banner
    dark = chrome.evaluate(
        "getComputedStyle(document.body).backgroundColor")
    assert (dark == "rgb(18, 20, 23)") == (scheme == "dark")
    contrast = chrome.evaluate(CONTRAST)
    low = {selector: round(ratio, 2) for selector, ratio in contrast.items()
           if ratio < 4.5}
    assert low == {}
    for label in chrome.evaluate(
            "[...document.querySelectorAll('svg')].map(s => "
            "s.getAttribute('role') + ':' + s.getAttribute('aria-label'))"):
        role, text = label.split(":", 1)
        assert role == "img" and text not in ("", "null")


def test_hostile_names_stay_text(chrome, pages):
    opened(chrome, pages["hostile"])

    assert chrome.evaluate("window.pwned === undefined")
    assert chrome.evaluate("document.querySelectorAll('img').length") == 0
    assert EVIL in chrome.evaluate("document.body.innerText")


@pytest.mark.parametrize("name", ["successful", "incomplete"])
def test_core_content_reads_without_javascript(chrome, pages, name):
    opened(chrome, pages[name], javascript=False)
    text = chrome.evaluate("document.body.innerText")

    assert STATUS[name] in text
    assert chrome.evaluate(
        "[...document.querySelectorAll('table')].every(t => "
        "t.offsetParent !== null || t.closest('details'))")
    # Controls that need the script stay hidden; every Run is shown.
    assert chrome.evaluate(
        "[...document.querySelectorAll('input[data-filter], #theme, #print')]"
        ".every(e => e.offsetParent === null)")
    assert chrome.evaluate(
        "[...document.querySelectorAll('.run-detail')]"
        ".every(e => !e.hidden)")
    assert chrome.evaluate(
        "document.getElementById('download').href.startsWith('data:')")


def focused(chrome: Chrome) -> str:
    return chrome.evaluate(
        "(e => e.id || e.className || e.tagName)(document.activeElement)")


def test_keyboard_reaches_and_operates_the_controls(chrome, pages):
    chrome.resize(1280)
    opened(chrome, pages["benchmark"])

    chrome.tab()
    assert focused(chrome) == "skip"
    chrome.tab()
    assert focused(chrome) == "download"
    chrome.tab()
    assert focused(chrome) == "theme"
    chrome.press("Enter", key_code=13, text="\r")
    assert chrome.evaluate(
        "document.documentElement.getAttribute('data-theme')") == "light"
    chrome.press("Enter", key_code=13, text="\r")
    assert chrome.evaluate(
        "document.documentElement.getAttribute('data-theme')") == "dark"

    # Sort the Runs table by wall time with the keyboard.
    chrome.evaluate("document.querySelectorAll('#runs-table th button')[6]"
                    ".focus()")
    chrome.press("Enter", key_code=13, text="\r")
    keys = chrome.evaluate(
        "[...document.querySelectorAll('#runs-table tbody tr')].map("
        "r => r.cells[6].getAttribute('data-sort'))")
    assert [int(k) for k in keys] == sorted(int(k) for k in keys)
    assert chrome.evaluate(
        "document.querySelectorAll('#runs-table th')[6]"
        ".getAttribute('aria-sort')") == "ascending"

    # Filter a table by typing.
    chrome.evaluate("document.querySelector('[data-filter=\"runs-table\"]')"
                    ".focus()")
    chrome.page("Input.insertText", text="warmup")
    assert chrome.evaluate(
        "[...document.querySelectorAll('#runs-table tbody tr')]"
        ".filter(r => !r.hidden).length") == 1
    assert "1 of" in chrome.evaluate(
        "document.getElementById('runs-table-count').innerText")

    # Choose one Run with the selector.
    chrome.evaluate("document.getElementById('run-select').focus()")
    chrome.press("ArrowDown", key_code=40)
    visible = chrome.evaluate(
        "[...document.querySelectorAll('.run-detail')]"
        ".filter(e => !e.hidden).map(e => e.dataset.run)")
    assert len(visible) == 1
    chrome.evaluate("localStorage.clear()")


def test_large_integers_sort_exactly(chrome, pages, tmp_path):
    report = json.loads(fixture_text("run-success.json"))
    slowest = report["observational"]["tasks"][0]["timing"]["slowest"]
    slowest[0]["wall_ns"] = 2**53 + 3
    slowest[1]["wall_ns"] = 2**53 + 2
    path = tmp_path / "big.html"
    path.write_text(render(json.dumps(report)))
    opened(chrome, path)

    chrome.evaluate("document.querySelectorAll('#slowest-table th button')[5]"
                    ".click()")
    chrome.evaluate("document.querySelectorAll('#slowest-table th button')[5]"
                    ".click()")  # descending
    keys = chrome.evaluate(
        "[...document.querySelectorAll('#slowest-table tbody tr')].slice(0, 2)"
        ".map(r => r.cells[5].getAttribute('data-sort'))")
    assert keys == [str(2**53 + 3), str(2**53 + 2)]


def test_print_shows_every_run_and_hides_controls(chrome, pages):
    opened(chrome, pages["benchmark"], media="print")

    assert chrome.evaluate(
        "[...document.querySelectorAll('.run-detail')]"
        ".every(e => getComputedStyle(e).display !== 'none')")
    assert chrome.evaluate(
        "getComputedStyle(document.querySelector('header.site')).display"
    ) == "none"
    assert chrome.pdf().startswith(b"%PDF")


def test_the_download_is_a_blob_of_the_source(chrome, pages):
    # The page's own policy forbids fetch; only this check reads the Blob.
    chrome.page("Page.setBypassCSP", enabled=True)
    try:
        opened(chrome, pages["successful"])
        text = chrome.evaluate("fetch(document.getElementById('download')"
                               ".href).then(r => r.text())")
    finally:
        chrome.page("Page.setBypassCSP", enabled=False)
    assert chrome.evaluate(
        "document.getElementById('download').href.startsWith('blob:')")
    assert text == fixture_text("run-success.json")
