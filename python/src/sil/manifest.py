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


class ManifestError(ValueError):
    """A manifest that could never be a valid kernel input."""


@dataclass(frozen=True)
class ManifestRef:
    path: Path
    hash: str


class Manifest:
    def __init__(self, duration_ns: int):
        if duration_ns <= 0:
            raise ManifestError(f"duration_ns must be positive, got {duration_ns}")
        self._duration_ns = duration_ns
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
            for f in fields:
                if f.get("type") not in _FIELD_TYPES:
                    raise ManifestError(
                        f"schema {name!r} field {f.get('name')!r}: "
                        f"unknown type {f.get('type')!r}"
                    )
            self._schemas[name] = schema

    def add_channel(self, name: str, *, schema: str, latency_ns: int | None = None) -> None:
        if name in self._channels:
            raise ManifestError(f"channel {name!r} already declared")
        if schema not in self._schemas:
            raise ManifestError(f"channel {name!r} references unknown schema {schema!r}")
        if latency_ns is not None and latency_ns < 0:
            raise ManifestError(f"channel {name!r}: latency_ns must be >= 0")
        entry: dict = {"schema": schema}
        if latency_ns is not None:
            entry["latency_ns"] = latency_ns
        self._channels[name] = entry

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
    ) -> None:
        if step_period_ns <= 0:
            raise ManifestError(
                f"participant {name!r}: step_period_ns must be positive"
            )
        if not command:
            raise ManifestError(f"participant {name!r}: command must not be empty")
        self._add_participant(
            name,
            {
                "type": "process",
                "command": command,
                "step_period_ns": step_period_ns,
                "subscribes": subscribes or [],
                "publishes": publishes or [],
                "priority": priority,
            },
        )

    def _add_participant(self, name: str, entry: dict) -> None:
        if name in self._participants:
            raise ManifestError(f"participant {name!r} already declared")
        self._participants[name] = entry

    def _validate(self) -> None:
        for pname, p in self._participants.items():
            for key in ("subscribes", "publishes"):
                for ch in p.get(key, []):
                    if ch not in self._channels:
                        raise ManifestError(
                            f"participant {pname!r} {key} unknown channel {ch!r}"
                        )

    def to_doc(self) -> dict:
        self._validate()
        return {
            "sil_manifest": MANIFEST_VERSION,
            "duration_ns": self._duration_ns,
            "schemas": self._schemas,
            "channels": self._channels,
            "participants": self._participants,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_doc(), sort_keys=True, separators=(",", ":")) + "\n"

    def hash(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()

    def write(self, path: str | Path) -> ManifestRef:
        path = Path(path)
        path.write_text(self.to_json())
        return ManifestRef(path=path, hash=self.hash())
