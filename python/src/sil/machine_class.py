"""The machine class of the host.

A machine class is one Linux architecture that SiL releases for. Linux
x86-64 is the reference class: some features are qualified only there.
macOS is a development host, not a machine class.
"""

from __future__ import annotations

import platform


def other_linux_machine_class() -> str | None:
    """The `uname -m` name of this host when it is Linux but not x86-64."""
    machine = platform.machine()
    if platform.system() == "Linux" and machine != "x86_64":
        return machine
    return None
