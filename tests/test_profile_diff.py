"""Compare portable artifact sets through the installed CLI boundary."""

import json
import subprocess
import sys

import pytest

from profile_sets import artifact_set, fixture, samples_document
from test_diagnostics_html import Page
from test_diagnostics_html import FIXTURES as DIAGNOSTICS


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
    assert "Self estimated weight BASE (ns)" in page.text
    assert "Inclusive estimated weight change (ns)" in page.text
    line = next(line for line in proc.stdout.splitlines()
                if line.startswith("silproof_native_spin_even"))
    assert "3003000 | 6006000 | +3003000" in line


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
    assert "0 | 3003000 | +3003000" in proc.stdout


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


def test_diff_keeps_repeated_benchmark_experiments_separate(tmp_path):
    import hashlib
    base = artifact_set(tmp_path / "base", "cpu-complete.json")
    before = tmp_path / "before.json"
    before.write_bytes((DIAGNOSTICS / "benchmark-complete.json").read_bytes())
    after = tmp_path / "after.json"
    after.write_bytes((DIAGNOSTICS / "benchmark-partial.json").read_bytes())

    proc = diff(tmp_path, base, base, "--base-benchmark", str(before),
                "--new-benchmark", str(after), "--html", "diff.html")

    assert proc.returncode == 0, proc.stderr
    text = Page((tmp_path / "diff.html").read_text()).text
    assert "BASE benchmark" in text and "NEW benchmark" in text
    assert hashlib.sha256(before.read_bytes()).hexdigest() in text
    assert "Separate repeated experiments" in text
    assert "warm-ups excluded" in text
    assert "run_wall_ns" in text and "scheduled_execution_ns" in text
    assert "run-001" in text and "run-002" in text
    assert "p95_ns" in text and "insufficient_samples" in text
    assert "reservoir" in text and "run_failed" in text
    assert "WARNING" in proc.stdout
    assert "same binaries" in text


@pytest.mark.parametrize("change", [
    lambda b: b["counts"].update(completed=1),
    lambda b: b["status"].update(complete=False),
    lambda b: b["aggregates"]["metrics"][0].update(mean=1),
    lambda b: b["aggregates"]["metrics"][0]["per_run"][0].update(label="warmup-001"),
    lambda b: b["aggregates"]["metrics"][0]["per_run"][0].update(value=123),
    lambda b: b["aggregates"]["metrics"][0].update(mean=float("inf")),
    lambda b: b.update(sil_benchmark_report=99),
])
def test_diff_rejects_explicit_inconsistent_benchmarks_before_output(tmp_path, change):
    profile = artifact_set(tmp_path / "base", "cpu-complete.json")
    benchmark = json.loads((DIAGNOSTICS / "benchmark-complete.json").read_text())
    change(benchmark)
    path = tmp_path / "benchmark.json"
    path.write_text(json.dumps(benchmark))
    proc = diff(tmp_path, profile, profile, "--base-benchmark", str(path),
                "--new-benchmark", str(path), "--html", "diff.html")
    assert proc.returncode == 2, proc.stderr
    assert not (tmp_path / "diff.html").exists()
    assert not proc.stdout


def test_diff_requires_paired_existing_bounded_benchmark_files(tmp_path):
    profile = artifact_set(tmp_path / "base", "cpu-complete.json")
    missing = tmp_path / "missing.json"
    assert diff(tmp_path, profile, profile, "--base-benchmark", str(missing)).returncode == 2
    assert diff(tmp_path, profile, profile, "--new-benchmark", str(missing)).returncode == 2
    assert diff(tmp_path, profile, profile, "--base-benchmark", str(missing),
                "--new-benchmark", str(missing)).returncode == 2
    oversized = tmp_path / "oversized.json"
    with oversized.open("wb") as stream:
        stream.truncate(256 * 1024 * 1024 + 1)
    proc = diff(tmp_path, profile, profile, "--base-benchmark", str(oversized),
                "--new-benchmark", str(oversized), "--html", "diff.html")
    assert proc.returncode == 2
    assert "exceeds 268435456 bytes" in proc.stderr
    assert not (tmp_path / "diff.html").exists()


