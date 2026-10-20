"""The free-space reserve of a profile directory (#313).

A collector stops its raw data when the file system of the profile
directory has less free space than RESERVE_BYTES. The reserve keeps room
for the diagnostics report of the Run and for the profile reports. Thus a
full disk does not stop the Run, and the saved Run outcome is kept. The
reserve is not a quota: other writers on the same file system can still
fill it.
"""

from __future__ import annotations

import os
from pathlib import Path

RESERVE_BYTES = 8 << 20


def free_bytes(directory: Path) -> int | None:
    """The bytes an unprivileged writer can still write, or None."""
    try:
        stat = os.statvfs(directory)
    except OSError:
        return None
    return stat.f_bavail * stat.f_frsize


def reserve_reached(directory: Path) -> bool:
    """True when the free space is below the reserve. Unknown free space
    does not stop a capture: the size limit still applies."""
    free = free_bytes(directory)
    return free is not None and free < RESERVE_BYTES


def space_warning(directory: Path, max_capture_bytes: int) -> str | None:
    """A warning when the capture limit is above the free space minus the
    reserve: the reserve then stops the capture before its limit."""
    free = free_bytes(directory)
    if free is None or free - RESERVE_BYTES >= max_capture_bytes:
        return None
    return (f"'{directory}' has {free} bytes free, less than "
            f"--max-capture-bytes {max_capture_bytes} plus the reserve of "
            f"{RESERVE_BYTES} bytes for the reports; the capture stops when "
            f"the free space falls below the reserve")
