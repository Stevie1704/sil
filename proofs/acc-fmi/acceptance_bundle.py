"""The bundle index: what a prepared bundle contains and how a Run checks it.

Preparation writes one digest per pinned artifact. Every acceptance Run
re-reads the bundle from disk and re-hashes it, so a tampered, truncated or
partially regenerated bundle fails before any Run starts.
"""
from __future__ import annotations

import json
from pathlib import Path

from acceptance_contract import BUNDLE_FORMAT, INDEX_NAME
from proof_support import file_sha256, require, write_json


def contents(root: Path) -> dict[str, str]:
    """Digest every bundle file except the index that records the digests."""
    return {
        str(path.relative_to(root)): file_sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != INDEX_NAME
    }


def write_index(root: Path, identity: dict) -> dict:
    index = {"bundle_format": BUNDLE_FORMAT, **identity, "contents_sha256": contents(root)}
    write_json(root / INDEX_NAME, index)
    return index


def verify(root: Path) -> dict:
    """Re-hash the bundle; a missing, extra or altered file fails the Run."""
    path = root / INDEX_NAME
    require(path.is_file(), f"bundle index {path} is missing")
    index = json.loads(path.read_text())
    require(
        index.get("bundle_format") == BUNDLE_FORMAT,
        f"bundle format {index.get('bundle_format')} is not {BUNDLE_FORMAT}",
    )
    recorded = index["contents_sha256"]
    actual = contents(root)
    missing = sorted(set(recorded) - set(actual))
    require(not missing, f"bundle is missing pinned artifacts: {missing}")
    unexpected = sorted(set(actual) - set(recorded))
    require(not unexpected, f"bundle carries unrecorded artifacts: {unexpected}")
    altered = sorted(name for name, digest in recorded.items() if actual[name] != digest)
    require(not altered, f"bundle artifacts differ from their recorded digests: {altered}")
    return index
