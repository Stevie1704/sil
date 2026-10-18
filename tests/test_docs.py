"""`sil docs`: the user docs of the installed version, offline (#267)."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

from conftest import ROOT
from test_staged_install import installed_environment


def _sil_docs(venv: Path, *argv: str, cwd: Path) -> subprocess.CompletedProcess:
    """`sil docs` of the installed wheel, away from the checkout."""
    return subprocess.run([str(venv / "bin" / "sil"), "docs", *argv], cwd=cwd,
                          env=installed_environment(venv),
                          capture_output=True, text=True)


def test_the_wheel_holds_exactly_the_listed_topics(wheel_path: Path):
    with zipfile.ZipFile(wheel_path) as wheel:
        names = wheel.namelist()
        topics = json.loads(wheel.read("sil/_docs/topics.json"))

    packaged = {name.removeprefix("sil/_docs/").removesuffix(".md")
                for name in names
                if name.startswith("sil/_docs/") and name.endswith(".md")}
    assert packaged == set(topics)
    assert "agent-guide" in topics


def test_docs_lists_each_topic_with_its_summary(installed_python: Path, tmp_path: Path):
    proc = _sil_docs(installed_python, cwd=tmp_path)

    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert lines[0].split() == ["topics:"]
    assert lines[1].split()[0] == "agent-guide"
    assert any(line.split()[0] == "step-protocol" and "Process participant" in line
               for line in lines[1:] if line.strip())


def test_docs_topic_prints_that_doc_as_markdown(installed_python: Path, tmp_path: Path):
    proc = _sil_docs(installed_python, "step-protocol", cwd=tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("# Step protocol\n")


def test_docs_path_prints_a_directory_an_agent_can_search(
    installed_python: Path, tmp_path: Path,
):
    proc = _sil_docs(installed_python, "--path", cwd=tmp_path)

    assert proc.returncode == 0, proc.stderr
    docs = Path(proc.stdout.strip())
    assert docs.is_relative_to(installed_python)
    assert (docs / "agent-guide.md").is_file()
    assert (docs / "glossary.md").is_file()


def test_an_unknown_topic_is_a_usage_error_that_lists_the_topics(
    installed_python: Path, tmp_path: Path,
):
    proc = _sil_docs(installed_python, "step-protokol", cwd=tmp_path)

    assert proc.returncode == 2
    assert proc.stdout == ""
    assert proc.stderr.startswith("usage: sil docs ")
    assert "sil docs: error: unknown topic 'step-protokol'" in proc.stderr
    assert "\n  step-protocol " in proc.stderr


def test_docs_path_takes_no_topic(installed_python: Path, tmp_path: Path):
    proc = _sil_docs(installed_python, "--path", "running", cwd=tmp_path)

    assert proc.returncode == 2
    assert proc.stdout == ""
    assert proc.stderr.endswith("sil docs: error: --path takes no topic\n")


def test_a_checkout_points_at_its_docs_directory(tmp_path: Path):
    """An unstamped checkout has no packaged docs, but has them in `docs/`."""
    proc = subprocess.run([sys.executable, "-m", "sil.docs", "agent-guide"],
                          cwd=tmp_path, capture_output=True, text=True,
                          check=False)

    assert proc.returncode == 1
    assert proc.stdout == ""
    assert str(ROOT / "docs" / "agent-guide.md") in proc.stderr


def _member(document, path: str) -> bool:
    """Whether the dotted `path` exists in `document`; a list uses its first item."""
    value = document
    for key in path.split("."):
        if isinstance(value, list):
            value = value[0]
        if not isinstance(value, dict) or key not in value:
            return False
        value = value[key]
    return True


def test_the_agent_guide_names_only_report_members_that_exist():
    guide = (ROOT / "docs" / "agent-guide.md").read_text()
    section = guide.partition("## Measure a Run")[2].partition("\n## ")[0]
    fixtures = ROOT / "tests" / "fixtures" / "diagnostics"
    run = json.loads((fixtures / "run-success.json").read_text())
    benchmark = json.loads((fixtures / "benchmark-complete.json").read_text())
    paths = re.findall(r"`((?:observational|completeness|status|runs)"
                       r"[a-z_.\[\]]*)`", section)

    assert len(paths) >= 8
    for path in paths:
        document = benchmark if path.startswith(("status", "runs")) else run
        assert _member(document, path.replace("[]", "")), path
