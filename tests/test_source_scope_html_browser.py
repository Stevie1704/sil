"""The source-scope HTML view in a browser: offline, both themes, keyboard
operation and escaped text (#373, contract rows Q23 and Q24)."""

import os

import pytest

from headless_chrome import Chrome, find_chrome
from test_diagnostics_html_browser import CONTRAST, opened
from test_source_scope_render import CASES, MAP, SELECT_0, capture_dir

from sil import profile_render

CHROME = find_chrome()
if CHROME is None and os.environ.get("SIL_TEST_CHROME"):
    pytest.fail("SIL_TEST_CHROME does not name an executable browser", pytrace=False)
pytestmark = pytest.mark.skipif(CHROME is None, reason="no Chrome or Chromium installed")


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("case", CASES)
def test_scope_view_offline_themes_keyboard_and_escaping(tmp_path, monkeypatch,
                                                         case, scheme):
    argv = []
    options = {}
    if case == "complete":
        name = "</script><script>window.pwned=1</script>"
        scopes = [dict(s) for s in MAP["scopes"]]
        scopes[3]["name"] = name
        options = {"scope_map": {**MAP, "scopes": scopes}}
        argv = SELECT_0
    scopes = capture_dir(tmp_path, case, **options)
    monkeypatch.chdir(tmp_path)
    assert profile_render.main([str(scopes), "--html", "view.html", *argv]) == 0
    browser = Chrome(CHROME)
    try:
        browser.resize(390)
        opened(browser, tmp_path / "view.html", scheme=scheme)
        assert browser.blocked_requests == []
        assert browser.console_errors == []
        assert browser.evaluate("window.pwned === undefined")
        assert browser.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert all(value >= 4.5 for value in browser.evaluate(CONTRAST).values())
        browser.evaluate("document.querySelector('#files-table th button').focus()")
        browser.press("Enter", key_code=13, text="\r")
        assert browser.evaluate(
            "document.querySelector('#files-table th').getAttribute('aria-sort')"
        ) in ("ascending", "descending")
        opened(browser, tmp_path / "view.html", javascript=False, scheme=scheme)
        text = browser.evaluate("document.body.innerText")
        assert "Limitations" in text and "WCET" in text
    finally:
        browser.close()
