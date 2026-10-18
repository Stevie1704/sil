"""The standalone HTML report of a saved diagnostics report (#304).

`sil.diagnostics_html.render` turns the text of a `sil_run_diagnostics` or a
`sil_benchmark_report` document into one HTML file. These tests read the
HTML with the standard library's parser, as a browser without JavaScript
would, so they need no browser: they pin what the static page says, that
report content never becomes markup, and that the page needs no other file.
"""

import base64
import json
import os
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from conftest import ROOT
from sil.benchmark import format_summary as benchmark_summary
from sil.diagnostics_html import render
from sil.diagnostics_report import ReportError
from sil.diagnostics_run import format_summary as run_summary
from test_run_diagnostics_cli import sil

FIXTURES = ROOT / "tests" / "fixtures" / "diagnostics"
ALL_FIXTURES = sorted(path.name for path in FIXTURES.glob("*.json"))


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()


class Page(HTMLParser):
    """The elements, attributes and visible text of a rendered page."""

    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict]] = []
        self.scripts: list[str] = []
        self.styles: list[str] = []
        self.text_parts: list[str] = []
        self._raw: str | None = None
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))
        if tag in ("script", "style"):
            self._raw = tag

    def handle_startendtag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_endtag(self, tag):
        if tag == self._raw:
            self._raw = None

    def handle_data(self, data):
        if self._raw == "script":
            self.scripts.append(data)
        elif self._raw == "style":
            self.styles.append(data)
        else:
            self.text_parts.append(data)

    @property
    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.text_parts))

    def find(self, tag: str, **attrs) -> list[dict]:
        return [a for t, a in self.elements if t == tag
                and all(a.get(k) == v for k, v in attrs.items())]


def page(name: str) -> Page:
    return Page(render(fixture_text(name)))


# --- the static page --------------------------------------------------------


def test_run_report_shows_status_identity_and_terminal_values():
    text = page("run-success.json").text

    assert "Succeeded" in text
    assert "exit code 0" in text
    assert "/work/mixed.json" in text
    assert "2f9ba41079fd01465e177ca9fd61daeebb01ff5d0424f8a5ae4c36494c70bdb0" \
        in text
    # The values and units of `sil run --diagnostics`'s summary.
    for value in ("67.235 ms", "98.97x", "0.30x", "118763.08/s", "9.000 MiB",
                  "14.000 MiB", "221.000 ms", "1.215 us", "113.750 us"):
        assert value in text, value
    assert "diagnostics" in text  # the measurement mode


VALUE = re.compile(r"-?\d+(?:\.\d+)? (?:ns|us|ms|s|B|KiB|MiB|GiB)\b"
                   r"|\d+\.\d\dx|\d+\.\d\d/s")


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_every_terminal_value_is_on_the_page(name):
    """The terminal summary and the page format the same values alike."""
    document = json.loads(fixture_text(name))
    if "sil_benchmark_report" in document:
        terminal = benchmark_summary(document, Path("summary.json"))
    else:
        terminal = run_summary(document, Path("run.json"))
    text = page(name).text

    values = set(VALUE.findall(terminal))
    assert sorted(v for v in values if v not in text) == []


def test_benchmark_shows_campaign_state_counts_and_warmups():
    text = page("benchmark-partial.json").text

    assert "Incomplete benchmark: run failed" in text
    assert "Exit code 1" in text
    assert "1 of 5 measured Runs succeeded (2 attempted, 1 failed)" in text
    assert "Warm-up (never aggregated)" in text
    assert "Failed: Run failure, exit code 1" in text
    assert "3 requested measured Runs were not attempted" in text
    assert "not uninstrumented timings" in text


def test_a_campaign_without_a_successful_run_has_no_statistic():
    text = page("benchmark-no-successful-run.json").text

    assert "Failed: ended by signal 9, no diagnostics report" in text
    assert "unavailable (no_successful_measured_runs)" in text
    assert "No measured Run succeeded" in text
    assert "unavailable (no_run_report)" in text


LONG = "a-participant-with-an-unusually-long-name-that-does-not-wrap-"


def many_participants(count: int = 40) -> str:
    """run-success with `count` more Process participants, long names."""
    report = json.loads(fixture_text("run-success.json"))
    template = report["execution"]["participants"][1]
    row = report["observational"]["tasks"][2]
    record = report["observational"]["resources"]["process_participants"][0]
    for index in range(count):
        name = f"{LONG}{index:03d}"
        report["execution"]["participants"].append(
            {**json.loads(json.dumps(template)), "participant": name})
        report["observational"]["tasks"].append(
            {**json.loads(json.dumps(row)), "participant": name})
        report["observational"]["resources"]["process_participants"].append(
            {**json.loads(json.dumps(record)), "participant": name})
    resources = report["observational"]["resources"]
    for key in ("user_cpu_ns", "system_cpu_ns"):
        resources["cpu_total"][key] = resources["runner"][key] + sum(
            r[key] for r in resources["process_participants"])
    return json.dumps(report, indent=1)


