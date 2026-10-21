"""Compare portable artifact sets through the installed CLI boundary."""

import json
import subprocess
import sys

import pytest

from profile_sets import artifact_set, fixture, samples_document
from test_diagnostics_html import Page


def diff(tmp_path, base, new, *args):
    return subprocess.run(
        [sys.executable, "-m", "sil.cli", "profile", "diff", str(base),
         str(new), *args], cwd=tmp_path, capture_output=True, text=True)


def test_cpu_diff_compares_shares_with_both_denominators(tmp_path):
    report = fixture("cpu-complete.json")
    samples = samples_document(report)
    base = artifact_set(tmp_path / "base", "cpu-complete.json")
    # Duplicate all samples: twice the weight, exactly the same shares.
    samples["samples"] *= 2
    def scale(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, int) and ("samples" in key or "weight_ns" in key):
                    value[key] *= 2
                else:
                    scale(item)
        elif isinstance(value, list):
            for item in value:
                scale(item)
    scale(report["capture"])
    new = artifact_set(tmp_path / "new", "cpu-complete.json", report=report,
                       samples=samples)

    proc = diff(tmp_path, base, new, "--html", "diff.html")

    assert proc.returncode == 0, proc.stderr
    assert "CPU profile diff" in proc.stdout
    assert "0.00 pp" in proc.stdout
    page = Page((tmp_path / "diff.html").read_text())
    assert "10010000 ns" in page.text and "20020000 ns" in page.text
    assert "sampling noise" in page.text
    assert "Self share" in page.text and "Inclusive share" in page.text
    assert "Manifest hash" in page.text and "Sampling settings" in page.text


def test_diff_warns_about_identities_and_functions_appearing_or_disappearing(tmp_path):
    base = artifact_set(tmp_path / "base", "cpu-complete.json")
    report = fixture("cpu-complete.json")
    report["host"]["architecture"] = "aarch64"
    report["identity"]["manifest_hash"] = "sha256:" + "2" * 64
    report["capture"]["sampling"]["frequency_hz"] = 100
    samples = samples_document(report)
    samples["frames"][1]["symbol"] = "new_function</script><script>window.pwned=1</script>"
    new = artifact_set(tmp_path / "new", "cpu-complete.json", report=report, samples=samples)

    proc = diff(tmp_path, base, new, "--html", "diff.html")

    assert proc.returncode == 0, proc.stderr
    assert "WARNING: identities differ: Manifest hash, Host, Sampling settings" in proc.stdout
    assert "-30.00 pp" in proc.stdout and "+30.00 pp" in proc.stdout
    html = (tmp_path / "diff.html").read_text()
    assert "<script>window.pwned" not in html


def test_heap_diff_compares_all_site_metrics_with_totals(tmp_path):
    base = artifact_set(tmp_path / "base", "heap-complete-runner.json")
    new = artifact_set(tmp_path / "new", "heap-complete-runner.json")

    proc = diff(tmp_path, base, new, "--html", "diff.html")

    assert proc.returncode == 0, proc.stderr
    assert "Identities match" in proc.stdout
    text = Page((tmp_path / "diff.html").read_text()).text
    assert "Allocated bytes change" in text
    assert "Temporary allocations" in text and "Outstanding allocations" in text
    assert "Other sites (not listed)" in text and "All sites" in text


@pytest.mark.parametrize("new_name", ["heap-complete-runner.json", "cpu-not-started.json"])
def test_diff_rejects_mismatched_or_missing_evidence(tmp_path, new_name):
    base = artifact_set(tmp_path / "base", "cpu-complete.json")
    new = artifact_set(tmp_path / "new", new_name)

    proc = diff(tmp_path, base, new)

    assert proc.returncode == 2


