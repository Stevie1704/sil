"""What the authoring commands share: reading a strict JSON document, and
digesting the files a Run is authored from.

A document is JSON with no duplicate key and no non-finite number, and every
object in it has exactly the keys its command states. Nothing here knows what
a document means.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


class AuthoringError(ValueError):
    """A Run a command refuses to write a Manifest for."""


def require_distinct(path: Path, written: str,
                     others: dict[str, Path]) -> None:
    """Refuse to write `path` over a file the Run reads or another output.

    Paths are compared resolved, so a relative spelling or a symbolic link
    of the same file is the same file.
    """
    for role, other in others.items():
        if path.resolve() == Path(other).resolve():
            raise AuthoringError(
                f"{written} path {str(path)!r} is the {role}; writing the "
                f"{written} would replace it"
            )


def read(path: Path, role: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as e:
        raise AuthoringError(f"cannot read {role} {str(path)!r}: {e}") from e


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def load(data: bytes, path: Path):
    """The JSON value `data` holds, with no duplicate key and no NaN."""
    try:
        return json.loads(data.decode("utf-8"),
                          parse_constant=_non_finite_literal,
                          object_pairs_hook=_unique_keys)
    except AuthoringError as e:
        raise AuthoringError(f"authoring document {str(path)!r}: {e}") from None
    except ValueError as e:
        raise AuthoringError(
            f"authoring document {str(path)!r} is not valid JSON: {e}"
        ) from e


def _non_finite_literal(literal: str):
    raise AuthoringError(f"{literal} is not a finite number")


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    doc: dict = {}
    for key, value in pairs:
        if key in doc:
            raise AuthoringError(f"duplicate key {key!r}")
        doc[key] = value
    return doc


def exact_object(value, context: str, keys: set[str]) -> dict:
    """`value` as an object that has exactly `keys`."""
    if not isinstance(value, dict):
        raise AuthoringError(f"{context} must be an object")
    unknown = sorted(set(value) - keys)
    if unknown:
        raise AuthoringError(f"{context} has unknown key(s) "
                             + ", ".join(map(repr, unknown)))
    missing = sorted(keys - set(value))
    if missing:
        raise AuthoringError(f"{context} is missing key(s) "
                             + ", ".join(map(repr, missing)))
    return value


def array(value, context: str) -> list:
    if not isinstance(value, list):
        raise AuthoringError(f"{context} must be an array")
    return value


def string(value, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise AuthoringError(f"{context} must be a non-empty string")
    return value


def unit(value, context: str) -> str | None:
    return None if value is None else string(value, context)