def one_sample() -> str:
    """run-success whose Process participant completed one Step."""
    report = json.loads(fixture_text("run-success.json"))
    task = report["execution"]["participants"][1]["tasks"][0]
    task.update(attempted=1, completed=1)
    timing = report["observational"]["tasks"][2]["timing"]
    timing.update(completed=1, samples_ns=[37958], slowest=timing["slowest"][1:],
                  min_ns=37958, max_ns=37958, mean_ns=37958.0, p50_ns=37958,
                  stddev_ns=0.0, sum_ns=37958)
    timing["quantiles"].update(retained_count=1, sample_count=1)
    return json.dumps(report, indent=1)


def test_many_participants_and_one_sample_render_every_row():
    text = page_of(many_participants()).text
    single = page_of(one_sample()).text

    assert text.count(f"{LONG}039") >= 4  # Task, slow and resource rows
    assert "all 1 of 1 completed activations retained" in single


def edited(name: str, edit) -> str:
    document = json.loads(fixture_text(name))
    edit(document)
    assert render(json.dumps(document))  # still a valid report
    return json.dumps(document)


def test_exit_0_without_a_report_is_shown_as_a_failed_run():
    def edit(summary):
        run = summary["runs"][0]
        run["process_outcome"] = {"exit_code": 0, "signal": None}
        run["run_exit_code"] = 0
        summary["status"]["exit_code"] = 1

    text = page_of(edited("benchmark-no-successful-run.json", edit)).text

    assert "Failed: exited 0 but wrote no diagnostics report" in text


def test_a_null_value_in_a_successful_run_names_both_reasons():
    def edit(summary):
        report = next(run["report"] for run in summary["runs"]
                      if run["label"] == "run-002")
        runner = report["observational"]["resources"]["runner"]
        runner["peak_rss_bytes"] = None
        runner["unavailable"] = {"peak_rss_bytes": "not_reported_by_os"}
        metric = next(m for m in summary["aggregates"]["metrics"]
                      if m["metric"] == "runner_peak_rss_bytes")
        for item in metric["per_run"]:
            if item["label"] == "run-002":
                item["value"] = None
        for key in ("min", "median", "max", "mean", "stddev"):
            metric[key] = None
        metric["unavailable"] = {
            key: "unavailable_in_a_measured_run"
            for key in ("min", "median", "max", "mean", "stddev")}

    text = page_of(edited("benchmark-complete.json", edit)).text

    assert "unavailable (unavailable_in_a_measured_run)" in text
    assert "unavailable (not_reported_by_os)" in text


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_every_frozen_example_renders_one_self_contained_file(name):
    html = render(fixture_text(name))
    rendered = Page(html)

    assert html.startswith("<!doctype html>")
    assert rendered.find("title")
    # No other file and no network: nothing refers outside the page.
    for _tag, attrs in rendered.elements:
        for key in ("src", "href", "action", "srcset", "poster", "data"):
            value = attrs.get(key)
            if value is not None:
                assert value.startswith(("#", "data:")), (key, value)
    assert not re.search(r"https?://", " ".join(rendered.styles
                                                + rendered.scripts))
    assert "@import" not in " ".join(rendered.styles)
    assert "url(" not in " ".join(rendered.styles)


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_the_download_is_the_source_json_byte_for_byte(name):
    source = fixture_text(name)
    links = [a for a in page(name).find("a") if "download" in a]

    assert len(links) == 1
    prefix = "data:application/json;base64,"
    assert links[0]["href"].startswith(prefix)
    assert base64.b64decode(links[0]["href"][len(prefix):]) == \
        source.encode("utf-8")


def test_invalid_input_raises_a_report_error():
    with pytest.raises(ReportError, match="not valid JSON"):
        render("{")
    with pytest.raises(ReportError, match="unsupported"):
        render(json.dumps({"sil_run_diagnostics": 2}))
    with pytest.raises(ReportError, match="unsupported"):
        render(json.dumps({"sil_benchmark_report": 99}))


# --- hostile and unusual content -----------------------------------------------

EVIL = "</script><script>window.pwned=1</script><img src=x onerror=alert(1)>"
QUOTES = "\"' onmouseover=\"alert(2)\" & <b>bold</b>"
UNICODE = "Bremse üß — \U0001F697 ‮"
HUGE_NS = 2**53 + 1  # a float would print 9007199254740992


def hostile(name: str) -> str:
    """A frozen example whose names, paths and messages are hostile."""
    text = fixture_text(name)
    for old, new in (("pyecho", EVIL), ("multi", QUOTES), ("test", UNICODE),
                     ("/work/mixed.json", "/wörk/</script>/m.json")):
        text = text.replace(old, json.dumps(new)[1:-1])
    document = json.loads(text)
    reports = ([run["report"] for run in document["runs"] if run["report"]]
               if "runs" in document else [document])
    for report in reports:
        report["observational"]["run_wall_time"]["wall_ns"] = HUGE_NS
    return json.dumps(document, indent=1)


