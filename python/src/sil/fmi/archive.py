"""The extracted FMU archives, and how long they live.

An FMU is a zip archive, and driving one means reading files out of it, so
every Run unpacks each archive before anything is loaded. The kernel starts
this process in its kernel-owned Run working directory, so an extraction is
inside the tree the kernel removes after reaping us; `TemporaryDirectory`'s own
cleanup is an eager optimization for a cooperative shutdown, not the guarantee.

Two instances of one FMU are two extractions of it: they are told apart by
where they were extracted, and nothing one of them writes belongs to the other.
"""

from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path

from sil.participant import ManifestError


class Extraction:
    """One participant's unpacked archives, dropped together at the end."""

    def __init__(self) -> None:
        self._directory = tempfile.TemporaryDirectory(
            prefix="sil-fmu-", dir=Path.cwd()
        )
        self.root = Path(self._directory.name)

    def unpack(self, archive: Path, instance: str = "") -> Path:
        """Extract one archive, under its instance name where it has one."""
        extracted = self.root
        if instance:
            extracted = self.root / instance
            extracted.mkdir()
        try:
            with zipfile.ZipFile(archive) as opened:
                opened.extractall(extracted)
        except (OSError, zipfile.BadZipFile) as error:
            named = f" of instance {instance!r}" if instance else ""
            raise ManifestError(
                f"cannot read FMU {str(archive)!r}{named}: {error}"
            ) from error
        return extracted

    def cleanup(self) -> None:
        self._directory.cleanup()
