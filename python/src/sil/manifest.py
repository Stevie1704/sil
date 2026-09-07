"""Builder for the declarative run manifest.

The manifest is the single execution input consumed by the kernel:
canonical JSON (sorted keys, compact), hashed with SHA-256 over the exact
file bytes. Anything that affects simulation output belongs in here; the
recording output *path* deliberately does not (it is a CLI argument), so
identical runs keep identical hashes.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

MANIFEST_VERSION = 1

_FIELD_TYPES = {"u8", "u16", "u32", "u64", "i8", "i16", "i32", "i64", "f32", "f64"}

# Inclusive [min, max] for the integer field types; float override values must
# be finite. The builder preserves accepted JSON numbers in the Manifest. The
# loader converts integer JSON numbers to binary64 (which may round beyond 53
# exact integer bits), retains floating JSON numbers as binary64, and narrows
# f32 exactly once more during plan compilation.
_INT_RANGES = {
    "u8": (0, 2**8 - 1),
    "u16": (0, 2**16 - 1),
    "u32": (0, 2**32 - 1),
    "u64": (0, 2**64 - 1),
    "i8": (-(2**7), 2**7 - 1),
    "i16": (-(2**15), 2**15 - 1),
    "i32": (-(2**31), 2**31 - 1),
    "i64": (-(2**63), 2**63 - 1),
}

_INTERCEPTOR_KINDS = {"drop", "drop_nth", "delay", "override"}

# Channel transports. "inline" (default) base64-encodes the payload into the
# JSON step line; "shm" hands megabyte-class payloads across the kernel↔process
# boundary through a per-channel arena, skipping base64/JSON.
_TRANSPORTS = {"inline", "shm"}
_FIELD_SIZES = {
    "u8": 1,
    "u16": 2,
    "u32": 4,
    "u64": 8,
    "i8": 1,
    "i16": 2,
    "i32": 4,
    "i64": 8,
    "f32": 4,
    "f64": 8,
}
_SIZE_MAX = sys.maxsize * 2 + 1
_FLOAT32_MAX = 3.4028234663852886e38


def _object(value, context: str) -> dict:
    if not isinstance(value, dict):
        raise ManifestError(f"{context} must be an object, got {value!r}")
    return value


def _array(value, context: str) -> list:
    if not isinstance(value, list):
        raise ManifestError(f"{context} must be an array, got {value!r}")
    return value


def _string(value, context: str) -> str:
    if not isinstance(value, str):
        raise ManifestError(f"{context} must be a string, got {value!r}")
    return value


def _integer(value, context: str, *, minimum=None, maximum=None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ManifestError(f"{context} must be an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise ManifestError(
            f"{context} must be >= {minimum}, got {value!r}"
        )
    if maximum is not None and value > maximum:
        raise ManifestError(
            f"{context} must be <= {maximum}, got {value!r}"
        )
    return value


def _channel_list(value, participant: str, key: str) -> list[str]:
    """Normalize one participant's declared Channel list to a list of names."""
    if value is None:
        return []
    context = f"participant {participant!r} {key}"
    return [
        _string(item, f"{context}[{index}]")
        for index, item in enumerate(_array(value, context))
    ]


def _reject_unknown(entry: dict, allowed: set[str], context: str) -> None:
    for key in entry:
        if key not in allowed:
            raise ManifestError(f"{context}: unknown key {key!r}")


class ManifestError(ValueError):
    """A manifest that could never be a valid kernel input."""


@dataclass(frozen=True)
class ManifestRef:
    path: Path
    hash: str


