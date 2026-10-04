"""The declarations of the port binding contract that `adapter.py` reads.

A port binding declares its ports and cyclic entry points with these types,
in the order the adapter uses them. See `gap_binding.py` and
docs/library.md, "Bind several Channels and entry points".
"""

from __future__ import annotations

from typing import NamedTuple


class Field(NamedTuple):
    """One Schema field: its name, primitive type and element count."""

    name: str
    type: str
    count: int = 1


class InputPort(NamedTuple):
    """One input struct or call, fed from one input Channel."""

    name: str
    fields: tuple[Field, ...]


class OutputPort(NamedTuple):
    """One output struct or call, published after its `entry` runs."""

    name: str
    entry: str
    fields: tuple[Field, ...]


class EntryPoint(NamedTuple):
    """One cyclic entry point, due when `t >= offset_ns` and
    `(t - offset_ns) % period_ns == 0`, in integer nanoseconds."""

    name: str
    period_ns: int
    offset_ns: int = 0
