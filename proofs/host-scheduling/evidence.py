"""Bounded proof-file access and the PID-namespace identity used by joins."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path

INPUT_BYTES = 256 << 20


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_text(path: Path) -> str:
    if path.stat().st_size > INPUT_BYTES:
        raise ValueError(f"input exceeds {INPUT_BYTES} bytes: {path}")
    return path.read_text()


def read_json(path: Path):
    return json.loads(read_text(path))


def write_json(path: Path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


@dataclass(frozen=True)
class PidNamespace:
    device: int
    inode: int

    @classmethod
    def current(cls):
        identity = os.stat("/proc/self/ns/pid")
        return cls(identity.st_dev, identity.st_ino)

    @classmethod
    def from_pair(cls, pair):
        device, inode = pair
        return cls(device, inode)

    @classmethod
    def from_argument(cls, text):
        device, inode = map(int, text.split(":"))
        return cls(device, inode)

    def as_pair(self):
        return [self.device, self.inode]

    def as_record(self):
        return {"device": self.device, "inode": self.inode}
