"""The combined portable workflow through public offline render commands."""

import shutil

import pytest

from profile_sets import artifact_set, correlation_document, fixture, samples_document
from test_diagnostics_html import Page
from test_profile_render import render


def selected_set(directory):
    report = fixture("activation-complete.json")
    samples = samples_document(report)
    samples["frames"] = [
        {"module": "m4", "symbol": "recursive_leaf", "address": "0x1",
         "module_offset": None, "source": {"file": "/absent/leaf.c", "line": 12}},
        {"module": "m4", "symbol": "entry", "address": "0x2",
         "module_offset": None, "source": {"file": "/absent/caller.c", "line": 99}}]
    samples["stacks"] = [[0, 1, 0, 1], [], [1]]
    # Controller index0 has precisely the first two samples. One has a
    # recursive leaf with a retained source; the other's stack is missing.
    for i, row in enumerate(samples["samples"]):
        row[4] = i if i < 2 else 2
    correlation = correlation_document(report, samples)
    return artifact_set(directory, "activation-complete.json", report=report,
                        samples=samples, correlation=correlation)


def test_selected_activation_reconciles_classes_callers_and_leaf_lines(tmp_path):
    profile = selected_set(tmp_path / "set")
    result = render([str(profile), "--participant", "controller", "--task", "tick",
                     "--activation-index", "0", "--html", "selected.html"], tmp_path)
    assert result.returncode == 0, result.stderr
    html = (tmp_path / "selected.html").read_text()
    text = Page(html).text
    assert "execution_context: 2 samples / 2002000 ns sampled weight" in text
    assert "window_associated: 0 samples / 0 ns sampled weight" in text
    assert "Selected function: recursive_leaf. 1 samples / 1001000 ns" in text
    assert "50.0% of the enclosing selection (2002000 ns)" in text
    assert "1 samples / 1001000 ns attributed; 1 samples / 1001000 ns unknown" in text
    assert "Denominator: 2 samples / 2002000 ns" in text
    source = html.split('id="source-lines"', 1)[1].split('</section>', 1)[0]
    assert "/absent/leaf.c:12" in source
    assert "/absent/caller.c:99" not in source


def test_selected_zero_sample_interval_is_zero_in_each_retained_view(tmp_path):
    profile = selected_set(tmp_path / "set")
    result = render([str(profile), "--participant", "controller", "--task", "tick",
                     "--activation-index", "3", "--html", "zero.html"], tmp_path)
    assert result.returncode == 0, result.stderr
    text = Page((tmp_path / "zero.html").read_text()).text
    assert "0 samples / 0 ns sampled weight" in text
    assert "0 samples / 0 ns attributed; 0 samples / 0 ns unknown" in text
    assert "Denominator: 0 samples / 0 ns" in text
    assert "0 samples / 0 ns" in text
    assert "not evidence" in text


@pytest.mark.parametrize("policy,expected", [
    ("legacy", "full (report predates the option)"),
    ("full", "full"), ("external", "external"), (None, "unknown")])
def test_copied_minimal_sets_keep_policy_and_missing_evidence_explicit(
        tmp_path, policy, expected):
    report = fixture("activation-partial-truncated.json")
    if policy != "legacy":
        report["configuration"]["provenance_mode"] = policy
    profile = artifact_set(tmp_path / "original", "activation-partial-truncated.json",
                           report=report)
    copy = tmp_path / "copied"
    copy.mkdir()
    for relative in ("profile.json", "cpu/samples.json", "activation/correlation.json"):
        target = copy / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(profile.parent / relative, target)
    shutil.rmtree(profile.parent)
    result = render([str(copy / "profile.json"), "--html", "portable.html"], tmp_path)
    assert result.returncode == 0, result.stderr
    text = Page((tmp_path / "portable.html").read_text()).text
    assert expected in text
    assert "Activation correlation partial" in text
    assert "Bottom-up callers" in text and "CPU source-line hotspots" in text
    assert "Unknown/missing/untrusted leaf source" in text
    assert not (copy / "run.diagnostics.json").exists()
    assert not (copy / "run.mcap").exists()
