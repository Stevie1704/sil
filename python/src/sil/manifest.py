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
from dataclasses import dataclass
from pathlib import Path

MANIFEST_VERSION = 1

_FIELD_TYPES = {"u8", "u16", "u32", "u64", "i8", "i16", "i32", "i64", "f32", "f64"}

# Inclusive [min, max] for the integer field types; float types accept any
# real value and are handled separately. Used to reject override constants
# that a field could never hold.
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


class ManifestError(ValueError):
    """A manifest that could never be a valid kernel input."""


@dataclass(frozen=True)
class ManifestRef:
    path: Path
    hash: str


class Manifest:
    def __init__(self, duration_ns: int, *, epoch_ns: int = 0):
        if duration_ns <= 0:
            raise ManifestError(f"duration_ns must be positive, got {duration_ns}")
        # Realtime epoch handed to shimmed process participants; affects output,
        # so it is hashed. A run's default (0) is omitted from the canonical doc
        # so manifests predating the clock shim keep byte-identical hashes.
        if not isinstance(epoch_ns, int) or isinstance(epoch_ns, bool):
            raise ManifestError(f"epoch_ns must be an integer, got {epoch_ns!r}")
        if epoch_ns < 0:
            raise ManifestError(f"epoch_ns must be >= 0, got {epoch_ns}")
        self._duration_ns = duration_ns
        self._epoch_ns = epoch_ns
        self._schemas: dict[str, dict] = {}
        self._channels: dict[str, dict] = {}
        self._participants: dict[str, dict] = {}

    def add_schemas(self, schemas: dict[str, dict]) -> None:
        for name, schema in schemas.items():
            if name in self._schemas:
                raise ManifestError(f"schema {name!r} already declared")
            fields = schema.get("fields")
            if not fields:
                raise ManifestError(f"schema {name!r} has no fields")
            field_names: set[str] = set()
            for f in fields:
                field_name = f.get("name")
                if f.get("type") not in _FIELD_TYPES:
                    raise ManifestError(
                        f"schema {name!r} field {f.get('name')!r}: "
                        f"unknown type {f.get('type')!r}"
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
                if field_name in field_names:
                    raise ManifestError(
                        f"schema {name!r} field {field_name!r}: "
                        "duplicate field name"
                    )
                field_names.add(field_name)
            self._schemas[name] = schema

    def add_channel(
        self,
        name: str,
        *,
        schema: str,
        latency_ns: int | None = None,
        transport: str = "inline",
    ) -> None:
        if name in self._channels:
            raise ManifestError(f"channel {name!r} already declared")
        if schema not in self._schemas:
            raise ManifestError(f"channel {name!r} references unknown schema {schema!r}")
        if latency_ns is not None and latency_ns < 0:
            raise ManifestError(f"channel {name!r}: latency_ns must be >= 0")
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
        if channel not in self._channels:
            raise ManifestError(f"interceptor references unknown channel {channel!r}")
        if kind not in _INTERCEPTOR_KINDS:
            raise ManifestError(
                f"interceptor on {channel!r}: unknown kind {kind!r}"
            )

        entry: dict = {"kind": kind}
        ctx = f"interceptor on {channel!r}"

        for bound_name, bound in (("start_ns", start_ns), ("end_ns", end_ns)):
            if bound is not None:
                if bound < 0:
                    raise ManifestError(f"{ctx}: {bound_name} must be >= 0")
                entry[bound_name] = bound
        lo = start_ns if start_ns is not None else 0
        if end_ns is not None and end_ns <= lo:
            raise ManifestError(
                f"{ctx}: window end_ns must be greater than start_ns"
            )

        if kind == "delay":
            if delay_ns is None:
                raise ManifestError(f"{ctx}: delay requires delay_ns")
            if delay_ns < 0:
                raise ManifestError(f"{ctx}: delay_ns must be >= 0")
            entry["delay_ns"] = delay_ns
        elif kind == "drop_nth":
            if n is None:
                raise ManifestError(f"{ctx}: drop_nth requires n")
            if n < 1:
                raise ManifestError(f"{ctx}: n must be >= 1")
            entry["n"] = n
        elif kind == "override":
            if field is None or value is None:
                raise ManifestError(f"{ctx}: override requires field and value")
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

    def add_native(self, name: str, *, library: str, config: dict | None = None) -> None:
        self._add_participant(
            name, {"type": "native", "library": library, "config": config or {}}
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
        if step_period_ns <= 0:
            raise ManifestError(
                f"participant {name!r}: step_period_ns must be positive"
            )
        if not command:
            raise ManifestError(f"participant {name!r}: command must not be empty")
        entry: dict = {
            "type": "process",
            "command": command,
            "step_period_ns": step_period_ns,
            "subscribes": subscribes or [],
            "publishes": publishes or [],
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
        if not channels:
            raise ManifestError(
                f"participant {name!r}: replay channels must not be empty"
            )
        for ch in channels:
            if ch not in self._channels:
                raise ManifestError(
                    f"participant {name!r} replays unknown channel {ch!r}"
                )
        recording = Path(recording)
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
        if name in self._participants:
            raise ManifestError(f"participant {name!r} already declared")
        self._participants[name] = entry

    def _validate(self) -> None:
        for pname, p in self._participants.items():
            for key in ("subscribes", "publishes", "channels"):
                for ch in p.get(key, []):
                    if ch not in self._channels:
                        raise ManifestError(
                            f"participant {pname!r} {key} unknown channel {ch!r}"
                        )

        # Open-loop replay must not race live production: a channel a process
        # participant publishes cannot also be replayed. The kernel enforces
        # this at load (over process publishes); reject it here so a bad
        # manifest never gets written.
        live_published = {
            ch
            for p in self._participants.values()
            if p["type"] == "process"
            for ch in p["publishes"]
        }
        for pname, p in self._participants.items():
            if p["type"] != "replay":
                continue
            for ch in p["channels"]:
                if ch in live_published:
                    raise ManifestError(
                        f"participant {pname!r}: replayed channel {ch!r} is also "
                        f"published by a live participant"
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
        return json.dumps(self.to_doc(), sort_keys=True, separators=(",", ":")) + "\n"

    def hash(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()

    def write(self, path: str | Path) -> ManifestRef:
        path = Path(path)
        path.write_text(self.to_json())
        return ManifestRef(path=path, hash=self.hash())
