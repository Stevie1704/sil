"""Release identity checks that do not require a registry or Docker daemon."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from release import (  # noqa: E402
    ReleaseError,
    make_native_archive,
    project_version,
    tag_version,
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

    assert metadata() == {
        "license": "Apache-2.0",
        "source_repository": "https://github.com/Stevie1704/sil",
        "source_revision": "development",
        "version": "0.1.0",
    }


def test_native_archive_is_complete_and_deterministic(tmp_path: Path):
    prefix = tmp_path / "prefix"
    files = (
        "bin/sil-run",
        "bin/silschema",
        "include/sil/arena.h",
        "include/sil/clock_region.h",
        "include/sil/participant.h",
        "share/licenses/sil/LICENSE",
        "share/licenses/sil/NOTICE",
        "share/licenses/sil/THIRD-PARTY-NOTICES.md",
        "share/sil/release.json",
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
