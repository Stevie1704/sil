"""Build metadata shipped with every Python distribution."""

from __future__ import annotations

import json
from importlib.resources import files


def _read() -> dict[str, str]:
    return json.loads(files("sil").joinpath("release.json").read_text())


_METADATA = _read()
__version__ = _METADATA["version"]
SOURCE_REPOSITORY = _METADATA["source_repository"]
SOURCE_REVISION = _METADATA["source_revision"]
LICENSE = _METADATA["license"]


def metadata() -> dict[str, str]:
    """Return a copy of the release metadata embedded in this build."""
    return dict(_METADATA)
