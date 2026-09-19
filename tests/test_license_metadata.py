"""The license metadata a release declares, checked at the repository boundary.

Issue #116 attaches a wheel, a source distribution, a native-development
archive, and a container image to one release, and every one of them has to
declare the same grant. These checks keep the declarations from drifting apart
between releases (issue #122).
"""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPDX_IDENTIFIER = "Apache-2.0"
# The canonical text from https://www.apache.org/licenses/LICENSE-2.0.txt.
# "Unmodified" is the point of the file, so it is pinned by content.
APACHE_2_0_SHA256 = (
    "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
)
POLICY_FILES = (
    "LICENSE",
    "NOTICE",
    "THIRD-PARTY-NOTICES.md",
    "SECURITY.md",
    "SUPPORT.md",
    "CONTRIBUTING.md",
)


def test_license_is_the_unmodified_apache_2_0_text():
    digest = hashlib.sha256((ROOT / "LICENSE").read_bytes()).hexdigest()
    assert digest == APACHE_2_0_SHA256


def test_notice_names_the_holder_and_the_identifier():
    notice = (ROOT / "NOTICE").read_text()
    assert "Copyright 2026 Steffen Krakau" in notice
    assert f"SPDX-License-Identifier: {SPDX_IDENTIFIER}" in notice


@pytest.mark.parametrize("name", ["LICENSE", "NOTICE"])
def test_python_distribution_ships_the_root_text_verbatim(name: str):
    """The wheel and the sdist can only include files under `python/`.

    The copies are what a consumer reads, so they have to stay byte-identical
    to the root files this repository is licensed under.
    """
    assert (ROOT / "python" / name).read_bytes() == (ROOT / name).read_bytes()


def test_python_metadata_declares_the_same_identifier():
    metadata = tomllib.loads((ROOT / "python" / "pyproject.toml").read_text())
    project = metadata["project"]
    assert project["license"] == SPDX_IDENTIFIER
    assert set(project["license-files"]) == {"LICENSE", "NOTICE"}
    # PEP 639 license expressions need this backend version.
    assert metadata["build-system"]["requires"] == ["hatchling>=1.27"]


def test_third_party_inventory_matches_the_container_lock():
    """Every pinned runtime dependency is inventoried at the version shipped.

    The image itself is checked in tests/test_container.py; this keeps the
    inventory honest without a Docker daemon.
    """
    inventory = (ROOT / "THIRD-PARTY-NOTICES.md").read_text()
    for name in ("nlohmann/json", "MCAP C++"):
        assert name in inventory
    lock = (ROOT / "container" / "requirements.lock").read_text()
    pins = [
        line.split("==")
        for line in lock.splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert pins, "the runtime lock declares no dependency"
    for name, version in pins:
        row = f"[{name}](https://pypi.org/project/{name}/) | {version} |"
        assert row in inventory, f"{name} {version} is not inventoried"


def test_support_statement_keeps_its_two_disclaimers():
    support = (ROOT / "SUPPORT.md").read_text()
    assert "SiL is not safety-qualified" in support
    assert "no safety case" in support
    assert "does not extend across machine classes" in support


def test_readme_links_every_policy_file():
    readme = (ROOT / "README.md").read_text()
    for name in POLICY_FILES:
        assert f"]({name})" in readme, f"README does not link {name}"


@pytest.mark.parametrize("name", POLICY_FILES)
def test_policy_file_exists(name: str):
    assert (ROOT / name).is_file()
