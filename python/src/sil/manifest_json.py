"""Read Manifest JSON as the kernel's parser reads it (#390, #394).

`json.loads` keeps the last value of a repeated key and says nothing. The
kernel refuses such a Manifest, so every tool that reads a Manifest, or JSON
that goes into one, reads it with `loads` and gives the kernel's verdict.
"""

from __future__ import annotations

import json
import math

from ._schema_types import INT_RANGES


class RepeatedKey(ValueError):
    """An object repeats a key. Each item of `repeats` is (object path, key).
    """

    def __init__(self, repeats: list[tuple[tuple, str]]):
        self.repeats = repeats
        super().__init__("; ".join(f"object '{pointer(path)}' repeats key "
                                   f"'{key}'" for path, key in repeats))


class LoneSurrogate(ValueError):
    """A string has half of a surrogate pair only."""

    def __init__(self):
        super().__init__("a string has a lone surrogate escape")


def pointer(tokens: tuple) -> str:
    """The JSON Pointer (RFC 6901) of a path."""
    return "".join("/" + str(token).replace("~", "~0").replace("/", "~1")
                   for token in tokens)


def loads(text: str | bytes):
    """The document in `text`; ValueError if the kernel refuses it."""
    if isinstance(text, bytes):
        text = text.decode("utf-8-sig")
    repeated = _RepeatedKeys()
    doc = json.loads(text, parse_int=_parse_int, parse_float=_parse_float,
                     parse_constant=_reject_constant,
                     object_pairs_hook=repeated)
    try:
        json.dumps(doc, ensure_ascii=False).encode()
    except UnicodeEncodeError:
        raise LoneSurrogate() from None
    if repeated.found:
        raise RepeatedKey(_repeats(doc, repeated.found))
    return doc


def _parse_int(text: str):
    # The kernel's JSON parser keeps an integer outside the 64-bit ranges as a
    # floating-point number, which no integer key accepts.
    value = int(text)
    low, high = INT_RANGES["i64"][0], INT_RANGES["u64"][1]
    return value if low <= value <= high else _parse_float(text)


def _parse_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"number overflow parsing '{text}'")
    return value


def _reject_constant(text: str):
    raise ValueError(f"invalid literal '{text}'")


class _RepeatedKeys:
    """`object_pairs_hook` that keeps the last value of a repeated key, as the
    kernel's JSON parser does, and records each repeat."""

    def __init__(self):
        self.found: list[tuple[dict, str]] = []

    def __call__(self, pairs) -> dict:
        obj = {}
        for key, value in pairs:
            if key in obj:
                self.found.append((obj, key))
            obj[key] = value
        return obj


def _container_paths(value, path=()):
    """The path of every object and array in `value`, by object identity."""
    paths = {id(value): path}
    items = value.items() if isinstance(value, dict) else enumerate(value)
    for token, child in items:
        if isinstance(child, (dict, list)):
            paths.update(_container_paths(child, path + (token,)))
    return paths


def _repeats(doc, found) -> list[tuple[tuple, str]]:
    # An object inside a dropped first definition is not in `doc`; the repeat
    # that dropped it is reported.
    paths = _container_paths(doc) if isinstance(doc, (dict, list)) else {}
    return [(paths[id(obj)], key) for obj, key in found if id(obj) in paths]
