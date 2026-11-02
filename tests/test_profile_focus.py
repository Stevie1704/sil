"""Saved profile caller analysis through the offline render command."""

from __future__ import annotations

from sil.profile_analysis import cpu_ranking, cpu_rows, function_paths
from sil.profile_focus import caller_tree, focus_data
from sil import profile_source_lines
from sil.profile_set import load_profile_set

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


def caller_totals(profile, metric):
    data = focus_data(load_profile_set(profile))
    return {tuple(data["functions"][key]): caller_tree(data, key, metric).weight
            for key in range(len(data["functions"]))}


def test_cpu_callers_reconcile_with_the_inclusive_ranking(tmp_path):
    profile = load_profile_set(recursive_cpu_set(tmp_path / "set"))
    functions, paths = function_paths(profile)
    ranking = cpu_ranking(paths, cpu_rows(profile))
    expected = {(symbol, module): ranking.inclusive.get(key, (0, 0))[1]
                for key, (module, symbol) in enumerate(functions)}
    totals = caller_totals(recursive_cpu_set(tmp_path / "again"), "sampled_weight_ns")
    assert {k: v for k, v in totals.items() if k[0] is not None} == \
        {k: v for k, v in expected.items() if k[0] is not None}
    assert {k[0]: v for k, v in totals.items()}["recurse"] == 9009000


def test_heap_callers_reconcile_with_independent_allocation_sums(tmp_path):
    profile = artifact_set(tmp_path / "set", "heap-complete-runner.json")
    document = load_profile_set(profile).allocations
    for metric, value in (("allocations", lambda size, count: count),
                          ("allocated_bytes", lambda size, count: size * count)):
        expected: dict = {}
        for stack, size, count, _temporary, _outstanding in document["allocations"]:
            for frame in {document["frames"][f]["symbol"] for f in document["stacks"][stack]}:
                expected[frame] = expected.get(frame, 0) + value(size, count)
        expected.pop(None, None)  # unknown frames share a key with empty stacks
        totals = caller_totals(profile, metric)
        assert {k[0]: v for k, v in totals.items() if k[0] in expected} == expected


def tied_lines_set(directory):
    """Ten leaf lines with equal weight, captured in descending line order."""
    report = fixture("cpu-complete.json")
    samples = samples_document(report)
    samples["frames"] = [
        {"module": "m4", "symbol": f"leaf{index}", "address": "0x1",
         "module_offset": None, "source": {"file": "/captured/many.c", "line": 10 - index}}
        for index in range(10)]
    samples["stacks"] = [[index] for index in range(10)]
    for index, row in enumerate(samples["samples"]):
        row[4] = index
    return artifact_set(directory, "cpu-complete.json", report=report, samples=samples)


def test_source_lines_bound_rows_with_an_omitted_remainder(tmp_path, monkeypatch):
    profile = tied_lines_set(tmp_path / "set")
    monkeypatch.setattr(profile_source_lines, "LINE_ROWS", 4)
    text = Page(str(profile_source_lines.source_lines_section(load_profile_set(profile)))).text
    assert "Other known lines (6 omitted) 6 6006000 ns 60.0%" in text
    assert "/captured/many.c:4" in text and "/captured/many.c:5" not in text


def test_source_lines_without_samples_have_no_invented_share(tmp_path):
    profile = artifact_set(tmp_path / "set", "cpu-zero-samples.json")
    proc = render([str(profile), "--html", "report.html"], tmp_path)
    assert proc.returncode == 0, proc.stderr
    section = (tmp_path / "report.html").read_text().split('id="source-lines"', 1)[1]
    text = Page(section.split("</section>", 1)[0]).text
    assert "0 samples / 0 ns attributed; 0 samples / 0 ns unknown" in text
    assert "Denominator: 0 samples / 0 ns" in text