def page_of(text: str) -> Page:
    return Page(render(text))


@pytest.mark.parametrize("name", ["run-success.json",
                                  "run-failed-partial.json",
                                  "benchmark-partial.json"])
def test_report_content_never_becomes_markup(name):
    rendered = page_of(hostile(name))

    # Only the page's own elements: one style, one script, no image.
    assert len(rendered.find("script")) == 1
    assert len(rendered.find("style")) == 1
    assert rendered.find("img") == [] and rendered.find("b") == []
    assert not any("onerror" in attrs or "onmouseover" in attrs
                   for _tag, attrs in rendered.elements)
    assert "pwned" not in " ".join(rendered.scripts)
    # The text reads as it was written.
    text = rendered.text
    original = fixture_text(name)
    for old, new in (("pyecho", EVIL), ("multi", QUOTES), ("test", UNICODE)):
        if f'"participant": "{old}"' in original:
            assert new in text, old


@pytest.mark.parametrize("name", ["run-success.json", "benchmark-partial.json"])
def test_large_nanosecond_integers_keep_every_digit(name):
    html = render(hostile(name))

    assert f'title="{HUGE_NS} ns"' in html
    assert f'data-sort="{HUGE_NS}"' in html
    assert "9007199254740992" not in html


def test_the_script_sees_no_report_data():
    html = render(hostile("benchmark-complete.json"))
    rendered = Page(html)
    (script,) = rendered.scripts

    assert EVIL not in script and "sil_benchmark_report" not in script
    assert "JSON.parse" not in script
    (policy,) = rendered.find("meta", **{
        "http-equiv": "Content-Security-Policy"})
    assert "default-src 'none'" in policy["content"]
    assert "unsafe-inline" not in policy["content"]


# --- sil diagnostics render ----------------------------------------------------


def render_command(tmp_path: Path, *argv: str):
    """`sil diagnostics render` with no native tool on PATH."""
    return sil(["diagnostics", "render", *argv], tmp_path / "no-tools",
               tmp_path)


@pytest.mark.parametrize("name", ["run-failed-partial.json",
                                  "benchmark-complete.json"])
def test_render_needs_only_the_copied_json(tmp_path, name):
    (tmp_path / "copy.json").write_text(fixture_text(name))

    proc = render_command(tmp_path, "copy.json", "--html", "report.html")

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "HTML report: report.html\n"
    assert proc.stderr == ""
    assert (tmp_path / "report.html").read_text() == render(fixture_text(name))
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "copy.json", "report.html"]


@pytest.mark.parametrize("content, message", [
    (None, "cannot read report"),
    ("{\"sil_run_diagnostics\": 1", "not valid JSON"),
    ("{\"sil_run_diagnostics\": 7}", "unsupported sil_run_diagnostics version"),
    ("{\"sil_benchmark_report\": 1}", "missing member"),
    ("[]", "not a JSON object"),
])
def test_invalid_input_exits_2_without_output(tmp_path, content, message):
    if content is not None:
        (tmp_path / "in.json").write_text(content)

    proc = render_command(tmp_path, "in.json", "--html", "out.html")

    assert proc.returncode == 2
    assert proc.stderr.startswith("sil diagnostics render: error: ")
    assert message in proc.stderr
    assert not (tmp_path / "out.html").exists()


def test_an_existing_output_is_never_replaced(tmp_path):
    (tmp_path / "in.json").write_text(fixture_text("run-success.json"))
    (tmp_path / "out.html").write_text("keep")

    proc = render_command(tmp_path, "in.json", "--html", "out.html")
    same = render_command(tmp_path, "in.json", "--html", "in.json")

    assert proc.returncode == 2 and "already exists" in proc.stderr
    assert (tmp_path / "out.html").read_text() == "keep"
    assert same.returncode == 2
    assert (tmp_path / "in.json").read_text() == fixture_text(
        "run-success.json")


def test_a_missing_output_directory_exits_2(tmp_path):
    (tmp_path / "in.json").write_text(fixture_text("run-success.json"))

    proc = render_command(tmp_path, "in.json", "--html", "no/out.html")

    assert proc.returncode == 2
    assert "does not exist" in proc.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_an_unwritable_output_exits_1(tmp_path):
    (tmp_path / "in.json").write_text(fixture_text("run-success.json"))
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o555)
    try:
        proc = render_command(tmp_path, "in.json", "--html",
                              "locked/out.html")
    finally:
        locked.chmod(0o755)

    assert proc.returncode == 1
    assert "cannot write the HTML report 'locked/out.html'" in proc.stderr
    assert list(locked.iterdir()) == []


@pytest.mark.parametrize("argv", [["in.json"], ["--html", "x.html"],
                                  ["in.json", "--html", "a", "--html", "b"]])
def test_usage_errors_exit_2(tmp_path, argv):
    proc = render_command(tmp_path, *argv)

    assert proc.returncode == 2
