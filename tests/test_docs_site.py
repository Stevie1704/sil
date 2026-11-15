"""Staging of the documentation site from the canonical Markdown (#401)."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import load_module

ROOT = Path(__file__).resolve().parents[1]
docs_site = load_module("docs_site", ROOT / "tools" / "docs_site.py")

REVISION = "0123456789abcdef0123456789abcdef01234567"
BLOB = f"https://github.com/Stevie1704/sil/blob/{REVISION}"
TREE = f"https://github.com/Stevie1704/sil/tree/{REVISION}"


def _checkout(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def _staged(tmp_path: Path, files: dict[str, str]) -> Path:
    root = _checkout(tmp_path / "repo", {"site/index.md": "# Home\n", **files})
    out = tmp_path / "out"
    docs_site.stage(root, out, REVISION)
    return out


def test_a_link_between_published_pages_stays_relative(tmp_path: Path):
    out = _staged(tmp_path, {
        "docs/fmi.md": "See [running](running.md#run-contract).\n",
        "docs/running.md": "# Running\n",
    })

    assert (out / "docs" / "fmi.md").read_text() == (
        "See [running](running.md#run-contract).\n")


def test_a_link_to_an_unpublished_file_points_at_the_source_revision(tmp_path: Path):
    out = _staged(tmp_path, {
        "docs/running.md": "The [ABI](../include/sil/participant.h#L3).\n",
        "include/sil/participant.h": "",
    })

    assert (out / "docs" / "running.md").read_text() == (
        f"The [ABI]({BLOB}/include/sil/participant.h#L3).\n")


def test_a_link_to_a_missing_target_fails_the_stage(tmp_path: Path):
    with pytest.raises(docs_site.SiteError,
                       match=r"docs/fmi\.md: link 'gone\.md' names no file"):
        _staged(tmp_path, {"docs/fmi.md": "See [gone](gone.md).\n"})


def test_the_landing_page_links_from_the_site_root(tmp_path: Path):
    out = _staged(tmp_path, {
        "site/index.md": "[Support](../SUPPORT.md#scope), [FMI](../docs/fmi.md)\n",
        "SUPPORT.md": "# Support\n",
        "docs/fmi.md": "[Glossary](../GLOSSARY.md)\n",
        "GLOSSARY.md": "# Glossary\n",
    })

    assert (out / "index.md").read_text() == (
        "[Support](SUPPORT.md#scope), [FMI](docs/fmi.md)\n")
    assert (out / "docs" / "fmi.md").read_text() == "[Glossary](../GLOSSARY.md)\n"
    assert not (out / "site").exists()


@pytest.mark.parametrize("page", ["docs/proposals/x.md", "docs/research/x.md"])
def test_prospective_material_says_so_below_its_title(tmp_path: Path, page: str):
    out = _staged(tmp_path, {page: "# Idea\n\nBody.\n"})

    title, notice, body = (out / page).read_text().split("\n\n", 2)
    assert title == "# Idea"
    assert notice.startswith('!!! warning "Prospective material"\n')
    assert "not implemented or supported behavior" in notice
    assert body == "Body.\n"


def test_agent_instructions_and_their_directory_link_stay_on_github(tmp_path: Path):
    out = _staged(tmp_path, {
        "docs/README.md": "[Agents](agents/)\n",
        "docs/agents/traps.md": "# Traps\n",
    })

    assert not (out / "docs" / "agents").exists()
    assert (out / "docs" / "README.md").read_text() == f"[Agents]({TREE}/docs/agents/)\n"