def test_diff_rejects_unknown_versions_missing_inputs_and_collisions(tmp_path):
    base = artifact_set(tmp_path / "base", "cpu-complete.json")
    new = artifact_set(tmp_path / "new", "cpu-complete.json")
    report = json.loads(new.read_text())
    report["sil_profile_report"] = 999
    new.write_text(json.dumps(report))
    assert diff(tmp_path, base, new).returncode == 2
    assert diff(tmp_path, base, tmp_path / "missing").returncode == 2
    assert diff(tmp_path, base, base, "--html", str(base)).returncode == 2
    assert diff(tmp_path, base, base, "--html", "missing/out.html").returncode == 2


@pytest.mark.parametrize("name, available", [("activation-complete.json", True),
                                           ("activation-partial-truncated.json", False)])
def test_task_diff_requires_complete_correlations_and_separates_classes(tmp_path, name, available):
    base = artifact_set(tmp_path / "base", name)
    new = artifact_set(tmp_path / "new", name)

    proc = diff(tmp_path, base, new, "--html", "diff.html")

    assert proc.returncode == 0, proc.stderr
    html = (tmp_path / "diff.html").read_text()
    assert ('id="diff-task-0-execution_context"' in html) == available
    assert ('id="diff-task-0-window_associated"' in html) == available
    assert ("both sets need complete" in proc.stdout) != available


def test_zero_weight_diff_does_not_invent_shares_or_changes(tmp_path):
    base = artifact_set(tmp_path / "base", "cpu-zero-samples.json")
    new = artifact_set(tmp_path / "new", "cpu-complete.json")
    proc = diff(tmp_path, base, new)
    assert proc.returncode == 0, proc.stderr
    assert "BASE 0 samples / 0 ns" in proc.stdout
    assert "unavailable (zero weight)" in proc.stdout
    assert "+0.00 pp" not in proc.stdout


def test_diff_write_failure_is_exit_one(tmp_path, monkeypatch, capsys):
    from sil.profile_diff import main
    base = artifact_set(tmp_path / "base", "cpu-complete.json")
    def no_space(*args):
        raise OSError("No space left on device")
    monkeypatch.setattr("sil.profile_diff.write_new_file", no_space)
    assert main([str(base), str(base), "--html", str(tmp_path / "diff.html")]) == 1
    assert "No space left on device" in capsys.readouterr().err


def test_task_bounds_retain_both_class_weight_remainders(tmp_path):
    from profile_sets import correlation_document
    from test_profile_render import render
    report = fixture("activation-complete.json")
    samples = samples_document(report)
    correlation = correlation_document(report, samples)
    summary = report["activation_correlation"]["summary"]
    for index in range(55):
        participant = f"extra-{index:03}"
        correlation["activations"].append({**correlation["activations"][0],
            "participant": participant, "samples": 0, "weight_ns": 0})
        summary["tasks"].append({"participant": participant, "task": "tick",
            "kind": "native_task", "activations": 1, "completed": 1,
            "failed": 0, "interrupted": 0, "with_samples": 0, "without_samples": 1,
            "execution_context": {"samples": 0, "weight_ns": 0},
            "window_associated": {"samples": 0, "weight_ns": 0}})
    summary["intervals"]["activations"] += 55
    profile = artifact_set(tmp_path / "set", "activation-complete.json", report=report,
                           samples=samples, correlation=correlation)
    proc = render([str(profile), "--html", "profile.html"], tmp_path)
    assert proc.returncode == 0, proc.stderr
    text = Page((tmp_path / "profile.html").read_text()).text
    remainder = text.split("7 other Tasks not expanded;", 1)[1]
    assert "10 Activations" in remainder
    assert "window_associated: 3 samples / 3003000 ns" in remainder
    proc = diff(tmp_path, profile, profile)
    assert proc.returncode == 0, proc.stderr
    remainder = proc.stdout.split("7 other Tasks not listed;", 1)[1]
    assert "BASE window_associated: 3 samples / 3003000 ns" in remainder
    assert "NEW window_associated: 3 samples / 3003000 ns" in remainder
