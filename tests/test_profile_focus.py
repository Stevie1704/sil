"""Saved profile caller analysis through the offline render command."""

from __future__ import annotations

import json

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
