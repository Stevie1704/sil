"""Release identity checks that do not require a registry or Docker daemon."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from release import (  # noqa: E402
    REQUIRED_NATIVE_PATHS,
    ReleaseError,
    make_native_archive,
    project_version,
    stamp_python,
    tag_version,
    validate_release_title,
    validate_native_archive,
)


def test_project_version_is_a_release_semver():
    assert project_version(ROOT) == "0.1.0"
    assert tag_version("v0.1.0") == "0.1.0"


def test_tag_convention_rejects_non_release_names():
    for tag in ("0.1.0", "v0.1", "v01.2.3", "v0.1.0-rc1"):
        with pytest.raises(ReleaseError):
            tag_version(tag)


def test_python_build_metadata_is_self_describing():
    from sil.build_info import metadata

    value = metadata()
    assert value["license"] == "Apache-2.0"
    assert value["source_repository"] == "https://github.com/Stevie1704/sil"
    assert value["source_revision"]
    assert value["version"] == "0.1.0"


def test_stamp_python_does_not_mutate_the_checkout(tmp_path: Path):
    source = ROOT / "python" / "src" / "sil" / "release.json"
    before = source.read_bytes()
    destination = tmp_path / "python-release"

    stamp_python(ROOT, "0.1.0", "test-revision", destination)

    assert source.read_bytes() == before
    assert json.loads(
        (destination / "src" / "sil" / "release.json").read_text()
    )["source_revision"] == "test-revision"


def test_release_title_must_match_version():
    validate_release_title("0.1.0", "0.1.0")
    with pytest.raises(ReleaseError):
        validate_release_title("SiL 0.1.0", "0.1.0")


def test_native_archive_is_complete_and_deterministic(tmp_path: Path):
    prefix = tmp_path / "prefix"
    files = (
        *REQUIRED_NATIVE_PATHS,
        "lib/libsil_clock_shim.so",
    )
    for relative in files:
        path = prefix / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative.endswith("release.json"):
            path.write_text(
                json.dumps(
                    {
                        "license": "Apache-2.0",
                        "source_repository": "https://github.com/Stevie1704/sil",
                        "source_revision": "test-revision",
                        "version": "0.1.0",
                    }
                )
            )
        else:
            path.write_text(relative)

    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    make_native_archive(prefix, first, "0.1.0")
    make_native_archive(prefix, second, "0.1.0")
    assert first.read_bytes() == second.read_bytes()
    validate_native_archive(first, "0.1.0", "test-revision")


def test_runner_reports_the_project_version(sil_run):
    version = subprocess.run(
        [str(sil_run), "--version"], capture_output=True, text=True, check=True
    )
    info = subprocess.run(
        [str(sil_run), "--build-info"], capture_output=True, text=True, check=True
    )
    assert version.stdout == "0.1.0\n"
    assert json.loads(info.stdout)["version"] == "0.1.0"


def test_stamp_python_packages_every_doc_listed_as_a_topic(tmp_path: Path):
    destination = tmp_path / "python-release"

    stamp_python(ROOT, "0.1.0", "test-revision", destination)

    docs = destination / "src" / "sil" / "_docs"
    topics = json.loads((docs / "topics.json").read_text())
    assert {"agent-guide", "step-protocol", "glossary", "support"} <= set(topics)
    assert all(summary for summary in topics.values())
    assert {path.stem for path in docs.glob("*.md")} == set(topics)
    assert (docs / "step-protocol.md").read_text().startswith("# Step protocol\n")


def test_stamped_docs_link_each_other_and_the_release_for_the_rest(tmp_path: Path):
    destination = tmp_path / "python-release"

    stamp_python(ROOT, "0.1.0", "test-revision", destination)

    docs = destination / "src" / "sil" / "_docs"
    support = (docs / "support.md").read_text()
    assert "](library.md#when-the-clock-shim-applies)" in support
    assert ("](https://github.com/Stevie1704/sil/blob/v0.1.0/include/sil/participant.h)"
            in support)
    running = (docs / "running.md").read_text()
    assert "](step-protocol.md)" in running
    assert "](https://github.com/Stevie1704/sil/blob/v0.1.0/README.md)" in running
    library = (docs / "library.md").read_text()
    assert "](https://github.com/Stevie1704/sil/tree/v0.1.0/examples/library/)" in library


def test_every_stamped_doc_link_resolves_in_the_package_or_the_release(tmp_path: Path):
    destination = tmp_path / "python-release"

    stamp_python(ROOT, "0.1.0", "test-revision", destination)

    docs = destination / "src" / "sil" / "_docs"
    for doc in docs.glob("*.md"):
        for target in re.findall(r"\]\(([^)]+)\)", doc.read_text()):
            if target.startswith(("https://", "http://", "mailto:", "#")):
                continue
            assert (docs / target.split("#")[0]).is_file(), f"{doc.name}: {target}"
