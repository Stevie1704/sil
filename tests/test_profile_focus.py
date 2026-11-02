"""Saved profile caller analysis through the offline render command."""

from __future__ import annotations

from profile_sets import artifact_set, fixture, samples_document
from test_profile_render import render
from test_diagnostics_html import Page


def recursive_cpu_set(directory):
    report = fixture("cpu-complete.json")
    samples = samples_document(report)
    samples["frames"] = [
        {"module": "m1", "symbol": symbol, "address": "0x1",
         "module_offset": None, "source": None}
        for symbol in ("entry", "recurse", "indirect", "other")]
    # Captured stacks are leaf first. The selected function occurs twice,
    # but every sample contributes to its denominator once.
    samples["stacks"] = [[1, 2, 1, 0], [1, 3, 0], []]
    for index, row in enumerate(samples["samples"]):
        row[4] = 0 if index < 6 else 1 if index < 9 else 2
    return artifact_set(directory, "cpu-complete.json", report=report,
                        samples=samples)


def test_cpu_bottom_up_keeps_recursion_without_multiplying_sample_weight(tmp_path):
    profile = recursive_cpu_set(tmp_path / "set")
    proc = render([str(profile), "--html", "report.html"], tmp_path)
    assert proc.returncode == 0, proc.stderr
    page = Page((tmp_path / "report.html").read_text())
    assert page.find("section", id="callers")
    assert "Selected function: recurse" in page.text
    assert "9 samples / 9009000 ns" in page.text
    assert "90.0% of the enclosing selection" in page.text
    assert "leafmost occurrence" in page.text
    assert "Recursive ancestors remain" in page.text
    assert "caller unavailable or truncated" in page.text
    assert page.find("select", id="caller-function")
    assert page.find("select", id="caller-mode")


def test_heap_callers_expose_each_metric_without_activation_filters(tmp_path):
    profile = artifact_set(tmp_path / "set", "heap-complete-runner.json")
    proc = render([str(profile), "--html", "report.html"], tmp_path)
    assert proc.returncode == 0, proc.stderr
    page = Page((tmp_path / "report.html").read_text())
    assert page.find("section", id="callers")
    assert "Heap has no Activation/thread/time attribution" in page.text
    options = [a.get("value") for t, a in page.elements if t == "option"]
    assert all(m in options for m in ("allocations", "allocated_bytes",
        "temporary_allocations", "outstanding_allocations", "outstanding_bytes"))
    assert not page.find("select", id="f-task")


def source_cpu_set(directory):
    report = fixture("cpu-complete.json")
    samples = samples_document(report)
    samples["frames"] = [
        {"module": module, "symbol": symbol, "address": "0x1",
         "module_offset": None, "source": source}
        for module, symbol, source in (
            ("m4", "inline_leaf", {"file": "/captured/shared.c", "line": 12}),
            ("m2", "inline_leaf", {"file": "/captured/shared.c", "line": 12}),
            ("m5", "mismatched", {"file": "/captured/untrusted.c", "line": 9}),
            ("m4", "caller", {"file": "/captured/ancestor.c", "line": 99}))]
    samples["stacks"] = [[0, 3], [1, 3], [2, 3], []]
    for index, row in enumerate(samples["samples"]):
        row[4] = 0 if index < 4 else 1 if index < 7 else 2 if index < 9 else 3
    return artifact_set(directory, "cpu-complete.json", report=report,
                        samples=samples)


def test_source_lines_partition_leaf_samples_by_captured_module_identity(tmp_path):
    profile = source_cpu_set(tmp_path / "set")
    proc = render([str(profile), "--html", "report.html"], tmp_path)
    assert proc.returncode == 0, proc.stderr
    html = (tmp_path / "report.html").read_text()
    page = Page(html)
    assert page.find("table", id="source-line-table")
    assert "7 samples / 7007000 ns attributed" in page.text
    assert "3 samples / 3003000 ns unknown" in page.text
    assert "70.0% of selected weight has a retained leaf source line" in page.text
    # Separate module rows even when path and line are identical.
    section = html.split('id="source-lines"', 1)[1].split('</section>', 1)[0]
    assert section.count("/captured/shared.c:12") == 2
    assert "/captured/ancestor.c:99" not in section
    assert "/captured/untrusted.c:9" not in section
    assert "m4" in section and "m2" in section


def test_missing_source_locations_remain_a_counted_partition(tmp_path):
    profile = artifact_set(tmp_path / "set", "cpu-complete.json")
    proc = render([str(profile), "--html", "report.html"], tmp_path)
    assert proc.returncode == 0, proc.stderr
    page = Page((tmp_path / "report.html").read_text())
    assert "0 samples / 0 ns attributed; 10 samples / 10010000 ns unknown" in page.text
    assert "Denominator: 10 samples / 10010000 ns" in page.text


def test_a_null_source_line_is_invalid_portable_evidence(tmp_path):
    report = fixture("cpu-complete.json")
    samples = samples_document(report)
    samples["frames"][1]["source"] = {"file": "missing.c", "line": None}
    profile = artifact_set(tmp_path / "set", "cpu-complete.json", samples=samples)
    proc = render([str(profile), "--html", "report.html"], tmp_path)
    assert proc.returncode == 2
    assert not (tmp_path / "report.html").exists()
