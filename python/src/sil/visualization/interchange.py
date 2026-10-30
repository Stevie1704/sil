"""The `sil_visualization_json` interchange profile, version 1 (spec #345 s1).

One source Channel maps to one topic with a generated JSON Schema
(`jsonschema` encoding) and JSON Messages (`json` encoding). A payload has two
members, so a source field name never collides with SiL's own members:

    {"sil": {"publication_time_ns": "...", "record_index": "...",
             "nonfinite": {"/fields/x": "NaN"}},
     "fields": {...decoded source fields...}}

Representation, per SiL type:

* Integers up to 32 bits: JSON integers.
* i64 and u64, also as array elements: decimal strings, so a JavaScript
  consumer keeps all 64 bits.
* Finite f32 and f64: JSON numbers that round-trip to the decoded value.
* A non-finite float: null, and a JSON pointer in `sil.nonfinite` names
  `NaN`, `Infinity` or `-Infinity`. Missing data never becomes zero.
* A u8 array: a base64 string (`contentEncoding: base64` in the schema).
* Any other array: a JSON array of exactly the declared length.

Times and locators are decimal strings in the payload. Nothing is inferred
from field names: no units, frames, sample times or entities.

The codec decodes with `sil.schema`, the same byte contract as the kernel.
A served-playback adapter reuses `ChannelCodec` to send identical payloads.
"""

from __future__ import annotations

import base64
import json
import math

from sil._schema_types import FORMATS, INT_RANGES
from sil.schema import MessageType

PROFILE = "sil_visualization_json"
PROFILE_VERSION = 1
SCHEMA_ENCODING = "jsonschema"
MESSAGE_ENCODING = "json"
SOURCE_ENCODING = "sil_pod"

_WIDE_INTEGERS = frozenset({"i64", "u64"})
_FLOATS = frozenset({"f32", "f64"})
_DECIMAL = r"^-?(0|[1-9][0-9]*)$"
_NONFINITE = {"NaN", "Infinity", "-Infinity"}

NUMERIC_POLICY = {
    "integers_up_to_32_bit": "JSON integer",
    "i64_u64": "decimal string, also as array elements",
    "finite_float": "JSON number that round-trips to the decoded value",
    "nonfinite_float": "null, named in sil.nonfinite by JSON pointer",
    "u8_array": "base64 string",
    "other_arrays": "JSON array of the declared length",
    "time": "integer nanoseconds of Virtual time; no offset; decimal "
            "strings in payloads",
}


class UnsupportedSchema(ValueError):
    """The source Schema is not a supported SiL schema declaration."""


class MalformedPayload(ValueError):
    """A source payload does not match its Schema's fixed layout."""


class ChannelCodec:
    """Converts the `sil_pod` payloads of one source Schema to the profile."""

    def __init__(self, schema_name: str, declaration: dict):
        self.schema_name = schema_name
        self._fields = _checked_fields(schema_name, declaration)
        self._type = MessageType(schema_name, {"fields": self._fields})

    def json_schema(self) -> bytes:
        """The generated JSON Schema, with original types and array lengths
        in its descriptions."""
        fields = {field["name"]: _field_schema(field) for field in self._fields}
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": self.schema_name,
            "description": (f"SiL Schema {self.schema_name} in profile "
                            f"{PROFILE} v{PROFILE_VERSION}"),
            "type": "object",
            "properties": {
                "sil": _SIL_MEMBER,
                "fields": {"type": "object", "properties": fields,
                           "required": list(fields),
                           "additionalProperties": False},
            },
            "required": ["sil", "fields"],
            "additionalProperties": False,
        }
        return json.dumps(schema, separators=(",", ":")).encode()

    def payload(self, data: bytes, publication_time_ns: int,
                record_index: int) -> bytes:
        """The JSON payload of one source Message."""
        if len(data) != self._type.size:
            raise MalformedPayload(
                f"a {self.schema_name} payload has {self._type.size} bytes, "
                f"got {len(data)}")
        decoded = self._type.unpack(data)
        nonfinite: dict[str, str] = {}
        fields = {field["name"]: _value(field, decoded[field["name"]],
                                        "/fields/" + _pointer(field["name"]),
                                        nonfinite)
                  for field in self._fields}
        envelope = {"sil": {"publication_time_ns": str(publication_time_ns),
                            "record_index": str(record_index),
                            "nonfinite": nonfinite},
                    "fields": fields}
        return json.dumps(envelope, separators=(",", ":"),
                          allow_nan=False).encode()


