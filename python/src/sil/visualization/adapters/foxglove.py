"""Foxglove adapter: serve the playback controller through the Foxglove SDK."""

from __future__ import annotations

from sil.visualization.adapters import AdapterUnavailable

try:
    import foxglove  # noqa: F401
except ImportError as e:
    raise AdapterUnavailable("the Foxglove adapter needs the foxglove-sdk "
                             "package") from e
