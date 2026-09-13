"""Worst-case declared payload memory for one manifest.

Issue #55's second gate asks whether peak memory is the binding constraint on a
sensor channel. Since #75 every newly authored subscriber route declares a
capacity, so the answer is a static property of the manifest: no benchmark and
no run. This reports it, and names every route that still carries no bound at
all, whose worst case is unbounded by construction.

Two declared payload costs are counted. A bounded subscriber route may hold
`capacity` messages, at the channel schema's byte size each. A shared-memory
channel maps one arena per process participant that names it, sized
`byte_size * slots`.

It is deliberately not a resident-memory prediction. Message envelopes,
allocator overhead, the recording writer's buffers, and participant-owned
storage all sit outside it — those are what a measured peak adds on top. This is
the part a manifest promises before anything runs, which is what the gate needs.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from sil.schema import load as load_message_types

EXIT_CONFIG_ERROR = 2

_MIB = 1024 * 1024


class FootprintError(Exception):
    """The manifest cannot be read far enough to compute a footprint."""


@dataclass(frozen=True)
class Route:
    participant: str
    channel: str
    payload_bytes: int
    capacity: int | None  # None: no declared bound, so no worst case

    @property
    def total_bytes(self) -> int | None:
        return None if self.capacity is None else self.capacity * self.payload_bytes


@dataclass(frozen=True)
class Arena:
    participant: str
    channel: str
    payload_bytes: int
    slots: int

    @property
    def total_bytes(self) -> int:
        return self.slots * self.payload_bytes


def payload_bytes_per_channel(doc: dict) -> dict[str, int]:
    """Channel name -> one message's declared byte size."""
    try:
        message_types = load_message_types(doc["schemas"])
    except (KeyError, TypeError, AttributeError) as exc:
        raise FootprintError(f"cannot read schemas: {exc}") from exc

    sizes: dict[str, int] = {}
    for name, channel in doc.get("channels", {}).items():
        schema = channel.get("schema")
        if schema not in message_types:
            raise FootprintError(
                f"channel {name!r} names unknown schema {schema!r}"
            )
        sizes[name] = message_types[schema].size
    return sizes


def routes(doc: dict) -> list[Route]:
    """Every subscriber route, with the memory its queue may hold.

    A pre-#75 string entry, and a route object with no capacity, are both
    unbounded — that is the compatibility behavior the kernel still accepts.
    """
    sizes = payload_bytes_per_channel(doc)
    found: list[Route] = []
    for name, participant in sorted(doc.get("participants", {}).items()):
        for entry in participant.get("subscribes", []):
            channel = entry if isinstance(entry, str) else entry.get("channel")
            capacity = None if isinstance(entry, str) else entry.get("capacity")
            if channel not in sizes:
                raise FootprintError(
                    f"participant {name!r} subscribes to unknown channel "
                    f"{channel!r}"
                )
            found.append(Route(name, channel, sizes[channel], capacity))
    return found


def arenas(doc: dict) -> list[Arena]:
    """Every shared-memory arena the kernel maps, one per participant/channel."""
    sizes = payload_bytes_per_channel(doc)
    channels = doc.get("channels", {})
    found: list[Arena] = []
    for name, participant in sorted(doc.get("participants", {}).items()):
        if participant.get("type") != "process":
            continue
        declared = [
            entry if isinstance(entry, str) else entry.get("channel")
            for entry in participant.get("subscribes", [])
        ] + list(participant.get("publishes", []))
        for channel in declared:
            spec = channels.get(channel, {})
            if spec.get("transport") != "shm":
                continue
            found.append(
                Arena(name, channel, sizes[channel], spec.get("slots", 1))
            )
    return found


def _mib(value: int) -> str:
    return f"{value / _MIB:9.2f} MiB"


def report(doc: dict, out=None) -> None:
    # Resolved per call, not per import: a caller that replaced sys.stdout
    # (pytest's capture, a driver collecting the report) must be honoured.
    out = sys.stdout if out is None else out
    route_rows = routes(doc)
    arena_rows = arenas(doc)
    unbounded = [r for r in route_rows if r.capacity is None]

    out.write("subscriber routes\n")
    if not route_rows:
        out.write("  (none declared)\n")
    for route in route_rows:
        if route.capacity is None:
            held = "  unbounded"
        else:
            held = _mib(route.total_bytes)
        out.write(
            f"  {route.participant} <- {route.channel}"
            f"  cap {route.capacity if route.capacity is not None else '-':>6}"
            f"  x {route.payload_bytes:>10} B  = {held}\n"
        )

    if arena_rows:
        out.write("\nshared-memory arenas\n")
        for arena in arena_rows:
            out.write(
                f"  {arena.participant} <-> {arena.channel}"
                f"  slots {arena.slots:>4}"
                f"  x {arena.payload_bytes:>10} B  = "
                f"{_mib(arena.total_bytes)}\n"
            )

    bounded_total = sum(
        r.total_bytes for r in route_rows if r.total_bytes is not None
    )
    arena_total = sum(a.total_bytes for a in arena_rows)
    total = bounded_total + arena_total

    out.write("\n")
    out.write(f"  bounded route queues      {_mib(bounded_total)}\n")
    out.write(f"  shared-memory arenas      {_mib(arena_total)}\n")
    out.write(f"  declared payload total    {_mib(total)}\n")

    if unbounded:
        out.write(
            f"\n{len(unbounded)} route(s) declare no capacity, so the real "
            "worst case is unbounded and the\ntotal above is only a lower "
            "bound. Declare a capacity per issue #75 to bound them:\n"
        )
        for route in unbounded:
            out.write(f"  {route.participant} <- {route.channel}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sil-footprint",
        description="Report the worst-case declared payload memory of a "
                    "manifest, from route capacities and arena slots alone.",
    )
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args(argv)

    try:
        doc = json.loads(args.manifest.read_text())
    except OSError as exc:
        sys.stderr.write(f"sil-footprint: cannot read manifest: {exc}\n")
        return EXIT_CONFIG_ERROR
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"sil-footprint: manifest is not valid JSON: {exc}\n")
        return EXIT_CONFIG_ERROR
    if not isinstance(doc, dict):
        sys.stderr.write("sil-footprint: manifest is not a JSON object\n")
        return EXIT_CONFIG_ERROR

    try:
        report(doc)
    except FootprintError as exc:
        sys.stderr.write(f"sil-footprint: {exc}\n")
        return EXIT_CONFIG_ERROR
    return 0


if __name__ == "__main__":
    sys.exit(main())
