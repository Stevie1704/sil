"""Offline diff tables remain usable with benchmark context and wide columns."""

import json
import os

import pytest

from headless_chrome import Chrome, find_chrome
from profile_sets import artifact_set
from test_diagnostics_html import FIXTURES
from test_diagnostics_html_browser import CONTRAST, opened
from test_profile_diff import diff

CHROME = find_chrome()
if CHROME is None and os.environ.get("SIL_TEST_CHROME"):
    pytest.fail("SIL_TEST_CHROME does not name an executable browser", pytrace=False)
pytestmark = pytest.mark.skipif(CHROME is None, reason="no Chrome or Chromium installed")


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width", [390, 1280])
def test_diff_benchmark_offline_tables_themes_keyboard_and_no_javascript(tmp_path, scheme, width):
    profile = artifact_set(tmp_path / "profile", "cpu-complete.json")
    benchmark = json.loads((FIXTURES / "benchmark-complete.json").read_text())
    benchmark["identity"]["source_revision"] = "</script><script>window.pwned=1</script>"
    path = tmp_path / "benchmark.json"
    path.write_text(json.dumps(benchmark))
    proc = diff(tmp_path, profile, profile, "--base-benchmark", str(path),
                "--new-benchmark", str(path), "--html", "diff.html")
    assert proc.returncode == 0, proc.stderr
    browser = Chrome(CHROME)
    try:
        browser.resize(width)
        opened(browser, tmp_path / "diff.html", scheme=scheme)
        assert browser.blocked_requests == []
        assert browser.console_errors == []
        assert browser.evaluate("window.pwned === undefined")
        assert browser.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert all(value >= 4.5 for value in browser.evaluate(CONTRAST).values())
        browser.evaluate("document.querySelector('#BASE-benchmark-metrics th button').focus()")
        browser.press("Enter", key_code=13, text="\r")
        assert browser.evaluate("document.querySelector('#BASE-benchmark-metrics th').getAttribute('aria-sort')") in ("ascending", "descending")
        opened(browser, tmp_path / "diff.html", javascript=False, scheme=scheme)
        text = browser.evaluate("document.body.innerText")
        assert "Self estimated weight BASE (ns)" in text
        assert "Separate repeated experiments" in text
        assert "Per-Run Task observations (ns)" in text
    finally:
        browser.close()