class Manifest:
    def __init__(self, duration_ns: int, *, epoch_ns: int = 0):
        duration_ns = _integer(
            duration_ns, "duration_ns", minimum=1, maximum=_SIZE_MAX
        )
        # Realtime epoch handed to shimmed process participants; affects output,
        # so it is hashed. A run's default (0) is omitted from the canonical doc
        # so manifests predating the clock shim keep byte-identical hashes.
        epoch_ns = _integer(epoch_ns, "epoch_ns", minimum=0, maximum=_SIZE_MAX)
        self._duration_ns = duration_ns
        self._epoch_ns = epoch_ns
        self._schemas: dict[str, dict] = {}
        self._channels: dict[str, dict] = {}
        self._participants: dict[str, dict] = {}

    def add_schemas(self, schemas: dict[str, dict]) -> None:
        schemas = _object(schemas, "schemas")
        for name, schema in schemas.items():
            name = _string(name, "schema name")
            schema = _object(schema, f"schema {name!r}")
            if name in self._schemas:
                raise ManifestError(f"schema {name!r} already declared")
            _reject_unknown(schema, {"fields"}, f"schema {name!r}")
            if "fields" not in schema:
                raise ManifestError(f"schema {name!r} is missing key 'fields'")
            fields = _array(schema["fields"], f"schema {name!r} key 'fields'")
            if not fields:
                raise ManifestError(f"schema {name!r} has no fields")
            field_names: set[str] = set()
            byte_size = 0
            for index, f in enumerate(fields):
                field_context = f"schema {name!r} field[{index}]"
                f = _object(f, field_context)
                _reject_unknown(f, {"name", "type", "count"}, field_context)
                field_name = f.get("name")
                if not isinstance(field_name, str) or not field_name:
                    raise ManifestError(
                        f"schema {name!r}: field name must be a non-empty string, "
                        f"got {field_name!r}"
                    )
                field_type = f.get("type")
                if not isinstance(field_type, str) or field_type not in _FIELD_TYPES:
                    raise ManifestError(
                        f"schema {name!r} field {f.get('name')!r}: "
                        f"unknown type {field_type!r}"
                    )
                # A `count` makes the field a fixed-size array of its element
                # type; it must be a positive integer. Absent means a scalar.
                count = f.get("count")
                if count is not None and (
                    not isinstance(count, int)
                    or isinstance(count, bool)
                    or count < 1
                ):
                    raise ManifestError(
                        f"schema {name!r} field {f.get('name')!r}: "
                        f"count must be an integer >= 1, got {count!r}"
                    )
                if count is not None and count > _SIZE_MAX:
                    raise ManifestError(
                        f"{field_context} key 'count' exceeds size_t: {count!r}"
                    )
                if field_name in field_names:
                    raise ManifestError(
                        f"schema {name!r} field {field_name!r}: "
                        "duplicate field name"
                    )
                field_names.add(field_name)
                elements = count if count is not None else 1
                field_size = _FIELD_SIZES[field_type] * elements
                if field_size > _SIZE_MAX or byte_size > _SIZE_MAX - field_size:
                    raise ManifestError(
                        f"schema {name!r}: byte size overflows size_t at "
                        f"field {field_name!r}"
                    )
                byte_size += field_size
            self._schemas[name] = schema

    def add_channel(
        self,
        name: str,
        *,
        schema: str,
        latency_ns: int | None = None,
        transport: str = "inline",
    ) -> None:
        name = _string(name, "channel name")
        schema = _string(schema, f"channel {name!r} schema")
        if name in self._channels:
            raise ManifestError(f"channel {name!r} already declared")
        if schema not in self._schemas:
            raise ManifestError(f"channel {name!r} references unknown schema {schema!r}")
        if latency_ns is not None:
            latency_ns = _integer(
                latency_ns,
                f"channel {name!r} latency_ns",
                minimum=0,
                maximum=_SIZE_MAX,
            )
        transport = _string(transport, f"channel {name!r} transport")
        if transport not in _TRANSPORTS:
            raise ManifestError(
                f"channel {name!r}: unknown transport {transport!r} "
                f"(expected one of {sorted(_TRANSPORTS)})"
            )
        entry: dict = {"schema": schema}
        if latency_ns is not None:
            entry["latency_ns"] = latency_ns
        # Inline is the default; omit it from the canonical doc so pre-shm
        # manifests keep byte-identical hashes.
        if transport != "inline":
            entry["transport"] = transport
        self._channels[name] = entry

    def add_interceptor(
        self,
        channel: str,
        *,
        kind: str,
        start_ns: int | None = None,
        end_ns: int | None = None,
        delay_ns: int | None = None,
        n: int | None = None,
        field: str | None = None,
        value: int | float | None = None,
    ) -> None:
        """Declare a fault interceptor on a channel's message stream.

        Interceptors live in the hashed manifest (faults are reproducible
        config) and apply in declared order. The window is half-open
        ``[start_ns, end_ns)``; both bounds are optional and default to the
        whole run. Validation here mirrors the kernel's load-time rules so a
        bad declaration fails before a kernel is ever invoked.
        """
        channel = _string(channel, "interceptor channel")
        if channel not in self._channels:
            raise ManifestError(f"interceptor references unknown channel {channel!r}")
        kind = _string(kind, f"interceptor on {channel!r} kind")
        if kind not in _INTERCEPTOR_KINDS:
            raise ManifestError(
                f"interceptor on {channel!r}: unknown kind {kind!r}"
            )

        entry: dict = {"kind": kind}
        ctx = f"interceptor on {channel!r}"

        for bound_name, bound in (("start_ns", start_ns), ("end_ns", end_ns)):
            if bound is not None:
                bound = _integer(
                    bound,
                    f"{ctx} {bound_name}",
                    minimum=0,
                    maximum=_SIZE_MAX,
                )
                entry[bound_name] = bound
        lo = start_ns if start_ns is not None else 0
        if end_ns is not None and end_ns <= lo:
            raise ManifestError(
                f"{ctx}: window end_ns must be greater than start_ns"
            )

        if kind == "delay":
            if delay_ns is None:
                raise ManifestError(f"{ctx}: delay requires delay_ns")
            delay_ns = _integer(
                delay_ns, f"{ctx} delay_ns", minimum=0, maximum=_SIZE_MAX
            )
            entry["delay_ns"] = delay_ns
        elif kind == "drop_nth":
            if n is None:
                raise ManifestError(f"{ctx}: drop_nth requires n")
            n = _integer(n, f"{ctx} n", minimum=1, maximum=_SIZE_MAX)
            entry["n"] = n
        elif kind == "override":
            if field is None or value is None:
                raise ManifestError(f"{ctx}: override requires field and value")
            field = _string(field, f"{ctx} field")
            self._check_override_value(channel, field, value, ctx)
            entry["field"] = field
            entry["value"] = value

        self._channels[channel].setdefault("interceptors", []).append(entry)

    def _check_override_value(
        self, channel: str, field: str, value: int | float, ctx: str
    ) -> None:
        schema = self._schemas[self._channels[channel]["schema"]]
        spec = next((f for f in schema["fields"] if f["name"] == field), None)
        if spec is None:
            raise ManifestError(
                f"{ctx}: override field {field!r} is not in the channel's schema"
            )
        if spec.get("count") is not None:
            raise ManifestError(
                f"{ctx}: override field {field!r} is a fixed-size array; "
                f"only scalar fields can be overridden"
            )
        ftype = spec["type"]
        if ftype in _INT_RANGES:
            lo, hi = _INT_RANGES[ftype]
            if not isinstance(value, int) or isinstance(value, bool) or not (
                lo <= value <= hi
            ):
                raise ManifestError(
                    f"{ctx}: override value {value!r} is unrepresentable in "
                    f"{field!r} ({ftype})"
                )
        elif not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ManifestError(
                f"{ctx}: override value {value!r} is not a number for "
                f"{field!r} ({ftype})"
            )
        else:
            try:
                numeric_value = float(value)
                if not math.isfinite(numeric_value) or (
                    ftype == "f32" and abs(numeric_value) > _FLOAT32_MAX
                ):
                    raise ManifestError(
                        f"{ctx}: override value {value!r} is not representable "
                        f"for {field!r} ({ftype})"
                    )
            except (OverflowError, ValueError) as exc:
                raise ManifestError(
                    f"{ctx}: override value {value!r} is not representable "
                    f"for {field!r} ({ftype})"
                ) from exc

    def add_native(
        self,
        name: str,
        *,
        library: str,
        config: dict | None = None,
        subscribes: list[str] | None = None,
        publishes: list[str] | None = None,
    ) -> None:
        """Declare a native participant and its Channel contract.

        The contract is declarative like a process participant's: the kernel
        knows every publisher before it loads participant code, and the C ABI
        stays free of a registration call. Both lists are always emitted, so a
        contract can never be silently absent from the hashed manifest.
        """
        name = _string(name, "participant name")
        library = _string(library, f"participant {name!r} library")
        if config is not None:
            config = _object(config, f"participant {name!r} config")
        self._add_participant(
            name,
            {
                "type": "native",
                "library": library,
                "config": config or {},
                "subscribes": _channel_list(subscribes, name, "subscribes"),
                "publishes": _channel_list(publishes, name, "publishes"),
            },
        )

    def add_process(
        self,
        name: str,
        *,
        command: list[str],
        step_period_ns: int,
        subscribes: list[str] | None = None,
        publishes: list[str] | None = None,
        priority: int = 0,
        shim: bool = False,
    ) -> None:
        name = _string(name, "participant name")
        command = _array(command, f"participant {name!r} command")
        if not command:
            raise ManifestError(f"participant {name!r}: command must not be empty")
        command = [
            _string(item, f"participant {name!r} command[{index}]")
            for index, item in enumerate(command)
        ]
        step_period_ns = _integer(
            step_period_ns,
            f"participant {name!r} step_period_ns",
            minimum=1,
            maximum=_SIZE_MAX,
        )
        subscribes = _channel_list(subscribes, name, "subscribes")
        publishes = _channel_list(publishes, name, "publishes")
        priority = _integer(
            priority,
            f"participant {name!r} priority",
            minimum=-(2**31),
            maximum=2**31 - 1,
        )
        if not isinstance(shim, bool):
            raise ManifestError(f"participant {name!r} shim must be a boolean")
        entry: dict = {
            "type": "process",
            "command": command,
            "step_period_ns": step_period_ns,
            "subscribes": subscribes,
            "publishes": publishes,
            "priority": priority,
        }
        # The virtual clock shim is opt-in per process participant. Only the
        # enabled case is emitted, so a shimmed and an unshimmed variant of the
        # same run hash differently while unshimmed manifests are unaffected.
        if shim:
            entry["shim"] = True
        self._add_participant(name, entry)

    def add_replay(
        self,
        name: str,
        *,
        recording: str | Path,
        channels: list[str],
    ) -> None:
        """Re-publish recorded channels from a prior run's MCAP.

        The recording is identified by its SHA-256 content hash, computed here
        from the file bytes and embedded in the manifest, so the manifest hash
        fully covers the run's stimulus. The file must exist at build time.
        """
        name = _string(name, "participant name")
        if not channels:
            raise ManifestError(
                f"participant {name!r}: replay channels must not be empty"
            )
        for ch in channels:
            if ch not in self._channels:
                raise ManifestError(
                    f"participant {name!r} replays unknown channel {ch!r}"
                )
        channels = _array(channels, f"participant {name!r} replay channels")
        channels = [
            _string(item, f"participant {name!r} replay channels[{index}]")
            for index, item in enumerate(channels)
        ]
        try:
            recording = Path(recording)
        except TypeError as exc:
            raise ManifestError(
                f"participant {name!r}: recording must be a path-like value, "
                f"got {recording!r}"
            ) from exc
        try:
            data = recording.read_bytes()
        except OSError as e:
            raise ManifestError(
                f"participant {name!r}: cannot read recording {str(recording)!r}: {e}"
            ) from e
        self._add_participant(
            name,
            {
                "type": "replay",
                "recording": str(recording),
                "recording_hash": hashlib.sha256(data).hexdigest(),
                "channels": list(channels),
            },
        )

    def _add_participant(self, name: str, entry: dict) -> None:
        name = _string(name, "participant name")
        if name in self._participants:
            raise ManifestError(f"participant {name!r} already declared")
        self._participants[name] = entry

    def _validate(self) -> None:
        for pname, p in self._participants.items():
            for key in ("subscribes", "publishes", "channels"):
                seen: set[str] = set()
                for ch in p.get(key, []):
                    if ch not in self._channels:
                        raise ManifestError(
                            f"participant {pname!r} {key} unknown channel {ch!r}"
                        )
                    if ch in seen:
                        raise ManifestError(
                            f"participant {pname!r} {key} lists channel "
                            f"{ch!r} twice"
                        )
                    seen.add(ch)

        # Open-loop replay must not race live production: a channel a live
        # participant publishes cannot also be replayed. Native and process
        # publishers go through this one path, so a collision is visible
        # wherever it is declared. Multiple live publishers stay allowed —
        # that cardinality is decided in #64, not here.
        # Name-sorted, like the kernel's manifest order, so both validators
        # name the same publisher when a channel has more than one.
        live_published: dict[str, str] = {}
        for pname, p in sorted(self._participants.items()):
            if p["type"] == "replay":
                continue
            for ch in p.get("publishes", []):
                live_published.setdefault(ch, pname)
        for pname, p in self._participants.items():
            if p["type"] != "replay":
                continue
            for ch in p["channels"]:
                publisher = live_published.get(ch)
                if publisher is not None:
                    raise ManifestError(
                        f"participant {pname!r}: replayed channel {ch!r} is also "
                        f"published by live participant {publisher!r}"
                    )

    def to_doc(self) -> dict:
        self._validate()
        doc: dict = {
            "sil_manifest": MANIFEST_VERSION,
            "duration_ns": self._duration_ns,
            "schemas": self._schemas,
            "channels": self._channels,
            "participants": self._participants,
        }
        # Absent epoch_ns means 0 (the kernel's default); omit the default so
        # pre-shim manifests keep byte-identical output.
        if self._epoch_ns:
            doc["epoch_ns"] = self._epoch_ns
        return doc

    def to_json(self) -> str:
        doc = self.to_doc()
        try:
            encoded = json.dumps(
                doc,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ManifestError(f"manifest contains a non-JSON value: {exc}") from exc
        return encoded + "\n"

    def hash(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()

    def write(self, path: str | Path) -> ManifestRef:
        path = Path(path)
        path.write_text(self.to_json())
        return ManifestRef(path=path, hash=self.hash())
