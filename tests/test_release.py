"""Release identity checks that do not require a registry or Docker daemon."""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from release import (  # noqa: E402
    PACKAGED_DOCS,
    PACKAGED_EXAMPLES,
    REQUIRED_NATIVE_PATHS,
    ReleaseError,
    make_native_archive,
    project_version,
    stamp_python,
    tag_version,
    validate_release_title,
    validate_native_archive,
)

VERSION = project_version(ROOT)


def test_project_version_is_a_release_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", project_version(ROOT))
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
    assert value["version"] == VERSION


def test_stamp_python_does_not_mutate_the_checkout(tmp_path: Path):
    source = ROOT / "python" / "src" / "sil" / "release.json"
    before = source.read_bytes()
    destination = tmp_path / "python-release"

    stamp_python(ROOT, VERSION, "test-revision", destination)

    assert source.read_bytes() == before
    assert json.loads(
        (destination / "src" / "sil" / "release.json").read_text()
    )["source_revision"] == "test-revision"


@pytest.mark.parametrize("dockerfile", [
    "Dockerfile",
    "proofs/adas-closed-loop/Dockerfile",
    "proofs/adas-cost/Dockerfile",
    "proofs/adas-equivalence/Dockerfile",
    "proofs/osmp-qualification/Dockerfile",
])
def test_docker_build_has_release_inputs_before_stamping(tmp_path: Path, dockerfile: str):
    """Exercise stamping with only inputs copied before the Docker RUN.

    Later COPY instructions cannot satisfy the release build's inputs. The
    ADAS matrix uses the closed-loop image as its preparation environment.
    """
    copied = []
    instructions = (ROOT / dockerfile).read_text().replace("\\\n", " ").splitlines()
    for instruction in instructions:
        if instruction.startswith("RUN ") and "stamp-python" in instruction:
            break
        if instruction.startswith("COPY ") and "--from=" not in instruction:
            copied.extend(Path(source) for source in shlex.split(instruction)[1:-1]
                          if not source.startswith("--"))
    else:
        pytest.fail(f"{dockerfile} no longer invokes stamp-python")
    inputs = {"python", "LICENSE", "tools/sil-skill.md",
              *(source for source, _ in PACKAGED_DOCS.values()),
              *(f"examples/{name}" for name in PACKAGED_EXAMPLES)}
    staged = tmp_path / "src"
    for name in sorted(inputs):
        if not any(Path(name).is_relative_to(source) for source in copied):
            continue
        target = staged / name
        target.parent.mkdir(parents=True, exist_ok=True)
        source = ROOT / name
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copyfile(source, target)
    release = tmp_path / "release"
    stamp_python(staged, VERSION, "test-docker-inputs", release)
    examples = release / "src" / "sil" / "_examples"
    assert all((examples / name).is_dir() for name in PACKAGED_EXAMPLES)
    assert (examples / "library" / "adapter.py").is_file()
    assert (examples / "bundle" / "prepare.py").is_file()


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
                        "version": VERSION,
                    }
                )
            )
        else:
            path.write_text(relative)

    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    make_native_archive(prefix, first, VERSION)
    make_native_archive(prefix, second, VERSION)
    assert first.read_bytes() == second.read_bytes()
    validate_native_archive(first, VERSION, "test-revision")


def test_runner_reports_the_project_version(sil_run):
    version = subprocess.run(
        [str(sil_run), "--version"], capture_output=True, text=True, check=True
    )
    info = subprocess.run(
        [str(sil_run), "--build-info"], capture_output=True, text=True, check=True
    )
    assert version.stdout == f"{VERSION}\n"
    assert json.loads(info.stdout)["version"] == VERSION


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    """The docs directory of a freshly stamped Python tree."""
    destination = tmp_path / "python-release"
    stamp_python(ROOT, VERSION, "test-revision", destination)
    return destination / "src" / "sil" / "_docs"


def test_stamp_python_packages_every_doc_listed_as_a_topic(docs: Path):
    topics = json.loads((docs / "topics.json").read_text())
    assert {"agent-guide", "step-protocol", "glossary", "support",
            "examples", "recording-queries"} <= set(topics)
    assert all(summary for summary in topics.values())
    assert {path.stem for path in docs.glob("*.md")} == set(topics)
    assert (docs / "step-protocol.md").read_text().startswith("# Step protocol\n")


def test_stamped_docs_link_each_other_and_the_release_for_the_rest(docs: Path):
    support = (docs / "support.md").read_text()
    assert "](library.md#when-the-clock-shim-applies)" in support
    assert (f"](https://github.com/Stevie1704/sil/blob/v{VERSION}/include/sil/participant.h)"
            in support)
    running = (docs / "running.md").read_text()
    assert "](step-protocol.md)" in running
    # The diagnostics report contract ships with the commands that write it.
    assert "](diagnostics-report.md)" in running
    assert f"](https://github.com/Stevie1704/sil/blob/v{VERSION}/README.md)" in running
    library = (docs / "library.md").read_text()
    assert "](../_examples/library/)" in library


def test_every_stamped_doc_link_resolves_in_the_package_or_the_release(docs: Path):
    for doc in docs.glob("*.md"):
        for target in re.findall(r"\]\(([^)]+)\)", doc.read_text()):
            if target.startswith(("https://", "http://", "mailto:", "#")):
                continue
            assert (docs / target.split("#")[0]).exists(), f"{doc.name}: {target}"


def test_every_doc_command_in_a_packaged_guide_has_a_topic(docs: Path):
    topics = json.loads((docs / "topics.json").read_text())
    for doc in docs.glob("*.md"):
        for topic in re.findall(r"sil\s+docs\s+([a-z][a-z-]*)", doc.read_text()):
            assert topic in topics, f"{doc.name}: sil docs {topic}"
