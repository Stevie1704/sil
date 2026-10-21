"""Browser checks of the profile HTML report (#318) in a headless Chrome.

Each page opens from `file://` with network requests failed and counted
(tests/headless_chrome.py). The checks cover layout at phone and desktop
width in light and dark themes, keyboard use of the caller tree and the
filters, Virtual-time filters at their interval boundaries with values above
2^53, long names, many frames, hostile names and the page without
JavaScript. They skip when no Chrome is installed; with SIL_TEST_CHROME set
a missing browser fails instead (as in CI).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from headless_chrome import Chrome, find_chrome
from profile_sets import (
    artifact_set, correlation_document, fixture, samples_document,
)
from sil.profile_html import render_profile
from sil.profile_set import load_profile_set
from test_diagnostics_html_browser import CONTRAST, opened
from test_profile_render import hostile_set

CHROME = find_chrome()
if CHROME is None and os.environ.get("SIL_TEST_CHROME"):
    pytest.fail(f"SIL_TEST_CHROME={os.environ['SIL_TEST_CHROME']!r} is not an "
                "executable browser", pytrace=False)
pytestmark = pytest.mark.skipif(CHROME is None,
                                reason="no Chrome or Chromium installed")

LONG = "ns::" + "VeryLongTemplateName<std::vector<int>>::" * 12 + "spin()"
DEPTH = 300


def deep_set(directory: Path) -> Path:
    """Long names, a 300-frame recursion and 400 sibling functions."""
    report = fixture("activation-complete.json")
    samples = samples_document(report)
    frames = samples["frames"]
    base = len(frames)
    frames += [{"module": None, "symbol": f"{LONG}{i}", "address": "0x1",
                "module_offset": None, "source": None} for i in range(400)]
    recursive = len(frames)
    frames.append({"module": "m1", "symbol": "recurse", "address": "0x2",
                   "module_offset": None, "source": None})
    samples["stacks"] = [[recursive] * DEPTH + [0]] + \
        [[base + i, 0] for i in range(9)]
    for index, row in enumerate(samples["samples"]):
        row[4] = index % len(samples["stacks"])
    correlation = correlation_document(report, samples)
    return artifact_set(directory, "activation-complete.json", report=report,
                        samples=samples, correlation=correlation)


def big_virtual_time_set(directory: Path) -> Path:
    report = fixture("activation-complete.json")
    samples = samples_document(report)
    correlation = correlation_document(report, samples)
    for index, row in enumerate(correlation["activations"]):
        row["virtual_time_ns"] = 2**53 + index  # 2^53 + 1 is not a double
    return artifact_set(directory, "activation-complete.json", report=report,
                        samples=samples, correlation=correlation)


SETS = {
    "cpu": lambda d: artifact_set(d, "cpu-complete.json"),
    "activations": lambda d: artifact_set(d, "activation-complete.json"),
    "unavailable": lambda d: artifact_set(d, "activation-reserved.json"),
    "partial": lambda d: artifact_set(d, "activation-partial-truncated.json"),
    "zero": lambda d: artifact_set(d, "cpu-zero-samples.json"),
    "failed": lambda d: artifact_set(d, "cpu-run-failed.json"),
    "heap": lambda d: artifact_set(d, "heap-complete-runner.json"),
    "heap-failed": lambda d: artifact_set(d, "heap-failed-no-data.json"),
    "deep": deep_set,
    "big-virtual-time": big_virtual_time_set,
    "hostile": lambda d: hostile_set(d),
}


@pytest.fixture(scope="module")
def chrome():
    browser = Chrome(CHROME)
    yield browser
    browser.close()


@pytest.fixture(scope="module")
def pages(tmp_path_factory):
    root = tmp_path_factory.mktemp("profile reports")
    paths = {}
    for name, make in SETS.items():
        profile = make(root / name)
        paths[name] = root / f"{name}.html"
        paths[name].write_text(render_profile(load_profile_set(profile),
                                              profile.read_text()))
    return paths


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("name", list(SETS))
def test_layout_and_contrast(chrome, pages, name, scheme, width):
    chrome.resize(width)
    opened(chrome, pages[name], scheme=scheme)

    assert chrome.blocked_requests == []
    assert chrome.console_errors == []
    assert chrome.evaluate("window.pwned === undefined")
    assert chrome.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth"), \
        "the page scrolls sideways"
    dark = chrome.evaluate("getComputedStyle(document.body).backgroundColor")
    assert (dark == "rgb(18, 20, 23)") == (scheme == "dark")
    low = {selector: round(ratio, 2) for selector, ratio in
           chrome.evaluate(CONTRAST.replace(
               '".facts dt", ".pill", "a"]',
               '".facts dt", ".pill", "a", ".tree-values", ".tree-other",'
               ' ".fn", ".module", ".status-line", ".selection"]')).items()
           if ratio < 4.5}
    assert low == {}


def test_without_javascript_every_table_and_the_tree_read(chrome, pages):
    opened(chrome, pages["activations"], javascript=False)

    assert chrome.evaluate("document.getElementById('explorer').offsetParent"
                           " === null")
    for table in ("self-table", "inclusive-table", "class-table",
                  "tasks-table", "activation-table", "process-table",
                  "module-table"):
        assert chrome.evaluate(
            f"document.getElementById('{table}').offsetParent !== null"), table
    assert chrome.evaluate(
        "document.querySelectorAll('#cpu-tree details').length") > 1
    text = chrome.evaluate("document.body.innerText")
    assert "Run succeeded" in text and "Activation correlation complete" in text


def tree_text(chrome: Chrome) -> str:
    return chrome.evaluate(
        "(() => { document.querySelectorAll('#cpu-tree details')"
        ".forEach(d => d.open = true);"
        " return document.getElementById('cpu-tree').innerText; })()")


@pytest.mark.parametrize("name", ["activations", "deep"])
def test_the_script_tree_without_a_filter_is_the_static_tree(chrome, pages,
                                                            name):
    opened(chrome, pages[name], javascript=False)
    static = tree_text(chrome)
    opened(chrome, pages[name])
    scripted = tree_text(chrome)

    assert scripted == static
    assert "All" in static


def test_many_frames_are_bounded_with_a_visible_remainder(chrome, pages):
    chrome.resize(390)
    opened(chrome, pages["deep"])
    text = tree_text(chrome)

    assert "deeper callees not shown" in text
    assert chrome.evaluate(
        "document.querySelectorAll('#cpu-tree li').length") < 1600
    assert chrome.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth")
    # A long name wraps inside its table cell.
    assert chrome.evaluate(
        "[...document.querySelectorAll('#self-table td')]"
        ".every(td => td.getBoundingClientRect().right <="
        " td.closest('.table-wrap').getBoundingClientRect().right"
        " + td.closest('.table-wrap').scrollWidth)")


def focused(chrome: Chrome) -> str:
    return chrome.evaluate(
        "(e => e.id || e.tagName)(document.activeElement)")


def test_the_keyboard_opens_tree_nodes_and_sets_filters(chrome, pages):
    chrome.resize(1280)
    opened(chrome, pages["activations"])

    chrome.evaluate("document.querySelector('#cpu-tree summary').focus()")
    assert focused(chrome) == "SUMMARY"
    open_before = chrome.evaluate(
        "document.querySelector('#cpu-tree details').open")
    chrome.press("Enter", key_code=13, text="\r")
    assert chrome.evaluate(
        "document.querySelector('#cpu-tree details').open") != open_before
    chrome.press("Enter", key_code=13, text="\r")

    # Choose the first Participant / Task with the keyboard.
    chrome.evaluate("document.getElementById('f-task').focus()")
    # Type-ahead: "p" chooses "plant / step".
    chrome.press("p", code="KeyP", key_code=80, text="p")
    assert chrome.evaluate(
        "document.getElementById('f-task').selectedOptions[0].text") == \
        "plant / step"
    participant = "plant"
    rows = chrome.evaluate(
        "[...document.querySelectorAll('#activation-table tbody tr')]"
        ".filter(r => !r.hidden).map(r => r.cells[0].innerText)")
    assert rows and set(rows) == {participant}
    selection = chrome.evaluate(
        "document.getElementById('selection').innerText")
    assert " of 10 samples" in selection
    assert chrome.evaluate(
        "document.querySelectorAll('#selection-tables table').length") == 2

    chrome.evaluate("document.getElementById('f-clear').click()")
    chrome.evaluate("new Promise(r => setTimeout(r, 50))")
    assert chrome.evaluate(
        "document.getElementById('selection').innerText").startswith(
        "10 of 10 samples")


def visible_virtual_times(chrome: Chrome) -> list[str]:
    return chrome.evaluate(
        "[...document.querySelectorAll('#activation-table tbody tr')]"
        ".filter(r => !r.hidden).map(r => r.cells[3].innerText)")


def set_range(chrome: Chrome, start: str, end: str) -> None:
    chrome.evaluate(
        "(() => { const f = document.getElementById('f-vt-from'),"
        " t = document.getElementById('f-vt-to');"
        f" f.value = {json.dumps(start)}; t.value = {json.dumps(end)};"
        " f.dispatchEvent(new Event('input', {bubbles: true})); })()")


def test_virtual_time_filters_are_half_open(chrome, pages):
    opened(chrome, pages["activations"])

    set_range(chrome, "10000000", "20000000")
    assert set(visible_virtual_times(chrome)) == {"10000000"}
    set_range(chrome, "", "10000000")
    assert set(visible_virtual_times(chrome)) == {"0"}
    set_range(chrome, "30000000", "")
    assert set(visible_virtual_times(chrome)) == {"30000000"}
    set_range(chrome, "x", "")
    assert "whole number" in chrome.evaluate(
        "document.getElementById('selection').innerText")


def test_virtual_times_above_2_53_filter_exactly(chrome, pages):
    opened(chrome, pages["big-virtual-time"])
    first = 2**53

    set_range(chrome, str(first + 1), str(first + 2))
    assert visible_virtual_times(chrome) == [str(first + 1)]


def test_a_filter_with_no_match_says_so(chrome, pages):
    opened(chrome, pages["activations"])

    set_range(chrome, "999999999999", "")
    assert "no sample matches the filters" in chrome.evaluate(
        "document.getElementById('selection').innerText")
    assert "No sample matches the filters." in chrome.evaluate(
        "document.getElementById('cpu-tree').innerText")


def test_an_unavailable_correlation_has_no_activation_filter(chrome, pages):
    opened(chrome, pages["unavailable"])

    assert chrome.evaluate("document.getElementById('f-task') === null")
    assert chrome.evaluate(
        "document.getElementById('explorer').offsetParent !== null")
    assert "No filter by Participant, Task or Virtual time" in \
        chrome.evaluate("document.getElementById('explorer').innerText")


def test_flame_zoom_search_reset_and_keyboard(chrome, pages):
    opened(chrome, pages["activations"])
    chrome.evaluate("document.querySelector('#cpu-flame [data-flame-path=\"0\"]').focus()")
    chrome.press("Enter", key_code=13, text="\r")
    assert "Zoom:" in chrome.evaluate("document.getElementById('flame-total').innerText")
    chrome.evaluate("(() => { const s = document.getElementById('flame-search');"
                    "s.value='spin'; s.dispatchEvent(new Event('input')); })()")
    assert chrome.evaluate("document.querySelectorAll('#cpu-flame .search-match').length") > 0
    assert "matching" in chrome.evaluate("document.getElementById('flame-search-status').innerText")
    chrome.evaluate("document.getElementById('flame-reset').click()")
    assert "Zoom:" not in chrome.evaluate("document.getElementById('flame-total').innerText")
    assert chrome.evaluate("document.getElementById('flame-search').value") == ""
    assert chrome.console_errors == []


def test_timeline_selection_and_task_link_share_filter_totals(chrome, pages):
    opened(chrome, pages["activations"])
    chrome.evaluate("document.querySelector('[data-interval-kind=activation]').focus()")
    chrome.press("Enter", key_code=13, text="\r")
    text = chrome.evaluate("document.getElementById('selection').innerText")
    assert "interval activation 0" in text
    assert "execution_context" in text and "window_associated" in text
    selected_weight = chrome.evaluate("document.getElementById('cpu-flame-svg').dataset.weight")
    assert selected_weight == "2002000"
    assert "2 of 10 samples" in text
    chrome.evaluate("document.querySelector('[data-task-filter][data-class-filter=execution_context]').click()")
    assert chrome.evaluate("document.getElementById('f-class').value") == "execution_context"
    assert chrome.evaluate("document.getElementById('f-task').value") == "0"
    assert "interval activation" not in chrome.evaluate("document.getElementById('selection').innerText")
    assert chrome.console_errors == []


def test_bounded_flame_retains_all_weight_and_full_names_on_focus(chrome, pages):
    opened(chrome, pages["deep"])
    chrome.page("Page.bringToFront")
    assert chrome.evaluate("document.querySelectorAll('#cpu-flame .flame-frame').length") <= 4501
    assert "other / deeper callees not shown" in chrome.evaluate("document.getElementById('cpu-flame').textContent")
    assert chrome.evaluate("document.getElementById('cpu-flame-svg').dataset.weight") == "10010000"
    chrome.evaluate("document.querySelector('#cpu-flame [data-name^=\"ns::\"]').focus()")
    assert LONG in chrome.evaluate("document.getElementById('flame-focus').textContent")
    assert chrome.console_errors == []


def test_zero_samples_and_unavailable_correlation_have_explicit_views(chrome, pages):
    opened(chrome, pages["zero"])
    assert "No sampled weight to draw" in chrome.evaluate("document.getElementById('cpu-flame').textContent")
    assert "Timeline unavailable" in chrome.evaluate("document.getElementById('timeline').textContent")
    opened(chrome, pages["unavailable"])
    assert chrome.evaluate("document.getElementById('timeline-svg') === null")
    assert chrome.evaluate("document.getElementById('task-drilldowns') === null")
    assert chrome.console_errors == []


@pytest.mark.parametrize("javascript", [True, False])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_diff_opens_offline_with_safe_fallback_tables(chrome, tmp_path, javascript, scheme):
    from test_profile_diff import diff
    base = hostile_set(tmp_path / "base")
    new = hostile_set(tmp_path / "new")
    out = tmp_path / "diff.html"
    proc = diff(tmp_path, base, new, "--html", str(out))
    assert proc.returncode == 0, proc.stderr
    opened(chrome, out, javascript=javascript, scheme=scheme)
    assert chrome.evaluate("window.pwned === undefined")
    assert chrome.evaluate("document.querySelector('#diff-functions').offsetParent !== null")
    assert "sampling noise" in chrome.evaluate("document.body.innerText")
    assert chrome.blocked_requests == [] and chrome.console_errors == []