def test_unknown_and_different_benchmark_identity_is_visible_not_silently_joined(tmp_path):
    profile = artifact_set(tmp_path / "base", "cpu-complete.json")
    before = json.loads((DIAGNOSTICS / "benchmark-complete.json").read_text())
    after = json.loads((DIAGNOSTICS / "benchmark-no-successful-run.json").read_text())
    before["policy"]["provenance_mode"] = "external"
    before["host"]["architecture"] = "different-host"
    before["identity"]["source_revision"] = "</script><script>window.pwned=1</script>"
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps(before)); b.write_text(json.dumps(after))
    proc = diff(tmp_path, profile, profile, "--base-benchmark", str(a),
                "--new-benchmark", str(b), "--html", "diff.html")
    assert proc.returncode == 0, proc.stderr
    assert "unknown profile/benchmark identity" in proc.stdout
    assert "Provenance mode: profile/benchmark mismatch" in proc.stdout
    assert "Host: profile/benchmark mismatch" in proc.stdout
    html = (tmp_path / "diff.html").read_text()
    assert "<script>window.pwned" not in html
    assert "diagnostics report missing" in Page(html).text
    assert "Artifacts" in Page(html).text
    assert "Within-experiment metadata differs" in proc.stdout


def test_benchmark_display_bounds_long_metadata_and_retains_download(tmp_path):
    import base64
    profile = artifact_set(tmp_path / "base", "cpu-complete.json")
    benchmark = json.loads((DIAGNOSTICS / "benchmark-complete.json").read_text())
    long_name = "hostile<&>" * 2000
    benchmark["identity"]["source_revision"] = long_name
    path = tmp_path / "benchmark.json"
    path.write_text(json.dumps(benchmark))
    proc = diff(tmp_path, profile, profile, "--base-benchmark", str(path),
                "--new-benchmark", str(path), "--html", "diff.html")
    assert proc.returncode == 0, proc.stderr
    page = Page((tmp_path / "diff.html").read_text())
    assert "characters omitted; see Download JSON" in page.text
    assert long_name not in page.text
    download = page.find("a", id="download")[0]["href"].split(",", 1)[1]
    data = json.loads(base64.b64decode(download))
    assert data["benchmarks"]["base"]["report"]["identity"]["source_revision"] == long_name


def test_recursive_inclusive_absolute_weights_count_a_sample_once(tmp_path):
    report = fixture("cpu-complete.json")
    samples = samples_document(report)
    # The root is repeated through direct and indirect recursion on every stack.
    samples["stacks"] = [stack + [0, stack[0], 0] for stack in samples["stacks"]]
    profile = artifact_set(tmp_path / "profile", "cpu-complete.json", report=report,
                           samples=samples)
    proc = diff(tmp_path, profile, profile)
    assert proc.returncode == 0, proc.stderr
    root = next(row for row in proc.stdout.splitlines() if row.startswith("main "))
    assert "100.00% | 100.00% | +0.00 pp | 10010000 | 10010000 | +0" in root
    assert "Inclusive shares overlap" in proc.stdout


def test_cpu_absolute_weight_rankings_bound_functions_and_preserve_remainders(tmp_path):
    report = fixture("cpu-complete.json")
    samples = samples_document(report)
    for index in range(110):
        samples["frames"].append({"module": None, "symbol": f"caller-{index:03}",
                                  "source": None, "address": "0x1", "module_offset": None})
        for stack in samples["stacks"]:
            stack.append(len(samples["frames"]) - 1)
    profile = artifact_set(tmp_path / "profile", "cpu-complete.json", report=report,
                           samples=samples)
    proc = diff(tmp_path, profile, profile, "--html", "diff.html")
    assert proc.returncode == 0, proc.stderr
    assert "17 other functions not listed" in proc.stdout
    assert "self remainder: BASE 9009000 ns, NEW 9009000 ns" in proc.stdout
    html = (tmp_path / "diff.html").read_text()
    functions = html.split('id="diff-functions"', 1)[1].split('</table>', 1)[0]
    assert functions.count('<tr>') == 101  # 100 function rows plus the header