_SIL_MEMBER = {
    "type": "object",
    "description": "SiL locator and representation notes; not a source field",
    "properties": {
        "publication_time_ns": {
            "type": "string", "pattern": _DECIMAL,
            "description": "publication time in integer nanoseconds of "
                           "Virtual time"},
        "record_index": {
            "type": "string", "pattern": _DECIMAL,
            "description": "ordinal in the whole source's stored Message "
                           "order; with the source SHA-256 a stable locator"},
        "nonfinite": {
            "type": "object",
            "description": "JSON pointer of each null field value to the "
                           "non-finite float it stands for",
            "additionalProperties": {"enum": sorted(_NONFINITE)}},
    },
    "required": ["publication_time_ns", "record_index", "nonfinite"],
    "additionalProperties": False,
}


def _checked_fields(schema_name: str, declaration) -> list[dict]:
    fields = declaration.get("fields") if isinstance(declaration, dict) else None
    if not isinstance(fields, list):
        raise UnsupportedSchema(
            f"schema {schema_name!r} is not a SiL schema declaration")
    names = set()
    for field in fields:
        if not isinstance(field, dict) or not isinstance(field.get("name"), str):
            raise UnsupportedSchema(
                f"schema {schema_name!r} has a field without a name")
        name, type_ = field["name"], field.get("type")
        if type_ not in FORMATS:
            raise UnsupportedSchema(
                f"schema {schema_name!r} field {name!r} has unsupported type "
                f"{type_!r}")
        count = field.get("count")
        if "count" in field and (isinstance(count, bool)
                                 or not isinstance(count, int) or count < 1):
            raise UnsupportedSchema(
                f"schema {schema_name!r} field {name!r} has count {count!r}; "
                "it must be a positive integer")
        if name in names:
            raise UnsupportedSchema(
                f"schema {schema_name!r} declares field {name!r} twice")
        names.add(name)
    return fields


def _element_schema(type_: str) -> dict:
    if type_ in _WIDE_INTEGERS:
        return {"type": "string", "pattern": _DECIMAL}
    if type_ in _FLOATS:
        return {"type": ["number", "null"]}
    low, high = INT_RANGES[type_]
    return {"type": "integer", "minimum": low, "maximum": high}


def _field_schema(field: dict) -> dict:
    type_, count = field["type"], field.get("count")
    notes = []
    if type_ in _WIDE_INTEGERS:
        notes.append("decimal string")
    if type_ in _FLOATS:
        notes.append("null when non-finite, see /sil/nonfinite")
    if count is None:
        schema = _element_schema(type_)
        original = type_
    elif type_ == "u8":
        schema = {"type": "string", "contentEncoding": "base64"}
        original = f"u8[{count}]"
        notes.append("base64")
    else:
        schema = {"type": "array", "items": _element_schema(type_),
                  "minItems": count, "maxItems": count}
        original = f"{type_}[{count}]"
    note = f" ({'; '.join(notes)})" if notes else ""
    return {**schema, "description": f"SiL {original}{note}"}


def _value(field: dict, value, pointer: str, nonfinite: dict[str, str]):
    type_ = field["type"]
    if "count" not in field:
        return _element(type_, value, pointer, nonfinite)
    if type_ == "u8":
        return base64.b64encode(value).decode("ascii")
    return [_element(type_, item, f"{pointer}/{i}", nonfinite)
            for i, item in enumerate(value)]


def _element(type_: str, value, pointer: str, nonfinite: dict[str, str]):
    if type_ in _WIDE_INTEGERS:
        return str(value)
    if type_ in _FLOATS and not math.isfinite(value):
        nonfinite[pointer] = ("NaN" if math.isnan(value)
                              else "Infinity" if value > 0 else "-Infinity")
        return None
    return value


def _pointer(name: str) -> str:
    """One JSON pointer reference token (RFC 6901)."""
    return name.replace("~", "~0").replace("/", "~1")
