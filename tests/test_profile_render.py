"""`sil profile render`: the offline report of a saved profile (#318).

The command reads `profile.json` and the normalized files it names, prints
the terminal summary and writes one standalone HTML file. These tests run
the command as a user does, on portable artifact sets (tests/profile_sets.py),
and read the HTML with the standard library's parser, as a browser without
JavaScript would. The browser checks are test_profile_html_browser.py.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

from profile_sets import artifact_set, fixture, samples_document
from test_diagnostics_html import Page


def render(argv: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "sil.cli", "profile", "render", *argv],
        capture_output=True, text=True, cwd=cwd,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": ":".join(sys.path)})


def html_of(tmp_path: Path, name: str, **options) -> str:
    profile = artifact_set(tmp_path / "set", name, **options)
    proc = render([str(profile), "--html", "report.html"], tmp_path)
    assert proc.returncode == 0, proc.stderr
    return (tmp_path / "report.html").read_text()


def test_render_prints_the_summary_and_writes_the_html(tmp_path):
    profile = artifact_set(tmp_path / "set", "cpu-complete.json")

    proc = render([str(profile), "--html", "report.html"], tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert "run: succeeded, exit code 0" in proc.stdout
    assert "capture: cpu, complete" in proc.stdout
    assert "HTML report: report.html" in proc.stdout
    page = Page((tmp_path / "report.html").read_text())
    assert "Run succeeded" in page.text
    assert "Capture complete" in page.text


def test_the_download_is_profile_json_byte_for_byte(tmp_path):
    profile = artifact_set(tmp_path / "set", "cpu-complete.json")
    # CRLF line ends are valid JSON whitespace; a text read would drop them.
    data = profile.read_bytes().replace(b"\n", b"\r\n")
    profile.write_bytes(data)

    proc = render([str(profile), "--html", "report.html"], tmp_path)

    assert proc.returncode == 0, proc.stderr
    html = (tmp_path / "report.html").read_text()
    prefix = 'href="data:application/json;base64,'
    start = html.index(prefix) + len(prefix)
    encoded = html[start:html.index('"', start)]
    assert base64.b64decode(encoded) == data


VALID = sorted(p.name for p in
               (Path(__file__).parent / "fixtures" / "profile").glob("*.json"))
RUN_LABELS = {"succeeded": "Run succeeded", "run_failure": "Run failed",
              "interrupted": "Run interrupted",
              "ended_by_signal": "Runner ended by a signal",
              "not_started": "Run not started"}
CAPTURE_LABELS = {"complete": "Capture complete", "partial": "Capture partial",
                  "interrupted": "Capture interrupted",
                  "failed": "Capture failed",
                  "not_started": "Capture not started",
                  "unavailable": "Capture unavailable"}


@pytest.mark.parametrize("name", VALID)
def test_every_fixture_states_its_independent_statuses_first(tmp_path, name):
    report = fixture(name)
    html = html_of(tmp_path, name)
    page = Page(html)

    banner = html[:html.index('id="measured"')]
    assert RUN_LABELS[report["run"]["status"]] in banner
    assert CAPTURE_LABELS[report["capture"]["status"]] in banner
    for reason in report["capture"]["reasons"]:
        assert f"<code>{reason}</code>" in banner
    # Method and coverage come before any result.
    text = page.text
    assert text.index("What was measured") < text.index("Limitations")
    if report["capture"]["kind"] == "cpu":
        assert text.index("Coverage and loss") < text.index("Hotspots")


def test_a_capture_without_samples_says_it_is_not_zero_cpu(tmp_path):
    text = Page(html_of(tmp_path, "cpu-zero-samples.json")).text

    assert "No sample: this is not evidence that no CPU time was used" in text
    assert "That is not evidence of no use" in text  # the empty tree


def test_lost_samples_and_unknown_functions_stay_visible(tmp_path):
    lost = Page(html_of(tmp_path / "a", "cpu-partial-lost.json")).text
    unknown = Page(html_of(tmp_path / "b", "cpu-missing-symbols.json")).text

    assert "Capture partial" in lost and "lost_samples" in lost
    assert "perf reports lost samples" in lost
    assert "[unknown function]" in unknown


def test_hotspot_tables_keep_the_remainder_and_the_total(tmp_path):
    report = fixture("cpu-complete.json")
    report["capture"]["hotspots"]["self"] = \
        report["capture"]["hotspots"]["self"][:1]
    kept = report["capture"]["hotspots"]["self"][0]
    totals = report["capture"]["totals"]
    report["capture"]["hotspots"]["self_remainder"] = {
        "samples": totals["samples"] - kept["self_samples"],
        "weight_ns": totals["weight_ns"] - kept["self_weight_ns"]}
    html = html_of(tmp_path, "cpu-complete.json", report=report)
    footer = html[html.index('id="self-table"'):]
    footer = footer[footer.index("<tfoot>"):footer.index("</tfoot>")]

    rest = Page(footer).text
    assert "Other functions (not listed) " \
        f"{totals['samples'] - kept['self_samples']}" in rest
    assert f"All samples {totals['samples']}" in rest
    assert "100.0%" in rest


@pytest.mark.parametrize("name, table_id, document", [
    ("cpu-complete.json", "inclusive-table", "samples"),
    ("heap-complete-runner.json", "heap-inclusive-table", "allocations")])
def test_an_inclusive_table_names_its_unlisted_rows_without_a_sum(
        tmp_path, name, table_id, document):
    report = fixture(name)
    capture = report["capture"]
    hotspots = (capture["analysis"] if "analysis" in capture
                else capture)["hotspots"]
    hotspots["inclusive"] = hotspots["inclusive"][:1]
    html = html_of(tmp_path, name, report=report)
    sidecar = json.loads(next((tmp_path / "set").rglob(
        "samples.json" if document == "samples" else "allocations.json"))
        .read_text())
    functions = {(f["module"], f["symbol"]) for f in sidecar["frames"]}

    after = Page(html[html.index(f'id="{table_id}"'):]).text

    assert f"{len(functions) - 1} other functions are not listed" in after
    assert "no remainder row" in after
    assert "<tfoot>" not in html[html.index(f'id="{table_id}"'):].split(
        "</table>")[0]


def test_a_heap_profile_shows_bounded_rankings_and_their_boundaries(tmp_path):
    text = Page(html_of(tmp_path, "heap-complete-runner.json")).text

    for heading in ("Allocation sites by bytes allocated",
                    "Churn: sites by temporary allocations",
                    "Retention: sites by bytes outstanding at capture end",
                    "Sites by inclusive bytes", "Allocation caller tree"):
        assert heading in text
    assert "not a leak verdict" in text
    assert "not RSS" in text
    assert "Activation correlation unavailable" in text
    assert "no_validated_clock_mapping" in text
    assert "Other sites (not listed)" in text
    assert "All allocations 8 " in text
    assert "retain_site" in text


def test_a_heap_profile_without_analysis_shows_no_empty_result(tmp_path):
    text = Page(html_of(tmp_path, "heap-failed-no-data.json")).text

    assert "No allocation analysis" in text
    assert "no_data" in text
    assert "Allocation sites by bytes" not in text


def test_an_unavailable_correlation_offers_no_activation_view(tmp_path):
    html = html_of(tmp_path, "activation-reserved.json")
    page = Page(html)

    assert "Activation correlation unavailable" in page.text
    assert "No sample is related to an Activation" in page.text
    assert not page.find("select", id="f-task")
    assert not page.find("input", id="f-vt-from")
    assert not page.find("table", id="activation-table")
    assert not page.find("table", id="tasks-table")


def test_a_partial_correlation_names_its_gap(tmp_path):
    page = Page(html_of(tmp_path, "activation-partial-truncated.json"))

    assert "The correlation is partial" in page.text
    assert "observations_truncated" in page.text
    assert page.find("select", id="f-task")


def test_activations_without_samples_stay_listed(tmp_path):
    html = html_of(tmp_path, "activation-complete.json")
    page = Page(html)

    rows = [a for t, a in page.elements if t == "tr"
            and a.get("id", "").startswith("activation-")]
    assert len(rows) == 8
    assert sum(1 for a in rows if a.get("class") == "no-samples") == 2
    assert "without samples is not evidence that it used no CPU time" in \
        page.text
    assert "associated with the Step window only" in page.text


def test_flame_timeline_and_task_views_share_the_saved_evidence(tmp_path):
    html = html_of(tmp_path, "activation-complete.json")
    page = Page(html)

    assert page.find("svg", id="cpu-flame-svg")
    assert "Flame graph" in page.text and "10010000 ns" in page.text
    assert page.find("section", id="timeline")
    assert len(page.find("g", **{"data-interval-kind": "activation"})) == 8
    assert page.find("section", id="task-drilldowns")
    assert page.find("table", id="task-0-execution_context-self")
    assert page.find("table", id="task-0-window_associated-self")
    assert "No sample is not evidence of no CPU use" in page.text
    assert "Host CLOCK_MONOTONIC" in page.text


def test_timeline_replay_lifecycle_and_incomplete_sample_extent(tmp_path):
    from profile_sets import correlation_document
    report = fixture("activation-partial-truncated.json")
    samples = samples_document(report)
    correlation = correlation_document(report, samples)
    # An already unattributed sample is now retained as a Replay Slot.
    index = next(i for i, r in enumerate(correlation["samples"]) if r[0] == "unattributed")
    value = samples["samples"][index][3]
    summary = report["activation_correlation"]["summary"]
    counts = summary["samples"]
    reason = next(r for r in counts["unattributed"]["reasons"] if r["samples"])
    reason["samples"] -= 1
    reason["weight_ns"] -= value
    counts["unattributed"]["samples"] -= 1
    counts["unattributed"]["weight_ns"] -= value
    counts["replay"] = {"samples": 1, "weight_ns": value}
    summary["intervals"]["replay"] = 1
    correlation["samples"][index] = ["replay", 0]
    origin = int(samples["time_origin_ns"])
    correlation["replays"] = [{"participant": "replayer", "index": 0,
                               "virtual_time_ns": 2**53 + 1, "begin_ns": str(origin),
                               "end_ns": str(origin+100), "outcome": "completed",
                               "samples": 1, "weight_ns": value}]
    html = html_of(tmp_path, "activation-partial-truncated.json", report=report,
                   samples=samples, correlation=correlation)
    page = Page(html)
    assert page.find("g", **{"data-interval-kind": "replay"})
    assert page.find("g", **{"data-interval-kind": "lifecycle"})
    assert "9007199254740993" in page.text
    assert "exact gap boundaries are not retained" in page.text


@pytest.mark.parametrize("damage", ["negative-interval", "phase-shape", "unknown-task"])
def test_invalid_timeline_evidence_is_exit_two(tmp_path, damage):
    from profile_sets import correlation_document
    report = fixture("activation-complete.json")
    samples = samples_document(report)
    correlation = correlation_document(report, samples)
    if damage == "negative-interval":
        correlation["activations"][0]["end_ns"] = "0"
    elif damage == "phase-shape":
        correlation["phases"] = [42]
    else:
        correlation["activations"][0]["task"] = "absent"
    profile = artifact_set(tmp_path / "set", "activation-complete.json", report=report,
                           samples=samples, correlation=correlation)
    proc = render([str(profile), "--html", "report.html"], tmp_path)
    assert proc.returncode == 2
    assert not (tmp_path / "report.html").exists()


def test_many_timeline_intervals_have_a_visible_remainder(tmp_path):
    from profile_sets import correlation_document
    report = fixture("activation-complete.json")
    samples = samples_document(report)
    correlation = correlation_document(report, samples)
    first = correlation["activations"][0]
    for index in range(501):
        correlation["activations"].append({**first, "index": index + 100,
                                           "samples": 0, "weight_ns": 0})
    summary = report["activation_correlation"]["summary"]
    key = "activations" if first["kind"] == "native_task" else "process_steps"
    summary["intervals"][key] += 501
    task = next(t for t in summary["tasks"] if t["participant"] == first["participant"]
                and t["task"] == first["task"])
    for key in ("activations", "completed", "without_samples"):
        task[key] += 501
    html = html_of(tmp_path, "activation-complete.json", report=report,
                   samples=samples, correlation=correlation)
    page = Page(html)
    assert len(page.find("g", **{"data-interval-kind": "activation"})) == 500
    assert "13 other intervals not drawn" in page.text  # includes four lifecycle phases
    assert "10 samples / 10010000 ns in the capture" in page.text


def test_without_html_it_prints_the_summary_only(tmp_path):
    profile = artifact_set(tmp_path / "set", "heap-complete-runner.json")

    proc = render([str(profile)], tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert "capture: heap, complete" in proc.stdout
    assert "HTML report" not in proc.stdout
    assert list(tmp_path.glob("*.html")) == []


@pytest.mark.parametrize("name", VALID)
def test_the_summary_reads_every_fixture(tmp_path, name):
    profile = artifact_set(tmp_path / "set", name)

    proc = render([str(profile)], tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith(f"profile: {profile}\n")


@pytest.mark.parametrize("name", ["cpu-complete.json",
                                  "heap-complete-runner.json"])
def test_the_summary_states_identity_and_limits_before_hotspots(tmp_path,
                                                                name):
    profile = artifact_set(tmp_path / "set", name)
    report = fixture(name)

    lines = render([str(profile)], tmp_path).stdout.splitlines()

    identity = report["identity"]
    manifest = next(i for i, line in enumerate(lines)
                    if line.startswith("manifest: "))
    assert lines[manifest] == (f"manifest: {identity['manifest_path']} "
                               f"({identity['manifest_hash']})")
    assert f"sil: {identity['sil_version']}, " \
           f"{identity['source_revision']}" in lines
    limits = next(i for i, line in enumerate(lines)
                  if line.startswith("limitations: "))
    assert lines[limits] == ("limitations: "
                             + ", ".join(report["limitations"]))
    top = next(i for i, line in enumerate(lines) if line.startswith("top "))
    assert manifest < limits < top


def test_the_cpu_summary_names_the_recorded_call_graph(tmp_path):
    report = fixture("cpu-complete.json")
    report["capture"]["call_graph"]["method"] = "fp"
    profile = artifact_set(tmp_path / "set", "cpu-complete.json",
                           report=report)

    text = render([str(profile)], tmp_path).stdout

    assert "frame-pointer call graph" in text
    assert "DWARF" not in text


def test_the_heap_summary_keeps_the_remainder_of_its_top_rows(tmp_path):
    report = fixture("heap-complete-runner.json")
    hotspots = report["capture"]["analysis"]["hotspots"]
    first, *rest = hotspots["self"]
    hotspots["self"] = [first]
    hotspots["self_remainder"] = {
        metric: hotspots["self_remainder"][metric]
        + sum(row[metric] for row in rest)
        for metric in hotspots["self_remainder"]}
    profile = artifact_set(tmp_path / "set", "heap-complete-runner.json",
                           report=report)
    remainder = hotspots["self_remainder"]

    text = render([str(profile)], tmp_path).stdout

    assert f"{remainder['allocations']:>8} calls  all other sites" in text


def test_the_terminal_summary_never_prints_control_characters(tmp_path):
    report = fixture("cpu-complete.json")
    capture = report["capture"]
    capture["hotspots"]["self"][0]["symbol"] = "\x1b[2J\x1b]0;owned\x07spin"
    capture["modules"][0]["path"] = "/opt/\x1b[31mred\nlib.so"
    report["identity"]["manifest_path"] = "m\x1b[1m.json"
    profile = artifact_set(tmp_path / "set", "cpu-complete.json",
                           report=report)

    text = render([str(profile)], tmp_path).stdout

    assert "\x1b" not in text and "\x07" not in text
    assert "\\x1b[2J\\x1b]0;owned\\x07spin" in text
    assert "m\\x1b[1m.json" in text


# --- input and output errors -------------------------------------------------


def test_an_unsupported_version_is_a_usage_error(tmp_path):
    report = fixture("cpu-complete.json")
    report["sil_profile_report"] = 2
    profile = artifact_set(tmp_path / "set", "cpu-complete.json",
                           report=report)

    proc = render([str(profile), "--html", "report.html"], tmp_path)

    assert proc.returncode == 2
    assert "unsupported sil_profile_report version 2" in proc.stderr
    assert not (tmp_path / "report.html").exists()


@pytest.mark.parametrize("damage, message", [
    (lambda d: (d / "cpu" / "samples.json").unlink(),
     "names 'cpu/samples.json', which cannot be read"),
    (lambda d: (d / "cpu" / "samples.json").write_text("{}\n"),
     "does not have the size and SHA-256"),
    (lambda d: (d / "profile.json").write_text("{\"sil_profile_report\": 1"),
     "not valid JSON"),
], ids=["missing", "foreign", "corrupt"])
def test_a_missing_or_foreign_file_is_a_usage_error(tmp_path, damage,
                                                    message):
    profile = artifact_set(tmp_path / "set", "cpu-complete.json")
    damage(profile.parent)

    proc = render([str(profile), "--html", "report.html"], tmp_path)

    assert proc.returncode == 2
    assert message in proc.stderr
    assert not (tmp_path / "report.html").exists()


@pytest.mark.parametrize("name", ["profile.json", "cpu/samples.json"])
def test_a_file_above_the_render_limit_is_refused_unread(tmp_path, name):
    # docs/profile-report.md, "Offline report": 256 MiB for each file of the
    # set. A sparse file has that size without the disk use.
    profile = artifact_set(tmp_path / "set", "cpu-complete.json")
    with (profile.parent / name).open("r+b") as large:
        large.truncate(256 * 1024 * 1024 + 1)

    proc = render([str(profile), "--html", "report.html"], tmp_path)

    assert proc.returncode == 2
    assert "larger than the render limit of 268435456 bytes" in proc.stderr
    assert not (tmp_path / "report.html").exists()


def test_a_sidecar_of_another_version_is_a_usage_error(tmp_path):
    profile = artifact_set(tmp_path / "set", "cpu-complete.json", samples={
        "sil_profile_cpu_samples": 2})

    proc = render([str(profile)], tmp_path)

    assert proc.returncode == 2
    assert "unsupported sil_profile_cpu_samples version 2" in proc.stderr


def test_samples_that_do_not_add_up_are_a_usage_error(tmp_path):
    samples = samples_document(fixture("cpu-complete.json"))
    samples["samples"].pop()
    profile = artifact_set(tmp_path / "set", "cpu-complete.json",
                           samples=samples)

    proc = render([str(profile)], tmp_path)

    assert proc.returncode == 2
    assert "profile.json has 10 of 10010000 ns" in proc.stderr


def test_an_existing_output_is_a_usage_error(tmp_path):
    profile = artifact_set(tmp_path / "set", "cpu-complete.json")
    (tmp_path / "report.html").write_text("keep")

    proc = render([str(profile), "--html", "report.html"], tmp_path)

    assert proc.returncode == 2
    assert "already exists" in proc.stderr
    assert proc.stdout == ""
    assert (tmp_path / "report.html").read_text() == "keep"


def test_the_input_itself_is_never_the_output(tmp_path):
    profile = artifact_set(tmp_path / "set", "cpu-complete.json")
    before = profile.read_bytes()

    proc = render([str(profile), "--html", str(profile)], tmp_path)

    assert proc.returncode == 2
    assert profile.read_bytes() == before


def test_an_output_without_a_directory_is_a_usage_error(tmp_path):
    profile = artifact_set(tmp_path / "set", "cpu-complete.json")

    proc = render([str(profile), "--html", "missing/report.html"], tmp_path)

    assert proc.returncode == 2
    assert proc.stdout == ""


# --- hostile content, embedded data and precision ---------------------------

EVIL = ("</script><script>window.pwned=1</script><!--<img src=x "
        "onerror=alert(1)>  ]]>")


def hostile_set(tmp_path: Path) -> Path:
    """The correlated CPU fixture with hostile names, paths and messages."""
    report = fixture("activation-complete.json")
    capture = report["capture"]
    for row in capture["hotspots"]["self"] + capture["hotspots"]["inclusive"]:
        row["symbol"] = EVIL
        for line in row["source_lines"]:
            line["file"] = "javascript:alert(1)//" + EVIL
    capture["modules"][0]["path"] = "/opt/" + EVIL
    capture["processes"][1]["commands"].append(EVIL)
    report["identity"]["manifest_path"] = EVIL
    report["run"]["status"] = "run_failure"
    report["run"]["exit_code"] = 1
    report["run"]["failure"] = {"message": EVIL, "category": EVIL}
    for task in report["activation_correlation"]["summary"]["tasks"]:
        task["participant"] = EVIL
    samples = samples_document(report)
    for frame in samples["frames"]:
        frame["symbol"] = EVIL
    return artifact_set(tmp_path / "set", "activation-complete.json",
                        report=report, samples=samples)


def test_hostile_text_never_becomes_markup(tmp_path):
    profile = hostile_set(tmp_path)
    proc = render([str(profile), "--html", "report.html"], tmp_path)
    assert proc.returncode == 0, proc.stderr
    html = (tmp_path / "report.html").read_text()
    page = Page(html)

    # Exactly the page's own script and its one inert data block.
    scripts = [a for t, a in page.elements if t == "script"]
    assert scripts == [{"type": "application/json", "id": "profile-data"}, {}]
    assert not page.find("img")
    assert EVIL.replace(" ", " ").replace(" ", " ").split()[0] \
        in page.text
    assert "<!--" not in html
    # No link from a report value: only file: links built from a path.
    hrefs = [a["href"] for t, a in page.elements if t == "a" and "href" in a]
    assert all(h.startswith(("#", "data:application/json;base64,",
                             "file:///")) for h in hrefs), hrefs


def test_the_data_block_is_inert_and_decodes_to_the_values(tmp_path):
    profile = hostile_set(tmp_path)
    render([str(profile), "--html", "report.html"], tmp_path)
    html = (tmp_path / "report.html").read_text()

    start = html.index('<script type="application/json" id="profile-data">')
    body = html[start:].split(">", 1)[1].split("</script>", 1)[0]
    for raw in ("<", ">", "&", " ", " "):
        assert raw not in body
    data = json.loads(body)
    assert EVIL in [function[0] for function in data["functions"]]
    assert EVIL in {task[0] for task in data["tasks"]}


def test_virtual_times_above_2_53_stay_exact(tmp_path):
    report = fixture("activation-complete.json")
    samples = samples_document(report)
    from profile_sets import correlation_document
    correlation = correlation_document(report, samples)
    correlation["activations"][1]["virtual_time_ns"] = 2**63 - 1
    correlation["activations"][2]["virtual_time_ns"] = 2**53 + 1
    profile = artifact_set(tmp_path / "set", "activation-complete.json",
                           samples=samples, correlation=correlation)
    render([str(profile), "--html", "report.html"], tmp_path)
    html = (tmp_path / "report.html").read_text()

    data = html.split('id="profile-data">', 1)[1].split("</script>", 1)[0]
    activations = json.loads(data)["activations"]
    assert activations[1][1] == "9223372036854775807"
    assert activations[2][1] == "9007199254740993"
    assert "<code>9007199254740993</code>" in html  # the static table


def test_no_stack_bytes_or_source_text_is_embedded(tmp_path):
    profile = artifact_set(tmp_path / "set", "cpu-complete.json")
    secret = "SECRET-STACK-BYTES-4f1c"
    (profile.parent / "cpu" / "perf.data").write_bytes(
        b"PERFILE2" + secret.encode())
    source = tmp_path / "silproof_native.c"
    source.write_text(f"int spin(void) {{ return 0; }} /* SOURCE-{secret} */")
    report = json.loads(profile.read_text())
    for row in report["capture"]["hotspots"]["self"]:
        for line in row["source_lines"]:
            line["file"] = str(source)
    profile.write_text(json.dumps(report))

    proc = render([str(profile), "--html", "report.html"], tmp_path)

    assert proc.returncode == 0, proc.stderr
    html = (tmp_path / "report.html").read_text()
    assert secret not in html
    assert f'href="{source.as_uri()}"' in html


def test_a_relative_source_path_is_text_not_a_link(tmp_path):
    report = fixture("cpu-complete.json")
    report["capture"]["hotspots"]["self"][0]["source_lines"][0]["file"] = \
        "javascript:alert(1)"
    html = html_of(tmp_path, "cpu-complete.json", report=report)

    assert "<code>javascript:alert(1):40</code>" in html
    assert 'href="javascript' not in html
